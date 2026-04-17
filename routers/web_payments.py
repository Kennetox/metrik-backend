from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from routers import web_payments_mercadopago as mp_router
from routers import web_payments_wompi as wompi_router
from routers.web_customers import require_web_customer_auth
from services.payments.registry import get_provider
from services.payments.routing import resolve_provider_for_method, resolve_provider_for_order


router = APIRouter(
    prefix="/web/payments",
    tags=["web-payments"],
)


def _get_provider_or_503(provider_name: str):
    provider = get_provider(provider_name)
    if provider is None:
        raise HTTPException(status_code=503, detail=f"Proveedor de pago no disponible: {provider_name}")
    return provider


@router.post(
    "/checkout",
    response_model=schemas.WebMercadoPagoCheckoutCreateResponse | schemas.WebWompiCheckoutCreateResponse,
)
def create_checkout(
    payload: schemas.WebCheckoutCreateRequest,
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

    try:
        provider_name = resolve_provider_for_method(payload.payment_method)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider = _get_provider_or_503(provider_name)

    if provider_name == "mercadopago":
        return provider.create_checkout(
            db,
            order,
            payer_input=payload.payer,
            payment_method=payload.payment_method,
        )

    if provider_name == "wompi":
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

    raise HTTPException(status_code=400, detail=f"Proveedor no soportado: {provider_name}")


@router.get(
    "/orders/{order_id}/status",
    response_model=schemas.WebMercadoPagoOrderPaymentStatusResponse | schemas.WebWompiOrderPaymentStatusResponse,
)
def get_checkout_order_status(
    order_id: int,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")

    provider_name = resolve_provider_for_order(order)
    provider = _get_provider_or_503(provider_name)
    try:
        order = provider.refresh_order_status(db, order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_after_refresh = resolve_provider_for_order(order)
    if resolved_after_refresh == "wompi":
        return wompi_router._build_status_response(order)
    return mp_router._build_status_response(order)
