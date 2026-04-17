from __future__ import annotations

import logging

import models
from .registry import get_provider
from .routing import resolve_provider_for_order


logger = logging.getLogger("kensar.payments")

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

        provider_name = resolve_provider_for_order(order)
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
