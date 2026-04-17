import hashlib
import logging
import os
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from routers import web_payments_mercadopago as mp_router
from routers.web_customers import require_web_customer_auth
from services.payments.registry import get_provider


router = APIRouter(
    prefix="/web/payments/wompi",
    tags=["web-payments-wompi"],
)

logger = logging.getLogger("kensar.wompi.webhook")


def _get_wompi_provider():
    provider = get_provider("wompi")
    if provider is None:
        raise HTTPException(status_code=503, detail="Proveedor Wompi no disponible")
    return provider


def _resolve_dotted_value(source: Any, dotted_path: str) -> Any:
    current: Any = source
    for part in (dotted_path or "").split("."):
        key = part.strip()
        if not key:
            continue
        if isinstance(current, dict):
            current = current.get(key)
            continue
        return None
    return current


def _is_valid_wompi_webhook_signature(
    *,
    body: dict[str, Any],
    secret: str,
    x_event_checksum: Optional[str],
) -> bool:
    signature = body.get("signature") if isinstance(body.get("signature"), dict) else {}
    properties = signature.get("properties") if isinstance(signature.get("properties"), list) else []
    timestamp = body.get("timestamp")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}

    if not properties or timestamp is None:
        return False

    raw = ""
    for dotted_path in properties:
        value = _resolve_dotted_value(data, str(dotted_path))
        raw += "" if value is None else str(value)
    raw += str(timestamp)
    raw += secret
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest().lower()

    body_checksum = str(signature.get("checksum") or "").strip().lower()
    header_checksum = str(x_event_checksum or "").strip().lower()
    provided = header_checksum or body_checksum
    if not provided:
        return False

    if body_checksum and body_checksum != expected:
        return False
    if header_checksum and header_checksum != expected:
        return False
    return True


def _extract_wompi_transaction_id(body: dict[str, Any]) -> str:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    tx = data.get("transaction") if isinstance(data.get("transaction"), dict) else data
    value = str(tx.get("id") or "").strip() if isinstance(tx, dict) else ""
    return value


def _build_status_response(order: models.WebOrder) -> schemas.WebWompiOrderPaymentStatusResponse:
    payments = sorted(order.payments or [], key=lambda row: row.created_at or datetime.min)
    wompi_payments = [
        payment
        for payment in payments
        if (payment.provider or "").strip().lower() == "wompi"
    ]
    last_payment = wompi_payments[-1] if wompi_payments else (payments[-1] if payments else None)

    checkout_url = None
    async_payment_url = None
    payment_method: Optional[schemas.WebWompiPaymentMethod] = None
    if last_payment:
        raw_payload = last_payment.raw_payload or {}
        payment_data = raw_payload.get("payment_method") if isinstance(raw_payload.get("payment_method"), dict) else {}
        extra = payment_data.get("extra") if isinstance(payment_data.get("extra"), dict) else {}
        checkout_url = (
            str(extra.get("checkout_url") or extra.get("url") or "").strip()
            or None
        )
        async_payment_url = (
            str(extra.get("async_payment_url") or extra.get("url") or "").strip()
            or None
        )
        method_raw = (last_payment.method or "").strip().lower()
        if method_raw in {"pse", "nequi"}:
            payment_method = method_raw  # type: ignore[assignment]

    items = [
        schemas.WebOrderItemRead(
            id=item.id,
            product_id=item.product_id,
            product_name=item.product_name_snapshot or f"Producto {item.product_id}",
            product_slug=(
                crud.resolve_product_web_slug(item.product)
                if item.product
                else crud.build_product_web_slug(
                    item.product_name_snapshot or f"producto-{item.product_id}",
                    item.product_sku_snapshot,
                )
            ),
            product_sku=item.product_sku_snapshot,
            image_url=(item.product.image_url if item.product else None),
            quantity=float(item.quantity or 0.0),
            unit_price=float(item.unit_price_snapshot or 0.0),
            line_discount_value=float(item.line_discount_value or 0.0),
            line_total=float(item.line_total or 0.0),
        )
        for item in (order.items or [])
    ]

    return schemas.WebWompiOrderPaymentStatusResponse(
        order_id=order.id,
        web_order_number=order.web_order_number,
        document_number=order.document_number,
        status=order.status,
        payment_status=order.payment_status,
        subtotal=float(order.subtotal or 0.0),
        discount_amount=float(order.discount_amount or 0.0),
        shipping_amount=float(order.shipping_amount or 0.0),
        total=float(order.total or 0.0),
        customer_name=order.customer_name,
        customer_email=order.customer_email,
        sale_id=order.sale_id,
        sale_document_number=order.sale_document_number,
        provider=last_payment.provider if last_payment else None,
        provider_reference=last_payment.provider_reference if last_payment else None,
        amount=float(last_payment.amount or 0.0) if last_payment else None,
        currency=last_payment.currency if last_payment else None,
        payment_record_status=last_payment.status if last_payment else None,
        payment_method=payment_method,
        checkout_url=checkout_url,
        async_payment_url=async_payment_url,
        items=items,
        updated_at=order.updated_at,
    )


@router.post("/checkout", response_model=schemas.WebWompiCheckoutCreateResponse)
def create_wompi_checkout(
    payload: schemas.WebWompiCheckoutCreateRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, payload.order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")

    if isinstance(payload.checkout_context, dict):
        order = mp_router._persist_checkout_context_on_order(
            db,
            order,
            checkout_context=payload.checkout_context,
        )

    provider = _get_wompi_provider()
    try:
        return provider.create_checkout(
            db,
            order,
            payment_method=payload.payment_method,
            payment_method_data=payload.payment_method_data,
            customer_email=payload.customer_email,
            customer_phone=payload.customer_phone,
            customer_full_name=payload.customer_full_name,
            acceptance_token=payload.acceptance_token,
            accept_personal_auth=payload.accept_personal_auth,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orders/{order_id}/status", response_model=schemas.WebWompiOrderPaymentStatusResponse)
def get_wompi_order_status(
    order_id: int,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")

    provider = _get_wompi_provider()
    try:
        order = provider.refresh_order_status(db, order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _build_status_response(order)


@router.post("/webhook")
async def receive_wompi_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_event_checksum: Optional[str] = Header(default=None),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    webhook_secret = (os.getenv("WOMPI_EVENTS_SECRET") or "").strip()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="WOMPI_EVENTS_SECRET no configurado")

    if not _is_valid_wompi_webhook_signature(
        body=body,
        secret=webhook_secret,
        x_event_checksum=x_event_checksum,
    ):
        raise HTTPException(status_code=401, detail="Firma de webhook Wompi inválida")

    provider = _get_wompi_provider()
    event_type = str(body.get("event") or "").strip().lower()
    data_id = _extract_wompi_transaction_id(body)
    try:
        return provider.process_webhook(
            db,
            event_type=event_type,
            data_id=data_id,
            body=body,
        )
    except ValueError as exc:
        logger.warning("Error procesando webhook Wompi: %s", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
