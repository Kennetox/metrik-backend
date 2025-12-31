from typing import Optional, Sequence

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import models
import crud
from database import get_db
from security import verify_access_token
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


def require_permission(permission_id: str):
    def _permission_checker(
        user: models.PosUser = Depends(require_pos_auth),
        db: Session = Depends(get_db),
    ):
        matrix = crud.get_role_permissions(db)
        if not permissions.role_has_permission(matrix, permission_id, user.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No autorizado para esta acción",
            )
        return user

    return _permission_checker
