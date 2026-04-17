from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

import models
from .registry import get_provider


logger = logging.getLogger("kensar.payments")


def _clean_provider(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip().lower()
    return normalized or None


def _resolve_order_provider(order: models.WebOrder) -> Optional[str]:
    payments = sorted(order.payments or [], key=lambda row: row.created_at or datetime.min)
    if payments:
        last_provider = _clean_provider(payments[-1].provider)
        if last_provider:
            return last_provider

    default_provider = _clean_provider(os.getenv("WEB_DEFAULT_PAYMENT_PROVIDER"))
    if default_provider:
        return default_provider
    return "mercadopago"


def refresh_backoffice_order_payment_statuses(
    db,
    orders: list[models.WebOrder],
) -> list[models.WebOrder]:
    if not orders:
        return []

    refreshed_orders: list[models.WebOrder] = []
    for order in orders:
        if not order:
            continue
        if order.status in {"cancelled", "refunded"}:
            refreshed_orders.append(order)
            continue

        provider_name = _resolve_order_provider(order)
        provider = get_provider(provider_name)
        if provider is None:
            logger.warning(
                "Payment provider unavailable for order refresh | order_id=%s provider=%s",
                getattr(order, "id", None),
                provider_name,
            )
            refreshed_orders.append(order)
            continue

        try:
            refreshed = provider.refresh_order_status(db, order)
            refreshed_orders.append(refreshed or order)
        except Exception:
            logger.exception(
                "Payment provider refresh failed | order_id=%s provider=%s",
                getattr(order, "id", None),
                provider_name,
            )
            refreshed_orders.append(order)

    return refreshed_orders
