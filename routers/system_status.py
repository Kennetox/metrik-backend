import os
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from database import get_db


router = APIRouter(tags=["system-status"])


class SystemStatusUpdate(BaseModel):
    state: Literal["healthy", "maintenance"]
    message: str | None = Field(default=None, max_length=255)
    updated_by: str | None = Field(default=None, max_length=120)


def _public_status(row: models.SystemStatus | None) -> dict[str, object]:
    if not row:
        return {
            "state": "healthy",
            "maintenance": False,
            "message": None,
            "updated_at": None,
            "updated_by": None,
        }
    return {
        "state": row.state,
        "maintenance": row.state == "maintenance",
        "message": row.message,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "updated_by": row.updated_by,
    }


@router.get("/system-status")
def get_system_status(db: Session = Depends(get_db)):
    row = db.query(models.SystemStatus).filter(models.SystemStatus.id == 1).first()
    return _public_status(row)


@router.post("/ops/system-status")
def update_system_status(
    payload: SystemStatusUpdate,
    db: Session = Depends(get_db),
    deployment_status_token: str | None = Header(
        default=None,
        alias="X-Deployment-Status-Token",
    ),
):
    expected_token = (os.getenv("DEPLOYMENT_STATUS_TOKEN") or "").strip()
    if not expected_token or deployment_status_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token de despliegue inválido",
        )

    row = db.query(models.SystemStatus).filter(models.SystemStatus.id == 1).first()
    if row is None:
        row = models.SystemStatus(id=1)
        db.add(row)
    row.state = payload.state
    row.message = payload.message or (
        "Estamos actualizando Metrik. Algunas funciones pueden no estar disponibles."
        if payload.state == "maintenance"
        else None
    )
    row.updated_by = payload.updated_by or "deployment"
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)

    # /readyz has a short in-process cache. Clear it immediately so clients
    # can observe maintenance before Render replaces the running instance.
    try:
        from main import _READYZ_CACHE

        _READYZ_CACHE.pop("readyz", None)
    except (ImportError, AttributeError):
        pass

    return _public_status(row)
