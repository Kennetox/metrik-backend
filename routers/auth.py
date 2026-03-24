from datetime import datetime, timedelta
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
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

PLATFORM_LOGIN_2FA_CODE_TTL_SECONDS = 10 * 60
PLATFORM_LOGIN_2FA_MAX_ATTEMPTS = 5
PLATFORM_TRUSTED_DEVICE_DAYS = 30


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "correo registrado"
    if len(local) <= 2:
        masked_local = f"{local[0]}*" if local else "*"
    else:
        masked_local = f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}"
    return f"{masked_local}@{domain}"


def _issue_platform_session(
    db: Session,
    user,
    trusted_device_token: str | None = None,
) -> schemas.PlatformLoginResponse:
    token = create_access_token(
        user.id,
        role="PlatformAdmin",
        ttl=WEB_TOKEN_TTL_SECONDS,
        subject_type="platform",
    )
    expires_at = datetime.utcnow() + timedelta(seconds=WEB_TOKEN_TTL_SECONDS)
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)
    user_read = schemas.PlatformUserRead.model_validate(user)
    return schemas.PlatformLoginResponse(
        token=token,
        user=user_read,
        expires_at=expires_at,
        trusted_device_token=trusted_device_token,
    )


def _build_pos_session_conflict_detail(
    db: Session,
    conflict: object,
) -> str:
    conflict_station_id = getattr(conflict, "station_id", None)
    conflict_type = getattr(conflict, "session_type", None) or "pos"
    station_label = None
    if conflict_station_id:
        station = crud.get_pos_station(
            db,
            conflict_station_id,
            tenant_id=getattr(conflict, "tenant_id", None),
        )
        if station:
            station_label = station.label
    if station_label:
        return (
            "Este usuario ya tiene una sesión abierta en otro POS "
            f"({station_label}). Cierra esa sesión para continuar."
        )
    if conflict_type == "tablet":
        return (
            "Este usuario ya tiene una sesión abierta en otra tablet POS. "
            "Cierra esa sesión para continuar."
        )
    return (
        "Este usuario ya tiene una sesión abierta en otro POS. "
        "Cierra esa sesión para continuar."
    )


def _ensure_tablet_station_ready(db: Session, station):
    if (station.station_type or "desktop") != "tablet":
        raise HTTPException(
            status_code=400,
            detail="Esta estación no está configurada como tablet.",
        )
    if station.tenant_id is None:
        raise HTTPException(status_code=400, detail="Estación sin empresa asignada")
    if not station.parent_station_id:
        raise HTTPException(
            status_code=400,
            detail="La estación tablet debe estar vinculada a una estación desktop activa.",
        )
    parent_station = crud.get_pos_station(
        db,
        station.parent_station_id,
        tenant_id=station.tenant_id,
    )
    if not parent_station or not parent_station.is_active:
        raise HTTPException(
            status_code=400,
            detail="La estación desktop vinculada no existe o está inactiva.",
        )
    if (parent_station.station_type or "desktop") != "desktop":
        raise HTTPException(
            status_code=400,
            detail="La estación vinculada para tablet debe ser de tipo desktop.",
        )
    return parent_station


@router.post("/login", response_model=schemas.AuthLoginResponse)
def login(
    payload: schemas.AuthLoginRequest,
    db: Session = Depends(get_db),
):
    tenant_id = None
    tenant = None
    if payload.tenant_slug:
        tenant = crud.get_tenant_by_slug(db, payload.tenant_slug.strip().lower())
        access_issue = crud.get_tenant_access_issue(tenant)
        if access_issue:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=access_issue,
            )
        tenant_id = tenant.id

    if tenant_id is not None:
        user = crud.get_pos_user_by_email(db, payload.email, tenant_id=tenant_id)
    else:
        user = crud.get_pos_user_by_email_global(db, payload.email)
        if user and user.tenant_id:
            tenant = crud.get_tenant(db, int(user.tenant_id))
            access_issue = crud.get_tenant_access_issue(tenant)
            if access_issue:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=access_issue,
                )

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

    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario sin empresa asignada",
        )

    if tenant is None and user.tenant_id is not None:
        tenant = crud.get_tenant(db, int(user.tenant_id))
        access_issue = crud.get_tenant_access_issue(tenant)
        if access_issue:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=access_issue,
            )

    crud.revoke_user_sessions(db, user.id, reason="replaced", session_type="web")
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
    return schemas.AuthLoginResponse(
        token=token,
        user=user_read,
        tenant=crud.build_tenant_session_read(tenant, user),
        expires_at=expires_at,
    )


@router.post("/demo/start", response_model=schemas.DemoStartResponse, status_code=201)
def start_demo(
    payload: schemas.DemoStartRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    normalized_email = payload.admin_email.strip().lower()
    active_demo = crud.get_active_demo_by_email(db, normalized_email)
    if active_demo:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una demo activa asociada a este correo.",
        )

    cooldown_hits = crud.get_recent_demo_signups_by_email(
        db,
        normalized_email,
        since=datetime.utcnow() - timedelta(days=30),
    )
    if cooldown_hits:
        raise HTTPException(
            status_code=429,
            detail="Este correo ya usó una demo recientemente. Contáctanos si necesitas reactivarla.",
        )

    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else None)
    )
    if client_ip:
        ip_count = crud.count_recent_demo_signups_by_ip(
            db,
            client_ip,
            since=datetime.utcnow() - timedelta(hours=24),
        )
        if ip_count >= 3:
            raise HTTPException(
                status_code=429,
                detail="Se alcanzó el límite de demos para esta red. Contáctanos para ayudarte.",
            )

    try:
        tenant, admin_user = crud.create_demo_tenant_with_admin(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    crud.revoke_user_sessions(db, admin_user.id, reason="replaced", session_type="web")
    token = create_access_token(admin_user.id, admin_user.role, WEB_TOKEN_TTL_SECONDS)
    expires_at = datetime.utcnow() + timedelta(seconds=WEB_TOKEN_TTL_SECONDS)
    crud.create_pos_session(
        db,
        user_id=admin_user.id,
        token=token,
        session_type="web",
        expires_at=expires_at,
    )
    admin_user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(admin_user)
    db.refresh(tenant)
    crud.record_demo_signup_audit(
        db,
        tenant_id=tenant.id,
        email=normalized_email,
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
    )

    settings = crud.get_pos_settings(db)
    internal_subject = f"Nueva demo creada: {tenant.name}"
    internal_body = (
        f"<p>Se creó una nueva demo de Metrik.</p>"
        f"<p><strong>Empresa:</strong> {tenant.name}</p>"
        f"<p><strong>Slug:</strong> {tenant.slug}</p>"
        f"<p><strong>Tipo de negocio:</strong> {payload.business_type or 'No especificado'}</p>"
        f"<p><strong>Admin:</strong> {admin_user.name}</p>"
        f"<p><strong>Correo:</strong> {admin_user.email}</p>"
        f"<p><strong>Teléfono admin:</strong> {payload.admin_phone or 'No especificado'}</p>"
        f"<p><strong>Teléfono empresa:</strong> {payload.company_phone or 'No especificado'}</p>"
        f"<p><strong>Ciudad:</strong> {payload.company_city or 'No especificada'}</p>"
        f"<p><strong>Vence:</strong> {tenant.trial_ends_at}</p>"
    )
    welcome_subject = "Tu demo de Metrik ya está lista"
    welcome_body = (
        f"<p>Hola {admin_user.name},</p>"
        f"<p>Tu demo de <strong>{tenant.name}</strong> fue creada correctamente.</p>"
        f"<p>Puedes ingresar con tu correo <strong>{admin_user.email}</strong> y usar Metrik por 7 días.</p>"
        "<p>Gracias por probar Metrik.</p>"
    )
    try:
        email_service.send_email(
            recipients=["kennethjc2301@gmail.com"],
            subject=internal_subject,
            html_body=internal_body,
            smtp_config=settings,
        )
        email_service.send_email(
            recipients=[admin_user.email],
            subject=welcome_subject,
            html_body=welcome_body,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return schemas.DemoStartResponse(
        token=token,
        user=schemas.PosUserRead.model_validate(admin_user),
        tenant=crud.build_tenant_session_read(tenant, admin_user),
        expires_at=expires_at,
    )


@router.post("/platform-login", response_model=schemas.PlatformLoginResponse)
def platform_login(
    payload: schemas.PlatformLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    user = crud.get_platform_user_by_email(db, payload.email)
    if (
        not user
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
        )

    if payload.device_token:
        trusted = crud.get_platform_trusted_device(db, user.id, payload.device_token)
        if trusted:
            return _issue_platform_session(db, user)

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    expires_at = datetime.utcnow() + timedelta(seconds=PLATFORM_LOGIN_2FA_CODE_TTL_SECONDS)
    challenge = crud.create_platform_login_2fa_challenge(
        db,
        user,
        code=code,
        expires_at=expires_at,
        user_agent=request.headers.get("user-agent"),
        ip_address=(
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None)
        ),
    )

    settings = crud.get_pos_settings(db)
    try:
        email_service.send_email(
            recipients=[user.email],
            subject="Codigo de verificacion para Platform",
            html_body=(
                f"<p>Hola {user.name or user.email},</p>"
                f"<p>Tu codigo para ingresar a Platform es:</p>"
                f"<p style='font-size:24px;font-weight:bold;letter-spacing:2px'>{code}</p>"
                f"<p>Este codigo expira en {PLATFORM_LOGIN_2FA_CODE_TTL_SECONDS // 60} minutos.</p>"
            ),
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=schemas.PlatformLogin2FARequiredResponse(
            challenge_id=challenge.id,
            masked_email=_mask_email(user.email),
            expires_in=PLATFORM_LOGIN_2FA_CODE_TTL_SECONDS,
        ).model_dump(),
    )


@router.post("/platform-login/verify-2fa", response_model=schemas.PlatformLoginResponse)
def platform_verify_login_2fa(
    payload: schemas.PlatformVerify2FARequest,
    request: Request,
    db: Session = Depends(get_db),
):
    challenge = crud.get_platform_login_2fa_challenge(db, payload.challenge_id)
    if not challenge:
        raise HTTPException(status_code=404, detail="Verificacion no encontrada.")
    user = challenge.user
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Usuario invalido o inactivo.")

    is_valid = crud.verify_platform_login_2fa_code(
        db,
        challenge,
        payload.code,
        max_attempts=PLATFORM_LOGIN_2FA_MAX_ATTEMPTS,
    )
    if not is_valid:
        raise HTTPException(
            status_code=401,
            detail="Codigo invalido, expirado o con demasiados intentos.",
        )

    trusted_token_to_return = None
    if payload.remember_device:
        trusted_token = payload.device_token.strip() if payload.device_token else secrets.token_urlsafe(32)
        trusted_token_to_return = trusted_token
        crud.trust_platform_device(
            db,
            user,
            trusted_token,
            expires_at=datetime.utcnow() + timedelta(days=PLATFORM_TRUSTED_DEVICE_DAYS),
            device_label=payload.device_label,
            user_agent=request.headers.get("user-agent"),
            ip_address=(
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else None)
            ),
        )

    return _issue_platform_session(
        db,
        user,
        trusted_device_token=trusted_token_to_return,
    )


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

    # Valida que exista resolución de tenant para la sesión activa.
    session_user = crud.get_pos_user(db, session.user_id, tenant_id=session.tenant_id)
    if not session_user:
        return {"status": "invalid", "reason": "missing"}
    if crud.resolve_user_tenant_id(db, session_user) is None:
        return {"status": "invalid", "reason": "tenant_missing"}
    tenant = crud.get_tenant(db, session.tenant_id) if session.tenant_id else None
    access_issue = crud.get_tenant_access_issue(tenant)
    if access_issue:
        return {"status": "invalid", "reason": "tenant_blocked", "detail": access_issue}

    return {"status": "active"}


@router.post("/pos-login", response_model=schemas.AuthLoginResponse)
def pos_login(
    payload: schemas.AuthPosLoginRequest,
    db: Session = Depends(get_db),
):
    station = crud.get_pos_station_any(db, payload.station_id)
    if not station or not station.is_active:
        raise HTTPException(status_code=400, detail="Estación inválida o inactiva")
    station_tenant_id = station.tenant_id
    if station_tenant_id is None:
        raise HTTPException(status_code=400, detail="Estación sin empresa asignada")
    user = None
    if payload.pin:
        try:
            user = crud.get_pos_user_by_pin(db, payload.pin, tenant_id=station_tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif payload.email and payload.password:
        user = crud.get_pos_user_by_email(db, payload.email, tenant_id=station_tenant_id)
        if not user or not verify_password(payload.password, user.password_hash):
            user = None
    else:
        raise HTTPException(
            status_code=400,
            detail="Debes ingresar PIN o correo y contraseña.",
        )
    if not user or not user.is_active or user.status != "Activo":
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(
            status_code=401, detail="Credenciales inválidas o usuario inactivo"
        )
    tenant = crud.get_tenant(db, int(user.tenant_id)) if user.tenant_id else None
    access_issue = crud.get_tenant_access_issue(tenant)
    if access_issue:
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(status_code=401, detail=access_issue)

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

    conflict = crud.get_active_pos_session_conflict(
        db,
        user_id=user.id,
        current_station_id=station.id,
        session_types=("pos", "tablet"),
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=_build_pos_session_conflict_detail(db, conflict),
        )

    crud.register_pos_station_login_success(db, station)
    crud.revoke_user_sessions(db, user.id, reason="replaced", session_type="pos")
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
    return schemas.AuthLoginResponse(
        token=token,
        user=user_read,
        tenant=crud.build_tenant_session_read(tenant, user),
        expires_at=expires_at,
    )


@router.post("/tablet-login", response_model=schemas.AuthLoginResponse)
def tablet_login(
    payload: schemas.AuthPosLoginRequest,
    db: Session = Depends(get_db),
):
    station_lookup = payload.station_id.strip()
    if not station_lookup:
        raise HTTPException(status_code=400, detail="Debes configurar una estación tablet.")

    station = crud.get_pos_station_any(db, station_lookup)
    if not station or not station.is_active:
        raise HTTPException(status_code=400, detail="Estación inválida o inactiva")
    _ensure_tablet_station_ready(db, station)
    station_tenant_id = station.tenant_id

    if not payload.pin:
        raise HTTPException(status_code=400, detail="Debes ingresar PIN.")

    try:
        user = crud.get_pos_user_by_pin(db, payload.pin, tenant_id=station_tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="PIN inválido") from exc

    if not user or not user.is_active or user.status != "Activo":
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(
            status_code=401, detail="PIN inválido o usuario inactivo"
        )
    tenant = crud.get_tenant(db, int(user.tenant_id)) if user.tenant_id else None
    access_issue = crud.get_tenant_access_issue(tenant)
    if access_issue:
        crud.register_pos_station_login_failure(db, station)
        raise HTTPException(status_code=401, detail=access_issue)

    if payload.email:
        if not user.email or user.email.lower() != payload.email.lower():
            crud.register_pos_station_login_failure(db, station)
            raise HTTPException(
                status_code=401, detail="El PIN no corresponde al correo validado."
            )

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

    conflict = crud.get_active_pos_session_conflict(
        db,
        user_id=user.id,
        current_station_id=station.id,
        session_types=("pos", "tablet"),
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=_build_pos_session_conflict_detail(db, conflict),
        )

    crud.register_pos_station_login_success(db, station)
    crud.revoke_user_sessions(db, user.id, reason="replaced", session_type="tablet")
    token = create_access_token(user.id, user.role, POS_TOKEN_TTL_SECONDS)
    expires_at = datetime.utcnow() + timedelta(seconds=POS_TOKEN_TTL_SECONDS)
    crud.create_pos_session(
        db,
        user_id=user.id,
        token=token,
        session_type="tablet",
        expires_at=expires_at,
        station_id=station.id,
        device_id=payload.device_id,
    )
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    user_read = schemas.PosUserRead.model_validate(user)
    return schemas.AuthLoginResponse(
        token=token,
        user=user_read,
        tenant=crud.build_tenant_session_read(tenant, user),
        expires_at=expires_at,
    )


@router.post(
    "/tablet-email-check",
    response_model=schemas.AuthTabletEmailCheckResponse,
)
def tablet_email_check(
    payload: schemas.AuthTabletEmailCheckRequest,
    db: Session = Depends(get_db),
):
    station = crud.get_pos_station_any(db, payload.station_id)
    if not station or not station.is_active:
        raise HTTPException(status_code=400, detail="Estación inválida o inactiva")
    _ensure_tablet_station_ready(db, station)
    tenant_id = station.tenant_id

    user = crud.get_pos_user_by_email(db, payload.email, tenant_id=tenant_id)
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(status_code=404, detail="Correo no encontrado o inactivo")

    return schemas.AuthTabletEmailCheckResponse(
        exists=True,
        user=schemas.PosUserRead.model_validate(user),
    )


@router.post(
    "/mobile-stock-email-check",
    response_model=schemas.AuthTabletEmailCheckResponse,
)
def mobile_stock_email_check(
    payload: schemas.AuthMobileStockEmailCheckRequest,
    db: Session = Depends(get_db),
):
    user = crud.get_pos_user_by_email_global(db, payload.email)
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(status_code=404, detail="Correo no encontrado o inactivo")
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Usuario sin empresa asignada")
    tenant = crud.get_tenant(db, int(user.tenant_id))
    access_issue = crud.get_tenant_access_issue(tenant)
    if access_issue:
        raise HTTPException(status_code=401, detail=access_issue)
    return schemas.AuthTabletEmailCheckResponse(
        exists=True,
        user=schemas.PosUserRead.model_validate(user),
    )


@router.post("/mobile-stock-login", response_model=schemas.AuthLoginResponse)
def mobile_stock_login(
    payload: schemas.AuthMobileStockLoginRequest,
    db: Session = Depends(get_db),
):
    user = crud.get_pos_user_by_email_global(db, payload.email)
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Usuario sin empresa asignada")
    tenant = crud.get_tenant(db, int(user.tenant_id))
    access_issue = crud.get_tenant_access_issue(tenant)
    if access_issue:
        raise HTTPException(status_code=401, detail=access_issue)

    if not user.pin_hash or not verify_password(payload.pin, user.pin_hash):
        raise HTTPException(status_code=401, detail="PIN inválido")

    crud.revoke_user_sessions(db, user.id, reason="replaced", session_type="stock-mobile")
    token = create_access_token(user.id, user.role, POS_TOKEN_TTL_SECONDS)
    expires_at = datetime.utcnow() + timedelta(seconds=POS_TOKEN_TTL_SECONDS)
    crud.create_pos_session(
        db,
        user_id=user.id,
        token=token,
        session_type="stock-mobile",
        expires_at=expires_at,
        device_id=payload.device_id,
    )
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    user_read = schemas.PosUserRead.model_validate(user)
    return schemas.AuthLoginResponse(
        token=token,
        user=user_read,
        tenant=crud.build_tenant_session_read(tenant, user),
        expires_at=expires_at,
    )


@router.post(
    "/pos-station-login",
    response_model=schemas.AuthPosStationLoginResponse,
)
def pos_station_login(
    payload: schemas.AuthPosStationLoginRequest,
    db: Session = Depends(get_db),
):
    station = crud.get_pos_station_by_email_any(db, payload.station_email)
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
    tenant_name = None
    resolved_tenant_id = station.tenant_id
    if resolved_tenant_id is None and getattr(station, "parent_station", None):
        resolved_tenant_id = station.parent_station.tenant_id
    if resolved_tenant_id is None and getattr(station, "user", None):
        resolved_tenant_id = station.user.tenant_id
    if resolved_tenant_id:
        tenant = crud.get_tenant(db, int(resolved_tenant_id))
        tenant_name = tenant.name if tenant else None

    return schemas.AuthPosStationLoginResponse(
        station_id=station.id,
        station_label=station.label,
        station_email=station.station_email or payload.station_email,
        tenant_name=tenant_name,
        parent_station_id=station.parent_station_id,
        parent_station_label=(
            station.parent_station.label
            if getattr(station, "parent_station", None)
            else None
        ),
    )


@router.post("/forgot-password")
def forgot_password(
    payload: schemas.AuthForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    user = crud.get_pos_user_by_email_global(db, payload.email)
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
