from datetime import datetime
from typing import List, Optional
import unicodedata
import os

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

import crud, models, schemas
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/separated-orders",
    tags=["separated-orders"],
)

FREE_SALE_NAME_FRAGMENT = "venta libre"
FREE_SALE_REASON_LABEL = "motivo venta libre"
FREE_SALE_REASON_REQUIRED = (
    os.getenv("FREE_SALE_REASON_REQUIRED", "true").strip().lower()
    not in {"0", "false", "no", "off"}
)


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()


def _sale_contains_free_sale(separated_in: schemas.SeparatedOrderCreate) -> bool:
    for item in separated_in.items or []:
        name = _normalize_text(getattr(item, "product_name", ""))
        sku = _normalize_text(getattr(item, "product_sku", ""))
        if FREE_SALE_NAME_FRAGMENT in name or "venta-libre" in sku or "venta libre" in sku:
            return True
    return False


def _has_required_free_sale_reason(notes: Optional[str]) -> bool:
    normalized_notes = _normalize_text(notes)
    if not normalized_notes:
        return False
    label_index = normalized_notes.find(FREE_SALE_REASON_LABEL)
    if label_index < 0:
        return False
    tail = normalized_notes[label_index + len(FREE_SALE_REASON_LABEL) :].strip(" :\n\t\r-")
    return bool(tail)


@router.post(
    "",
    response_model=schemas.SeparatedOrderRead,
    status_code=status.HTTP_201_CREATED,
)
def create_separated_order(
    separated_in: schemas.SeparatedOrderCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders")
    ),
):
    if (
        FREE_SALE_REASON_REQUIRED
        and _sale_contains_free_sale(separated_in)
        and not _has_required_free_sale_reason(separated_in.notes)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "La nota debe incluir el motivo de venta libre cuando se use este producto."
            ),
        )
    try:
        sale = crud.create_sale(db, separated_in, created_by_user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No se pudo crear la venta") from exc

    try:
        order = crud.create_separated_order(db, sale, separated_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return order


@router.get("", response_model=List[schemas.SeparatedOrderRead])
def list_separated_orders(
    barcode: Optional[str] = Query(default=None),
    sale_number: Optional[int] = Query(default=None),
    customer: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    date_from: Optional[datetime] = Query(default=None),
    date_to: Optional[datetime] = Query(default=None),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    return crud.list_separated_orders(
        db,
        skip=skip,
        limit=limit,
        barcode=barcode,
        sale_number=sale_number,
        customer=customer,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        tenant_id=tenant_id,
    )


@router.get("/{order_id}", response_model=schemas.SeparatedOrderRead)
def get_separated_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    order = crud.get_separated_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Separado no encontrado")
    return order


@router.post(
    "/{order_id}/payments",
    response_model=schemas.SeparatedOrderRead,
)
def add_separated_payment(
    order_id: int,
    payment_in: schemas.SeparatedOrderPaymentCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    order = crud.get_separated_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Separado no encontrado")
    try:
        updated = crud.add_separated_order_payment(db, order, payment_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.post(
    "/{order_id}/payments/{payment_id}/void",
    response_model=schemas.SeparatedOrderRead,
)
def void_separated_payment(
    order_id: int,
    payment_id: int,
    payload: schemas.SeparatedOrderPaymentVoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders.void_payment")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    order = crud.get_separated_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Separado no encontrado")
    payment = crud.get_separated_order_payment(db, payment_id)
    if not payment or payment.separated_order_id != order.id:
        raise HTTPException(status_code=404, detail="Abono no encontrado")
    try:
        updated = crud.void_separated_order_payment(
            db, payment, current_user, payload.reason, payload.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.patch(
    "/{order_id}/complete",
    response_model=schemas.SeparatedOrderRead,
)
def complete_separated_order(
    order_id: int,
    payload: Optional[schemas.SeparatedOrderStatusUpdate] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    order = crud.get_separated_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Separado no encontrado")
    note = payload.notes if payload else None
    try:
        updated = crud.complete_separated_order(db, order, note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.patch(
    "/{order_id}/cancel",
    response_model=schemas.SeparatedOrderRead,
)
def cancel_separated_order(
    order_id: int,
    payload: Optional[schemas.SeparatedOrderStatusUpdate] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("documents.separated_orders")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    order = crud.get_separated_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Separado no encontrado")
    note = payload.notes if payload else None
    try:
        updated = crud.cancel_separated_order(db, order, note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated
