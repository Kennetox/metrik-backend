from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import crud
import models
import schemas


logger = logging.getLogger("kensar.wompi")
_ORDER_REF_PATTERN = re.compile(r"^web-order:(\d+)")


class WompiPaymentProvider:
    name = "wompi"

    def map_external_status(self, status: str | None) -> str:
        normalized = (status or "").strip().upper()
        if normalized == "APPROVED":
            return "approved"
        if normalized in {"DECLINED", "ERROR"}:
            return "failed"
        if normalized == "VOIDED":
            return "cancelled"
        if normalized == "PENDING":
            return "pending"
        return "pending"

    def create_checkout(
        self,
        db,
        order: models.WebOrder,
        *,
        payment_method: str,
        payment_method_data: Optional[dict[str, Any]] = None,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_full_name: Optional[str] = None,
        acceptance_token: Optional[str] = None,
        accept_personal_auth: Optional[str] = None,
    ) -> schemas.WebWompiCheckoutCreateResponse:
        if order.status in {"cancelled", "refunded", "fulfilled"}:
            raise ValueError("La orden no admite nuevos pagos")
        if order.payment_status == "approved":
            raise ValueError("La orden ya tiene un pago aprobado")
        if not order.items:
            raise ValueError("La orden no tiene items para pagar")

        method = (payment_method or "").strip().lower()
        if method not in {"pse", "nequi"}:
            raise ValueError("Wompi solo admite 'pse' o 'nequi' en este flujo")

        public_key = self._get_required_env("WOMPI_PUBLIC_KEY")
        private_key = self._get_required_env("WOMPI_PRIVATE_KEY")
        integrity_secret = self._get_required_env("WOMPI_INTEGRITY_SECRET")

        acceptance = self._resolve_acceptance_tokens(
            public_key=public_key,
            acceptance_token=acceptance_token,
            accept_personal_auth=accept_personal_auth,
        )

        amount_in_cents = self._to_amount_in_cents(order.total)
        currency = (order.currency or "COP").strip().upper() or "COP"
        reference = f"web-order:{order.id}:{int(time.time())}"
        signature = self._build_integrity_signature(
            reference=reference,
            amount_in_cents=amount_in_cents,
            currency=currency,
            integrity_secret=integrity_secret,
        )

        email = (
            (customer_email or "").strip()
            or (order.customer_email or "").strip()
            or "guest.checkout@kensar.example.com"
        )
        full_name = (customer_full_name or order.customer_name or "Cliente Kensar").strip()
        phone = (customer_phone or order.customer_phone or "").strip()

        method_payload = self._build_payment_method_payload(
            method,
            payment_method_data=payment_method_data or {},
            order=order,
            email=email,
            full_name=full_name,
            phone=phone,
        )

        transaction_payload: dict[str, Any] = {
            "acceptance_token": acceptance["acceptance_token"],
            "accept_personal_auth": acceptance["accept_personal_auth"],
            "amount_in_cents": amount_in_cents,
            "currency": currency,
            "customer_email": email,
            "payment_method_type": method_payload["type"],
            "payment_method": method_payload,
            "reference": reference,
            "signature": signature,
            "redirect_url": self._build_redirect_url(order.id),
        }
        customer_data: dict[str, Any] = {}
        if full_name:
            customer_data["full_name"] = full_name
        if phone:
            customer_data["phone_number"] = phone
        if customer_data:
            transaction_payload["customer_data"] = customer_data

        tx_response = self._wompi_request(
            "POST",
            "/v1/transactions",
            token=private_key,
            payload=transaction_payload,
        )
        tx_data = tx_response.get("data") if isinstance(tx_response, dict) else None
        if not isinstance(tx_data, dict):
            raise ValueError("Wompi no devolvió datos de transacción")

        transaction_id = str(tx_data.get("id") or "").strip()
        if not transaction_id:
            raise ValueError("Wompi no devolvió id de transacción")

        provider_status = str(tx_data.get("status") or "").strip().upper()
        internal_status = self.map_external_status(provider_status)
        amount_from_tx = float(tx_data.get("amount_in_cents") or amount_in_cents) / 100.0

        crud.record_web_order_payment(
            db,
            order,
            schemas.WebOrderPaymentRecordRequest(
                method=method,
                amount=amount_from_tx,
                provider="wompi",
                provider_reference=transaction_id,
                status=internal_status,
                note=f"Checkout Wompi ({method}) - {provider_status or 'PENDING'}",
                raw_payload=tx_data,
            ),
            actor_user_id=None,
        )

        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id) or order
        return schemas.WebWompiCheckoutCreateResponse(
            order_id=order.id,
            provider="wompi",
            payment_method=method,
            transaction_id=transaction_id,
            status=refreshed.payment_status,
            reference=reference,
            redirect_url=transaction_payload.get("redirect_url"),
            checkout_url=self._extract_checkout_url(tx_data),
            async_payment_url=self._extract_async_payment_url(tx_data),
            acceptance_token_permalink=acceptance.get("acceptance_permalink"),
            personal_data_auth_permalink=acceptance.get("personal_auth_permalink"),
        )

    def refresh_order_status(self, db, order: models.WebOrder) -> models.WebOrder:
        target_payment = self._latest_wompi_payment(order)
        if target_payment is None:
            return order

        transaction_id = (target_payment.provider_reference or "").strip()
        if not transaction_id:
            return order

        private_key = self._get_required_env("WOMPI_PRIVATE_KEY")
        tx_response = self._wompi_request(
            "GET",
            f"/v1/transactions/{urllib_parse.quote(transaction_id)}",
            token=private_key,
        )
        tx_data = tx_response.get("data") if isinstance(tx_response, dict) else None
        if not isinstance(tx_data, dict):
            return order

        provider_status = str(tx_data.get("status") or "").strip().upper()
        internal_status = self.map_external_status(provider_status)
        method = (
            (target_payment.method or "").strip().lower()
            or str((tx_data.get("payment_method") or {}).get("type") or "").strip().lower()
            or "wompi"
        )
        amount_value = float(tx_data.get("amount_in_cents") or 0) / 100.0
        if amount_value <= 0:
            amount_value = float(target_payment.amount or order.total or 0.0)

        crud.record_web_order_payment(
            db,
            order,
            schemas.WebOrderPaymentRecordRequest(
                method=method,
                amount=amount_value,
                provider="wompi",
                provider_reference=transaction_id,
                status=internal_status,
                note=f"Sync Wompi ({provider_status or 'UNKNOWN'})",
                raw_payload=tx_data,
            ),
            actor_user_id=None,
        )
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        return refreshed or order

    def process_webhook(self, db, **kwargs: Any) -> dict[str, Any]:
        event_type = (kwargs.get("event_type") or "").strip().lower()
        data_id = str(kwargs.get("data_id") or "").strip()
        body = kwargs.get("body") if isinstance(kwargs.get("body"), dict) else {}

        if event_type and event_type != "transaction.updated":
            return {"ok": True, "ignored": event_type}

        tx_obj = self._extract_transaction_from_event(body)
        transaction_id = data_id or str(tx_obj.get("id") or "").strip()
        if not transaction_id:
            return {"ok": True, "ignored": "missing_transaction_id"}

        reference = str(tx_obj.get("reference") or "").strip()
        order_id = self._extract_order_id_from_reference(reference)

        order: Optional[models.WebOrder] = None
        if order_id is not None:
            order = db.query(models.WebOrder).filter(models.WebOrder.id == order_id).first()

        if order is None:
            payment = (
                db.query(models.WebOrderPayment)
                .filter(
                    models.WebOrderPayment.provider == "wompi",
                    models.WebOrderPayment.provider_reference == transaction_id,
                )
                .order_by(models.WebOrderPayment.id.desc())
                .first()
            )
            if payment:
                order = crud.get_backoffice_web_order(db, payment.web_order_id, tenant_id=payment.tenant_id)

        if order is None:
            return {"ok": True, "ignored": "order_not_found", "transaction_id": transaction_id}

        internal_status = self.map_external_status(str(tx_obj.get("status") or ""))
        method = str((tx_obj.get("payment_method") or {}).get("type") or "wompi").strip().lower()
        amount_value = float(tx_obj.get("amount_in_cents") or 0) / 100.0
        if amount_value <= 0:
            amount_value = float(order.total or 0.0)

        updated = crud.record_web_order_payment(
            db,
            order,
            schemas.WebOrderPaymentRecordRequest(
                method=method,
                amount=amount_value,
                provider="wompi",
                provider_reference=transaction_id,
                status=internal_status,
                note=f"Webhook Wompi ({internal_status})",
                raw_payload=tx_obj,
            ),
            actor_user_id=None,
        )
        return {"ok": True, "order_id": updated.id, "transaction_id": transaction_id}

    def _resolve_acceptance_tokens(
        self,
        *,
        public_key: str,
        acceptance_token: Optional[str],
        accept_personal_auth: Optional[str],
    ) -> dict[str, Optional[str]]:
        at = (acceptance_token or "").strip()
        apa = (accept_personal_auth or "").strip()
        acceptance_permalink: Optional[str] = None
        personal_auth_permalink: Optional[str] = None
        if at and apa:
            return {
                "acceptance_token": at,
                "accept_personal_auth": apa,
                "acceptance_permalink": None,
                "personal_auth_permalink": None,
            }

        merchant = self._wompi_request("GET", f"/v1/merchants/{urllib_parse.quote(public_key)}")
        data = merchant.get("data") if isinstance(merchant, dict) else None
        if not isinstance(data, dict):
            raise ValueError("No fue posible obtener tokens de aceptación de Wompi")

        presigned_acceptance = data.get("presigned_acceptance") if isinstance(data.get("presigned_acceptance"), dict) else {}
        personal_auth = data.get("presigned_personal_data_auth") if isinstance(data.get("presigned_personal_data_auth"), dict) else {}

        at = at or str(presigned_acceptance.get("acceptance_token") or "").strip()
        apa = apa or str(personal_auth.get("acceptance_token") or "").strip()
        acceptance_permalink = str(presigned_acceptance.get("permalink") or "").strip() or None
        personal_auth_permalink = str(personal_auth.get("permalink") or "").strip() or None
        if not at or not apa:
            raise ValueError("No se pudieron resolver tokens de aceptación de Wompi")

        return {
            "acceptance_token": at,
            "accept_personal_auth": apa,
            "acceptance_permalink": acceptance_permalink,
            "personal_auth_permalink": personal_auth_permalink,
        }

    def _build_payment_method_payload(
        self,
        method: str,
        *,
        payment_method_data: dict[str, Any],
        order: models.WebOrder,
        email: str,
        full_name: str,
        phone: str,
    ) -> dict[str, Any]:
        if method == "nequi":
            phone_number = str(payment_method_data.get("phone_number") or phone or "").strip()
            if not phone_number:
                raise ValueError("Nequi requiere phone_number")
            return {
                "type": "NEQUI",
                "phone_number": phone_number,
            }

        user_type = int(payment_method_data.get("user_type", 0))
        legal_id_type = str(payment_method_data.get("user_legal_id_type") or "CC").strip().upper()
        legal_id = str(payment_method_data.get("user_legal_id") or order.customer_tax_id or "").strip()
        institution_code = str(payment_method_data.get("financial_institution_code") or "").strip()
        if not legal_id:
            raise ValueError("PSE requiere user_legal_id")
        if not institution_code:
            raise ValueError("PSE requiere financial_institution_code")

        payment_description = str(
            payment_method_data.get("payment_description")
            or f"Pago orden {order.document_number or order.id}"
        ).strip()

        payload = {
            "type": "PSE",
            "user_type": user_type,
            "user_legal_id_type": legal_id_type,
            "user_legal_id": legal_id,
            "financial_institution_code": institution_code,
            "payment_description": payment_description[:64],
        }

        for ref_field in ("reference_one", "reference_two", "reference_three"):
            value = str(payment_method_data.get(ref_field) or "").strip()
            if value:
                payload[ref_field] = value

        if full_name:
            payload["full_name"] = full_name
        if email:
            payload["email"] = email
        if phone:
            payload["phone_number"] = phone
        return payload

    def _build_redirect_url(self, order_id: int) -> Optional[str]:
        base = (os.getenv("WEB_CHECKOUT_BASE_URL") or "").strip().rstrip("/")
        if not base:
            return None
        return f"{base}/pago/resultado?orderId={urllib_parse.quote(str(order_id))}&payment=pending&provider=wompi"

    def _build_integrity_signature(
        self,
        *,
        reference: str,
        amount_in_cents: int,
        currency: str,
        integrity_secret: str,
    ) -> str:
        raw = f"{reference}{amount_in_cents}{currency}{integrity_secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _to_amount_in_cents(self, amount: float | int | None) -> int:
        value = float(amount or 0.0)
        cents = int(round(value * 100))
        if cents <= 0:
            raise ValueError("El monto de la orden debe ser mayor que cero")
        return cents

    def _latest_wompi_payment(self, order: models.WebOrder) -> Optional[models.WebOrderPayment]:
        wompi_payments = [
            payment
            for payment in (order.payments or [])
            if (payment.provider or "").strip().lower() == "wompi"
        ]
        if not wompi_payments:
            return None
        return sorted(wompi_payments, key=lambda row: row.created_at or datetime.min)[-1]

    def _extract_checkout_url(self, tx_data: dict[str, Any]) -> Optional[str]:
        payment_method = tx_data.get("payment_method") if isinstance(tx_data.get("payment_method"), dict) else {}
        extra = payment_method.get("extra") if isinstance(payment_method.get("extra"), dict) else {}
        for key in ("url", "checkout_url", "payment_url", "async_payment_url"):
            value = str(extra.get(key) or "").strip()
            if value:
                return value
        return None

    def _extract_async_payment_url(self, tx_data: dict[str, Any]) -> Optional[str]:
        payment_method = tx_data.get("payment_method") if isinstance(tx_data.get("payment_method"), dict) else {}
        extra = payment_method.get("extra") if isinstance(payment_method.get("extra"), dict) else {}
        value = str(extra.get("async_payment_url") or extra.get("url") or "").strip()
        return value or None

    def _extract_order_id_from_reference(self, reference: str) -> Optional[int]:
        match = _ORDER_REF_PATTERN.match((reference or "").strip())
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    def _extract_transaction_from_event(self, body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        if isinstance(data.get("transaction"), dict):
            return data.get("transaction")
        return data if isinstance(data, dict) else {}

    def _get_required_env(self, name: str) -> str:
        value = (os.getenv(name) or "").strip()
        if not value:
            raise ValueError(f"{name} no está configurado")
        return value

    def _wompi_base_url(self) -> str:
        return (os.getenv("WOMPI_BASE_URL") or "https://sandbox.wompi.co").strip().rstrip("/")

    def _wompi_request(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_path = path if path.startswith("/") else f"/{path}"
        url = f"{self._wompi_base_url()}{normalized_path}"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        body_bytes: Optional[bytes] = None
        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")

        request = urllib_request.Request(url=url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urllib_request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib_error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode("utf-8")
                parsed = json.loads(raw) if raw else {}
                detail = str(parsed.get("error") or parsed.get("message") or "")
            except Exception:
                detail = ""
            logger.warning(
                "Wompi HTTP error | method=%s path=%s status=%s detail=%s",
                method.upper(),
                normalized_path,
                exc.code,
                detail or "-",
            )
            raise ValueError(detail or f"Wompi HTTP {exc.code}") from exc
        except urllib_error.URLError as exc:
            raise ValueError("No se pudo conectar con Wompi") from exc
