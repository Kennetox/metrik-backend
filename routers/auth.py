from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import schemas, crud
from database import get_db
from security import (
    verify_password,
    create_access_token,
    POS_TOKEN_TTL_SECONDS,
    WEB_TOKEN_TTL_SECONDS,
    verify_access_token,
    WEB_INACTIVITY_TIMEOUT_SECONDS,
)
from services import email as email_service
from services.password_reset import (
    PASSWORD_RESET_TOKEN_TTL_SECONDS,
    build_reset_link,
    generate_token_and_expiry,
)

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)


@router.post("/login", response_model=schemas.AuthLoginResponse)
def login(
    payload: schemas.AuthLoginRequest,
    db: Session = Depends(get_db),
):
    user = crud.get_pos_user_by_email(db, payload.email)
    if (
        not user
        or not user.is_active
        or user.status != "Activo"
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
        )

    crud.revoke_user_sessions(db, user.id, reason="replaced")
    token = create_access_token(user.id, user.role, WEB_TOKEN_TTL_SECONDS)
    expires_at = datetime.utcnow() + timedelta(seconds=WEB_TOKEN_TTL_SECONDS)
    crud.create_pos_session(
        db,
        user_id=user.id,
        token=token,
        session_type="web",
        expires_at=expires_at,
    )
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    user_read = schemas.PosUserRead.model_validate(user)
    return schemas.AuthLoginResponse(token=token, user=user_read, expires_at=expires_at)


@router.post("/logout")
def logout(
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        return {"detail": "Sesión finalizada. Elimina el token en el cliente."}
    token = authorization.split(" ", 1)[1]
    session = crud.get_session_by_token(db, token)
    if session and not session.revoked_at:
        session.revoked_at = datetime.utcnow()
        session.revoked_reason = "logout"
        db.commit()
    return {"detail": "Sesión finalizada. Elimina el token en el cliente."}


@router.get("/session-status")
def session_status(
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        return {"status": "invalid", "reason": "missing"}
    token = authorization.split(" ", 1)[1]
    try:
        verify_access_token(token)
    except ValueError as exc:
        reason = "expired" if "expirado" in str(exc).lower() else "invalid"
        return {"status": "invalid", "reason": reason}

    session = crud.get_session_by_token(db, token)
    if not session:
        return {"status": "invalid", "reason": "missing"}
    if session.revoked_at:
        return {"status": "revoked", "reason": session.revoked_reason}

    now = datetime.utcnow()
    if session.expires_at < now:
        session.revoked_at = now
        session.revoked_reason = "expired"
        db.commit()
        return {"status": "invalid", "reason": "expired"}

    if (
        session.session_type == "web"
        and session.last_seen_at
        and (now - session.last_seen_at).total_seconds()
        > WEB_INACTIVITY_TIMEOUT_SECONDS
    ):
        session.revoked_at = now
        session.revoked_reason = "inactive"
        db.commit()
        return {"status": "invalid", "reason": "inactive"}

    return {"status": "active"}


@router.post("/pos-login", response_model=schemas.AuthLoginResponse)
def pos_login(
    payload: schemas.AuthPosLoginRequest,
    db: Session = Depends(get_db),
):
    if not payload.pin:
        raise HTTPException(status_code=400, detail="PIN requerido")
    station = crud.get_pos_station(db, payload.station_id)
    if not station or not station.is_active:
        raise HTTPException(status_code=400, detail="Estación inválida o inactiva")
    try:
        user = crud.get_pos_user_by_pin(db, payload.pin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not user or not user.is_active or user.status != "Activo":
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(status_code=401, detail="PIN inválido o usuario inactivo")

    if payload.device_id:
        if station.bound_device_id and station.bound_device_id != payload.device_id:
            raise HTTPException(
                status_code=409,
                detail="Esta estación ya está vinculada a otro equipo. Solicita al administrador que la libere.",
            )
        if not station.bound_device_id:
            station.bound_device_id = payload.device_id
            station.bound_device_label = payload.device_label
            station.bound_at = datetime.utcnow()
            station.bound_by_user_id = user.id
            station.bound_by_user_name = user.name
        elif payload.device_label and not station.bound_device_label:
            station.bound_device_label = payload.device_label

    crud.register_pos_station_login_success(db, station)
    crud.revoke_user_sessions(db, user.id, reason="replaced")
    token = create_access_token(user.id, user.role, POS_TOKEN_TTL_SECONDS)
    expires_at = datetime.utcnow() + timedelta(seconds=POS_TOKEN_TTL_SECONDS)
    crud.create_pos_session(
        db,
        user_id=user.id,
        token=token,
        session_type="pos",
        expires_at=expires_at,
        station_id=station.id,
        device_id=payload.device_id,
    )
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    user_read = schemas.PosUserRead.model_validate(user)
    return schemas.AuthLoginResponse(token=token, user=user_read, expires_at=expires_at)


@router.post(
    "/pos-station-login",
    response_model=schemas.AuthPosStationLoginResponse,
)
def pos_station_login(
    payload: schemas.AuthPosStationLoginRequest,
    db: Session = Depends(get_db),
):
    station = crud.get_pos_station_by_email(db, payload.station_email)
    if not station or not station.is_active:
        raise HTTPException(status_code=400, detail="Estación inválida o inactiva")

    if not station.station_password_hash or not verify_password(
        payload.station_password, station.station_password_hash
    ):
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    if payload.device_id:
        if station.bound_device_id and station.bound_device_id != payload.device_id:
            raise HTTPException(
                status_code=409,
                detail="Esta estación ya está vinculada a otro equipo. Solicita al administrador que la libere.",
            )
        if not station.bound_device_id:
            station.bound_device_id = payload.device_id
            station.bound_device_label = payload.device_label
            station.bound_at = datetime.utcnow()
            station.bound_by_user_id = None
            station.bound_by_user_name = None
        elif payload.device_label and not station.bound_device_label:
            station.bound_device_label = payload.device_label

    crud.register_pos_station_login_success(db, station)
    db.commit()
    db.refresh(station)

    return schemas.AuthPosStationLoginResponse(
        station_id=station.id,
        station_label=station.label,
        station_email=station.station_email or payload.station_email,
    )


@router.post("/forgot-password")
def forgot_password(
    payload: schemas.AuthForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    user = crud.get_pos_user_by_email(db, payload.email)
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    crud.invalidate_password_reset_tokens(db, user.id)
    token, expires_at = generate_token_and_expiry()
    crud.create_password_reset_token(db, user, token, expires_at)

    settings = crud.get_pos_settings(db)
    reset_link = build_reset_link(token)
    subject = "Recuperación de contraseña"
    html_body = (
        f"<p>Hola {user.name or user.email},</p>"
        f"<p>Recibimos una solicitud para restablecer tu contraseña en Metrik. "
        f"Haz clic en el siguiente enlace para continuar:</p>"
        f"<p><a href='{reset_link}' target='_blank'>Restablecer contraseña</a></p>"
        f"<p>El enlace expirará en {PASSWORD_RESET_TOKEN_TTL_SECONDS // 60} minutos.</p>"
        "<p>Si no solicitaste este cambio, puedes ignorar este correo.</p>"
    )

    try:
        email_service.send_email(
            recipients=[user.email],
            subject=subject,
            html_body=html_body,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "detail": "Enviamos un correo con instrucciones para restablecer tu contraseña.",
        "expires_in": PASSWORD_RESET_TOKEN_TTL_SECONDS,
    }


@router.post("/reset-password")
def reset_password(
    payload: schemas.AuthResetPasswordRequest,
    db: Session = Depends(get_db),
):
    reset_entry = crud.get_password_reset_by_token(db, payload.token)
    if not reset_entry:
        raise HTTPException(status_code=400, detail="Token inválido")
    if reset_entry.used_at is not None:
        raise HTTPException(status_code=400, detail="Token ya utilizado")
    if reset_entry.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Token expirado")
    if not reset_entry.user or not reset_entry.user.is_active:
        raise HTTPException(status_code=400, detail="Usuario inválido")

    crud.complete_password_reset(db, reset_entry, payload.password)
    return {"detail": "Contraseña actualizada correctamente"}


@router.post(
    "/validate-reset-token",
    response_model=schemas.AuthValidateResetTokenResponse,
)
def validate_reset_token(
    payload: schemas.AuthValidateResetTokenRequest,
    db: Session = Depends(get_db),
):
    reset_entry = crud.get_password_reset_by_token(db, payload.token)
    if (
        not reset_entry
        or reset_entry.used_at is not None
        or reset_entry.expires_at < datetime.utcnow()
    ):
        return schemas.AuthValidateResetTokenResponse(valid=False, expires_at=None)
    return schemas.AuthValidateResetTokenResponse(
        valid=True,
        expires_at=reset_entry.expires_at,
    )
