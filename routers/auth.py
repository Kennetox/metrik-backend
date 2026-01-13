from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas, crud
from database import get_db
from security import verify_password, create_access_token
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

    token = create_access_token(user.id, user.role)
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    user_read = schemas.PosUserRead.model_validate(user)
    return schemas.AuthLoginResponse(token=token, user=user_read)


@router.post("/logout")
def logout():
    return {"detail": "Sesión finalizada. Elimina el token en el cliente."}


@router.post("/pos-login", response_model=schemas.AuthLoginResponse)
def pos_login(
    payload: schemas.AuthPosLoginRequest,
    db: Session = Depends(get_db),
):
    station = crud.get_pos_station(db, payload.station_id)
    if not station or not station.is_active:
        raise HTTPException(status_code=400, detail="Estación inválida o inactiva")
    user = station.user
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(status_code=400, detail="Usuario no disponible para esta estación")

    if not verify_password(payload.pin, station.pin_hash):
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(status_code=401, detail="PIN inválido o expirado")

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
    token = create_access_token(user.id, user.role)
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    user_read = schemas.PosUserRead.model_validate(user)
    return schemas.AuthLoginResponse(token=token, user=user_read)


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
