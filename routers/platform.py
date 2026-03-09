from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud, models, schemas
from database import get_db
from dependencies import require_platform_admin
from services import email as email_service
from services.password_reset import (
    PASSWORD_RESET_TOKEN_TTL_SECONDS,
    build_reset_link,
    generate_token_and_expiry,
)


router = APIRouter(
    prefix="/platform",
    tags=["platform"],
)


@router.get("/tenants", response_model=List[schemas.PlatformTenantRead])
def list_platform_tenants(
    db: Session = Depends(get_db),
    _: models.PlatformUser = Depends(require_platform_admin),
):
    return crud.list_platform_tenant_reads(db)


@router.post("/tenants", response_model=schemas.PlatformTenantCreateResponse, status_code=201)
def create_platform_tenant(
    payload: schemas.PlatformTenantCreateRequest,
    db: Session = Depends(get_db),
    _: models.PlatformUser = Depends(require_platform_admin),
):
    try:
        tenant, admin_user = crud.create_tenant_with_admin(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.PlatformTenantCreateResponse(
        tenant=crud.build_platform_tenant_read(db, tenant),
        admin_user=schemas.PosUserRead.model_validate(admin_user),
    )


@router.patch("/tenants/{tenant_id}", response_model=schemas.PlatformTenantRead)
def update_platform_tenant(
    tenant_id: int,
    payload: schemas.PlatformTenantUpdateRequest,
    db: Session = Depends(get_db),
    _: models.PlatformUser = Depends(require_platform_admin),
):
    tenant = crud.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    updated = crud.update_tenant(db, tenant, payload)
    return crud.build_platform_tenant_read(db, updated)


@router.post(
    "/tenants/{tenant_id}/admin/recovery",
    response_model=schemas.PlatformTenantRecoveryResponse,
)
def send_platform_tenant_admin_recovery(
    tenant_id: int,
    db: Session = Depends(get_db),
    _: models.PlatformUser = Depends(require_platform_admin),
):
    tenant = crud.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    admin_user = crud.get_tenant_primary_admin(db, tenant.id)
    if not admin_user or not admin_user.is_active or admin_user.status != "Activo":
        raise HTTPException(
            status_code=400,
            detail="La empresa no tiene un administrador principal activo",
        )
    if not admin_user.email:
        raise HTTPException(
            status_code=400,
            detail="El administrador principal no tiene un correo configurado",
        )

    crud.invalidate_password_reset_tokens(db, admin_user.id)
    token, expires_at = generate_token_and_expiry()
    crud.create_password_reset_token(db, admin_user, token, expires_at)

    # La recuperación de acceso de platform usa siempre el SMTP estándar de Kensar,
    # no la configuración particular del tenant gestionado.
    settings = crud.get_pos_settings(db)
    reset_link = build_reset_link(token)
    subject = f"Recuperación de acceso a {tenant.name}"
    html_body = (
        f"<p>Hola {admin_user.name or admin_user.email},</p>"
        f"<p>Desde la consola de plataforma de Metrik se solicitó recuperar el acceso "
        f"del administrador principal de <strong>{tenant.name}</strong>.</p>"
        f"<p><a href='{reset_link}' target='_blank'>Restablecer contraseña</a></p>"
        f"<p>El enlace expirará en {PASSWORD_RESET_TOKEN_TTL_SECONDS // 60} minutos.</p>"
        "<p>Si no necesitas este cambio, puedes ignorar este correo.</p>"
    )

    try:
        email_service.send_email(
            recipients=[admin_user.email],
            subject=subject,
            html_body=html_body,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return schemas.PlatformTenantRecoveryResponse(
        detail="Correo de recuperación enviado al administrador principal",
        recipient=admin_user.email,
        expires_in=PASSWORD_RESET_TOKEN_TTL_SECONDS,
    )


@router.post("/tenants/{tenant_id}/convert", response_model=schemas.PlatformTenantRead)
def convert_platform_tenant_to_active(
    tenant_id: int,
    db: Session = Depends(get_db),
    _: models.PlatformUser = Depends(require_platform_admin),
):
    tenant = crud.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    updated = crud.convert_tenant_to_active(db, tenant)
    return crud.build_platform_tenant_read(db, updated)


@router.post("/tenants/{tenant_id}/extend-trial", response_model=schemas.PlatformTenantRead)
def extend_platform_tenant_trial(
    tenant_id: int,
    payload: schemas.PlatformTenantTrialUpdateRequest,
    db: Session = Depends(get_db),
    _: models.PlatformUser = Depends(require_platform_admin),
):
    tenant = crud.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    updated = crud.extend_tenant_trial(db, tenant, extra_days=payload.extra_days)
    return crud.build_platform_tenant_read(db, updated)
