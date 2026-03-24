import os
from typing import Optional, Sequence
from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import models
import crud
from database import get_db
from security import verify_access_token, WEB_INACTIVITY_TIMEOUT_SECONDS
from services import permissions


def require_pos_auth(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> models.PosUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token POS requerido",
        )

    token = authorization.split(" ", 1)[1]
    try:
        payload = verify_access_token(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )

    user = db.query(models.PosUser).filter(models.PosUser.id == user_id).first()
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no autorizado",
        )
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario sin empresa asignada",
        )

    session = crud.get_session_by_token(db, token)
    if not session or session.revoked_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión inválida o cerrada",
        )

    now = datetime.utcnow()
    if session.expires_at < now:
        session.revoked_at = now
        session.revoked_reason = "expired"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión expirada",
        )

    if (
        session.session_type == "web"
        and session.last_seen_at
        and now - session.last_seen_at > timedelta(seconds=WEB_INACTIVITY_TIMEOUT_SECONDS)
    ):
        session.revoked_at = now
        session.revoked_reason = "inactive"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión expirada por inactividad",
        )

    session.last_seen_at = now
    db.commit()

    return user


def require_role(
    allowed_roles: Sequence[str],
):
    def _role_checker(user: models.PosUser = Depends(require_pos_auth)):
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No autorizado para esta acción",
            )
        return user

    return _role_checker


def get_current_active_user(
    user: models.PosUser = Depends(require_pos_auth),
) -> models.PosUser:
    """Returns the authenticated POS user (must be active)."""
    return user


def get_current_tenant_id(
    user: models.PosUser = Depends(require_pos_auth),
    db: Session = Depends(get_db),
) -> int:
    tenant_id = crud.resolve_user_tenant_id(db, user)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario sin empresa asignada",
        )
    return tenant_id


def require_permission(permission_id: str):
    def _permission_checker(
        user: models.PosUser = Depends(require_pos_auth),
        db: Session = Depends(get_db),
    ):
        tenant_id = crud.resolve_user_tenant_id(db, user)
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Usuario sin empresa asignada",
            )
        matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
        if not permissions.role_has_permission(matrix, permission_id, user.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No autorizado para esta acción",
            )
        return user

    return _permission_checker


def require_any_permission(*permission_ids: str):
    def _permission_checker(
        user: models.PosUser = Depends(require_pos_auth),
        db: Session = Depends(get_db),
    ):
        tenant_id = crud.resolve_user_tenant_id(db, user)
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Usuario sin empresa asignada",
            )
        matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
        if not any(
            permissions.role_has_permission(matrix, permission_id, user.role)
            for permission_id in permission_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No autorizado para esta acción",
            )
        return user

    return _permission_checker


def require_module_access(module_id: str):
    def _module_checker(
        user: models.PosUser = Depends(require_pos_auth),
        db: Session = Depends(get_db),
    ):
        tenant_id = crud.resolve_user_tenant_id(db, user)
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Usuario sin empresa asignada",
            )
        tenant = crud.get_tenant(db, tenant_id)
        if not crud.can_user_access_tenant_module(tenant, module_id, user=user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No autorizado para este módulo",
            )
        return user

    return _module_checker


def require_platform_auth(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> models.PlatformUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de plataforma requerido",
        )
    token = authorization.split(" ", 1)[1]
    try:
        payload = verify_access_token(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    if payload.get("kind") != "platform":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de plataforma inválido",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )
    user = crud.get_platform_user(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario de plataforma no autorizado",
        )
    return user


def require_platform_admin(
    user: models.PlatformUser = Depends(require_platform_auth),
) -> models.PlatformUser:
    configured = {
        item.strip().lower()
        for item in os.getenv("PLATFORM_ADMIN_EMAILS", "").split(",")
        if item.strip()
    }
    user_email = (user.email or "").strip().lower()
    if configured and user_email not in configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para administración de plataforma",
        )
    return user
