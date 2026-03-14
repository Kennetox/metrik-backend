from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/stock/devices",
    tags=["stock-devices"],
)


def _require_tenant_id(db: Session, user: models.PosUser) -> int:
    tenant_id = crud.resolve_user_tenant_id(db, user)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario sin empresa asignada",
        )
    return tenant_id


@router.get("", response_model=schemas.StockDevicePage)
def list_stock_devices(
    active_only: bool = Query(default=False),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.view")),
):
    tenant_id = _require_tenant_id(db, current_user)
    items = crud.list_stock_devices(
        db,
        tenant_id=tenant_id,
        active_only=active_only,
        skip=skip,
        limit=limit,
    )
    total = crud.count_stock_devices(
        db,
        tenant_id=tenant_id,
        active_only=active_only,
    )
    return schemas.StockDevicePage(items=items, total=total, skip=skip, limit=limit)


@router.post("", response_model=schemas.StockDeviceRead, status_code=status.HTTP_201_CREATED)
def create_stock_device(
    payload: schemas.StockDeviceCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    try:
        return crud.create_stock_device(
            db,
            payload,
            tenant_id=tenant_id,
            created_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.patch("/{stock_device_id}", response_model=schemas.StockDeviceRead)
def update_stock_device(
    stock_device_id: str,
    payload: schemas.StockDeviceUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    device = crud.get_stock_device(db, stock_device_id, tenant_id=tenant_id)
    if not device:
        raise HTTPException(status_code=404, detail="Dispositivo no encontrado")
    try:
        return crud.update_stock_device(db, device, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
