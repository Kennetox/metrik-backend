from __future__ import annotations

from typing import Any, Optional
from urllib import parse as urllib_parse

import models
from routers import web_payments_mercadopago as mp_router


class MercadoPagoPaymentProvider:
    name = "mercadopago"

    def map_external_status(self, status: str | None) -> str:
        return mp_router._normalize_payment_status(status)

    def create_checkout(
        self,
        db,
        order: models.WebOrder,
        *,
        payer_input: Optional[Any] = None,
        order_access_token: Optional[str] = None,
    ) -> Any:
        return mp_router._create_checkout_preference_for_order(
            order,
            payer_input=payer_input,
            order_access_token=order_access_token,
        )

    def refresh_order_status(self, db, order: models.WebOrder) -> models.WebOrder:
        return mp_router._refresh_order_payment_status_from_provider(db, order)

    def process_webhook(
        self,
        db,
        *,
        event_type: str,
        data_id: str,
    ) -> dict[str, Any]:
        normalized_event = (event_type or "").strip().lower()
        if normalized_event in {"payment", "payments"}:
            payment_id = (data_id or "").strip()
            if not payment_id:
                raise ValueError("Notificación de pago sin data.id")
            updated = mp_router._process_payment_notification(db, payment_id)
            return {"ok": True, "order_id": updated.id, "status": updated.status}

        if normalized_event in {"merchant_order", "order"}:
            merchant_order_id = (data_id or "").strip()
            if not merchant_order_id:
                return {"ok": True, "ignored": "merchant_order sin data.id"}
            token = mp_router._get_mercadopago_access_token()
            order_data = mp_router._mercadopago_request(
                "GET",
                f"/merchant_orders/{urllib_parse.quote(str(merchant_order_id))}",
                access_token=token,
            )
            processed = 0
            for payment in (order_data.get("payments") or []):
                payment_id = str(payment.get("id") or "").strip()
                if not payment_id:
                    continue
                mp_router._process_payment_notification(db, payment_id)
                processed += 1
            return {"ok": True, "processed_payments": processed}

        return {"ok": True, "ignored": normalized_event or "unknown"}
