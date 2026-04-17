from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

import models


_METHOD_TO_PROVIDER_MAP: dict[str, str] = {
    "card": "mercadopago",
    "pse": "wompi",
    "nequi": "wompi",
    "wompi": "wompi",
}


def _normalize_value(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def resolve_provider_for_method(method: Optional[str], context: Optional[dict[str, Any]] = None) -> str:
    _ = context
    normalized_method = _normalize_value(method)
    provider = _METHOD_TO_PROVIDER_MAP.get(normalized_method)
    if not provider:
        raise ValueError(f"Método de pago no soportado: {method or '-'}")
    return provider


def resolve_provider_for_order(order: models.WebOrder, context: Optional[dict[str, Any]] = None) -> str:
    _ = context
    payments = sorted(order.payments or [], key=lambda row: row.created_at or datetime.min)
    if payments:
        latest_payment = payments[-1]
        provider = _normalize_value(latest_payment.provider)
        if provider:
            return provider
        method = _normalize_value(latest_payment.method)
        if method:
            try:
                return resolve_provider_for_method(method)
            except ValueError:
                pass

    default_provider = _normalize_value(os.getenv("WEB_DEFAULT_PAYMENT_PROVIDER"))
    if default_provider:
        return default_provider
    return "mercadopago"
