from datetime import datetime, timedelta
import functools
import inspect
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
TECH_SERVICE_REASON_LABEL = "motivo servicio tecnico"
BALANCE_TOPUP_REASON_LABEL = "motivo abono de saldo"
TECH_SERVICE_SKU = "138"
BALANCE_TOPUP_SKU = "1087"
FREE_SALE_REASON_REQUIRED = (
    os.getenv("FREE_SALE_REASON_REQUIRED", "true").strip().lower()
    not in {"0", "false", "no", "off"}
)
_SEPARATED_ORDERS_CACHE: dict[str, tuple[datetime, object]] = {}


def _separated_cache_key(name: str, **params: object) -> str:
    parts = [name]
    for key in sorted(params.keys()):
        value = params[key]
        if isinstance(value, datetime):
            rendered = value.isoformat()
        else:
            rendered = repr(value)
        parts.append(f"{key}={rendered}")
    return "|".join(parts)


def _get_separated_cache(key: str):
    cache_entry = _SEPARATED_ORDERS_CACHE.get(key)
    if not cache_entry:
        return None
    expires_at, value = cache_entry
    if expires_at <= datetime.utcnow():
        _SEPARATED_ORDERS_CACHE.pop(key, None)
        return None
    return value


def _set_separated_cache(key: str, value: object, ttl_seconds: int) -> None:
    if len(_SEPARATED_ORDERS_CACHE) >= 512:
        expired_keys = [
            cache_key
            for cache_key, (expires_at, _) in _SEPARATED_ORDERS_CACHE.items()
            if expires_at <= datetime.utcnow()
        ]
        for cache_key in expired_keys:
            _SEPARATED_ORDERS_CACHE.pop(cache_key, None)
        while len(_SEPARATED_ORDERS_CACHE) >= 512:
            oldest_key = min(
                _SEPARATED_ORDERS_CACHE,
                key=lambda cache_key: _SEPARATED_ORDERS_CACHE[cache_key][0],
            )
            _SEPARATED_ORDERS_CACHE.pop(oldest_key, None)
    _SEPARATED_ORDERS_CACHE[key] = (datetime.utcnow() + timedelta(seconds=ttl_seconds), value)


def _separated_cached(ttl_seconds: int, fallback_factory):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound_args = inspect.signature(func).bind_partial(*args, **kwargs)
            bound_args.apply_defaults()
            cache_params = {
                key: value
                for key, value in bound_args.arguments.items()
                if key not in {"db", "current_user"}
            }
            current_user = bound_args.arguments.get("current_user")
            cache_params["auth_scope"] = (
                getattr(current_user, "tenant_id", None),
                getattr(current_user, "id", None),
                getattr(current_user, "role", None),
            )
            cache_key = _separated_cache_key(func.__name__, **cache_params)
            cached = _get_separated_cache(cache_key)
            if cached is not None:
                return cached
            result = func(*args, **kwargs)
            _set_separated_cache(cache_key, result, ttl_seconds)
            return result

        return wrapper

    return decorator


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()


def _sale_contains_required_reason_product(separated_in: schemas.SeparatedOrderCreate) -> bool:
    for item in separated_in.items or []:
        name = _normalize_text(getattr(item, "product_name", ""))
        sku = _normalize_text(getattr(item, "product_sku", ""))
        if (
            FREE_SALE_NAME_FRAGMENT in name
            or "venta-libre" in sku
            or "venta libre" in sku
            or sku == TECH_SERVICE_SKU
            or sku == BALANCE_TOPUP_SKU
        ):
            return True
    return False


def _has_required_sale_reason(notes: Optional[str]) -> bool:
    normalized_notes = _normalize_text(notes)
    if not normalized_notes:
        return False
    labels = [
        FREE_SALE_REASON_LABEL,
        TECH_SERVICE_REASON_LABEL,
        BALANCE_TOPUP_REASON_LABEL,
    ]
    for label in labels:
        label_index = normalized_notes.find(label)
        if label_index < 0:
            continue
        tail = normalized_notes[label_index + len(label) :].strip(" :\n\t\r-")
        if tail:
            return True
    return False


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
        and _sale_contains_required_reason_product(separated_in)
        and not _has_required_sale_reason(separated_in.notes)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "La nota debe incluir el motivo correspondiente cuando se use este producto."
            ),
        )
    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        sale = crud.create_sale(
            db,
            separated_in,
            created_by_user_id=current_user.id,
            tenant_id=tenant_id,
            commit=False,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo crear la venta") from exc

    try:
        order = crud.create_separated_order(db, sale, separated_in)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo crear el separado") from exc
    _SEPARATED_ORDERS_CACHE.clear()
    return order


@router.get("", response_model=List[schemas.SeparatedOrderRead])
@_separated_cached(ttl_seconds=10, fallback_factory=list)
def list_separated_orders(
    barcode: Optional[str] = Query(default=None),
    sale_number: Optional[int] = Query(default=None),
    customer: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    date_from: Optional[datetime] = Query(default=None),
    date_to: Optional[datetime] = Query(default=None),
    paid_from: Optional[datetime] = Query(default=None),
    paid_to: Optional[datetime] = Query(default=None),
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
        paid_from=paid_from,
        paid_to=paid_to,
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
        updated = crud.add_separated_order_payment(
            db,
            order,
            payment_in,
            user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _SEPARATED_ORDERS_CACHE.clear()
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
    _SEPARATED_ORDERS_CACHE.clear()
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
    _SEPARATED_ORDERS_CACHE.clear()
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
        updated = crud.resolve_separated_order(
            db,
            order,
            schemas.SeparatedOrderResolveRequest(
                action="cancel",
                reason=(note or "Cancelación administrativa").strip(),
                notes=note,
                refund_amount=0,
                remainder_disposition="retained",
            ),
            current_user,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _SEPARATED_ORDERS_CACHE.clear()
    return updated


@router.post(
    "/{order_id}/resolve",
    response_model=schemas.SeparatedOrderRead,
)
def resolve_separated_order(
    order_id: int,
    payload: schemas.SeparatedOrderResolveRequest,
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
        updated = crud.resolve_separated_order(db, order, payload, current_user)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    _SEPARATED_ORDERS_CACHE.clear()
    return updated
