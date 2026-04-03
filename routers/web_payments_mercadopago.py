import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from routers.web_customers import require_web_customer_auth
from services import email as email_service
from services import ticket_renderer
from security import create_access_token, verify_access_token


router = APIRouter(
    prefix="/web/payments/mercadopago",
    tags=["web-payments-mercadopago"],
)

logger = logging.getLogger("kensar.mercadopago")
WEB_GUEST_ORDER_TOKEN_TTL_SECONDS = int(
    os.getenv("WEB_GUEST_ORDER_TOKEN_TTL", 60 * 60 * 24 * 7)
)
BOGOTA_TZ = ZoneInfo("America/Bogota")


def _get_mercadopago_access_token() -> str:
    token = (os.getenv("MERCADOPAGO_ACCESS_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="Mercado Pago no está configurado")
    return token


def _get_mercadopago_public_key() -> Optional[str]:
    value = (os.getenv("MERCADOPAGO_PUBLIC_KEY") or "").strip()
    return value or None


def _get_checkout_base_url() -> Optional[str]:
    raw = (os.getenv("WEB_CHECKOUT_BASE_URL") or "").strip()
    if not raw:
        return None
    return raw.rstrip("/")


def _is_valid_public_back_url(url: str) -> bool:
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return False
    return True


def _get_webhook_url() -> str:
    return (os.getenv("MERCADOPAGO_WEBHOOK_URL") or "https://api.metrikpos.com/web/payments/mercadopago/webhook").strip()


def _is_guest_order_reuse_enabled() -> bool:
    raw = (os.getenv("WEB_GUEST_REUSE_PENDING_ORDER") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _mercadopago_request(
    method: str,
    path: str,
    *,
    access_token: str,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    path = path if path.startswith("/") else f"/{path}"
    url = f"https://api.mercadopago.com{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    body_bytes: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(payload).encode("utf-8")

    request = urllib_request.Request(url=url, data=body_bytes, headers=headers, method=method.upper())
    try:
        with urllib_request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib_error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {}
        detail = parsed.get("message") or parsed.get("error") or f"Mercado Pago HTTP {exc.code}"
        raise HTTPException(status_code=400, detail=f"Mercado Pago: {detail}") from exc
    except urllib_error.URLError as exc:
        raise HTTPException(status_code=502, detail="No se pudo conectar con Mercado Pago") from exc


def _extract_signature_parts(signature_header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in (signature_header or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _is_valid_webhook_signature(
    *,
    secret: str,
    signature_header: str,
    request_id: str,
    data_id: str,
) -> bool:
    parts = _extract_signature_parts(signature_header)
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    digest = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, v1)


def _normalize_payment_status(value: Optional[str]) -> schemas.WebOrderPaymentStatus:
    normalized = (value or "").strip().lower()
    if normalized == "approved":
        return "approved"
    if normalized in {"rejected"}:
        return "failed"
    if normalized in {"cancelled"}:
        return "cancelled"
    if normalized in {"refunded", "charged_back"}:
        return "refunded"
    return "pending"


def _extract_order_id(payment_payload: dict[str, Any]) -> Optional[int]:
    metadata = payment_payload.get("metadata")
    if isinstance(metadata, dict):
        raw = metadata.get("order_id")
        if raw is not None:
            try:
                return int(str(raw))
            except Exception:
                pass

    external_reference = str(payment_payload.get("external_reference") or "").strip()
    if external_reference.startswith("web-order:"):
        raw = external_reference.split(":", 1)[1]
        try:
            return int(raw)
        except Exception:
            return None
    if external_reference.isdigit():
        return int(external_reference)
    return None


def _extract_tenant_id(payment_payload: dict[str, Any]) -> Optional[int]:
    metadata = payment_payload.get("metadata")
    if isinstance(metadata, dict):
        raw = metadata.get("tenant_id")
        if raw is None:
            return None
        try:
            return int(str(raw))
        except Exception:
            return None
    return None


def _build_checkout_items(order: models.WebOrder) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in order.items or []:
        quantity = max(1, int(round(float(item.quantity or 0.0))))
        unit_price = round(float(item.unit_price_snapshot or 0.0), 2)
        if unit_price <= 0:
            continue
        items.append(
            {
                "id": str(item.product_id),
                "title": item.product_name_snapshot or f"Producto {item.product_id}",
                "quantity": quantity,
                "currency_id": order.currency or "COP",
                "unit_price": unit_price,
            }
        )

    if items:
        return items

    total = round(float(order.total or 0.0), 2)
    return [
        {
            "id": str(order.id),
            "title": f"Orden {order.document_number or order.id}",
            "quantity": 1,
            "currency_id": order.currency or "COP",
            "unit_price": total,
        }
    ]


def _build_status_response(order: models.WebOrder) -> schemas.WebMercadoPagoOrderPaymentStatusResponse:
    payments = sorted(order.payments or [], key=lambda row: row.created_at or datetime.min)
    last_payment = payments[-1] if payments else None
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
    return schemas.WebMercadoPagoOrderPaymentStatusResponse(
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
        items=items,
        updated_at=order.updated_at,
    )


def _format_money_cop(value: float | int | None) -> str:
    amount = float(value or 0.0)
    formatted = f"{amount:,.2f}"
    formatted = formatted.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"${formatted} COP"


def _format_datetime_bogota(dt_value: Optional[datetime]) -> str:
    if not dt_value:
        return "-"
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(BOGOTA_TZ).strftime("%d/%m/%Y %H:%M")


def _smtp_settings_dict(settings: models.PosSettings) -> dict[str, Any]:
    return {
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_user": settings.smtp_user,
        "smtp_password": settings.smtp_password,
        "smtp_use_tls": settings.smtp_use_tls,
        "email_from": settings.email_from,
        "company_name": settings.company_name,
    }


def _payment_method_labels_by_slug(db: Session, tenant_id: Optional[int]) -> dict[str, str]:
    payment_methods = crud.list_payment_methods(db, tenant_id=tenant_id)
    return {
        (method.slug or "").strip().lower(): method.name.strip()
        for method in payment_methods
        if (method.slug or "").strip() and (method.name or "").strip()
    }


def _collect_internal_notification_recipients(settings: models.PosSettings) -> list[str]:
    values: list[str] = []
    recipients = settings.closure_email_recipients or []
    if isinstance(recipients, list):
        values.extend(str(item).strip() for item in recipients if str(item).strip())
    if settings.contact_email and str(settings.contact_email).strip():
        values.append(str(settings.contact_email).strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for email in values:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(email)
    return deduped


def _build_web_order_approved_customer_html(
    order: models.WebOrder,
    *,
    sale: Optional[models.Sale] = None,
) -> str:
    customer_name = (order.customer_name or "Cliente").strip()
    order_number = order.document_number or f"OW-{order.id:06d}"
    sale_number = sale.document_number if sale else None
    lines = []
    for item in order.items or []:
        quantity = float(item.quantity or 0.0)
        line_total = float(item.line_total or 0.0)
        lines.append(
            "<tr>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb;'>{escape(item.product_name_snapshot or 'Producto')}</td>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{quantity:g}</td>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(line_total)}</td>"
            "</tr>"
        )
    if not lines:
        lines.append(
            "<tr><td colspan='3' style='padding:8px; border:1px solid #e5e7eb; color:#6b7280;'>Sin items registrados</td></tr>"
        )

    return (
        "<div style='font-family:Arial,sans-serif; color:#0f172a; line-height:1.5;'>"
        f"<p>Hola {escape(customer_name)},</p>"
        "<p>Tu pago fue aprobado y tu pedido ya está confirmado.</p>"
        f"<p><strong>Pedido:</strong> {escape(order_number)}<br/>"
        f"<strong>Fecha:</strong> {_format_datetime_bogota(order.paid_at or order.updated_at)}<br/>"
        f"<strong>Total:</strong> {_format_money_cop(order.total)}<br/>"
        f"<strong>Método de entrega:</strong> {'Retiro en tienda' if float(order.shipping_amount or 0.0) <= 0 else 'Envío'}</p>"
        + (
            f"<p><strong>Documento de venta:</strong> {escape(sale_number)}</p>"
            if sale_number
            else ""
        )
        + "<p><strong>Resumen del pedido</strong></p>"
        + "<table style='border-collapse:collapse; width:100%; max-width:760px; margin-bottom:12px;'>"
        + "<thead><tr>"
          "<th style='padding:6px 8px; border:1px solid #e5e7eb; text-align:left;'>Producto</th>"
          "<th style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>Cantidad</th>"
          "<th style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>Total</th>"
          "</tr></thead>"
        + f"<tbody>{''.join(lines)}</tbody></table>"
        + "<p>Gracias por comprar con Kensar Electronic.</p>"
        + "</div>"
    )


def _build_web_order_approved_internal_html(
    order: models.WebOrder,
    *,
    sale: Optional[models.Sale],
    conversion_error: Optional[str],
) -> str:
    order_number = order.document_number or f"OW-{order.id:06d}"
    provider_ref = ""
    if order.payments:
        latest = sorted(order.payments, key=lambda row: row.created_at or datetime.min)[-1]
        provider_ref = (latest.provider_reference or "").strip()
    items_html = []
    for item in order.items or []:
        items_html.append(
            "<li>"
            f"{escape(item.product_name_snapshot or 'Producto')} × {float(item.quantity or 0.0):g} "
            f"({ _format_money_cop(item.line_total) })"
            "</li>"
        )
    if not items_html:
        items_html.append("<li>Sin ítems.</li>")

    conversion_state = (
        f"Convertida a venta {escape(sale.document_number or str(sale.id))}"
        if sale
        else f"Pendiente de conversión: {escape(conversion_error or 'sin detalle')}"
    )
    return (
        "<div style='font-family:Arial,sans-serif; color:#0f172a; line-height:1.5;'>"
        "<p>Se registró un pago aprobado en Comercio Web.</p>"
        f"<p><strong>Orden:</strong> {escape(order_number)}<br/>"
        f"<strong>Estado pago:</strong> aprobado<br/>"
        f"<strong>Referencia proveedor:</strong> {escape(provider_ref or '-')}<br/>"
        f"<strong>Total:</strong> {_format_money_cop(order.total)}<br/>"
        f"<strong>Cliente:</strong> {escape(order.customer_name or 'No definido')}<br/>"
        f"<strong>Correo cliente:</strong> {escape(order.customer_email or 'No definido')}<br/>"
        f"<strong>Teléfono:</strong> {escape(order.customer_phone or '-')}<br/>"
        f"<strong>Dirección:</strong> {escape(order.customer_address or '-')}<br/>"
        f"<strong>Conversión:</strong> {conversion_state}</p>"
        "<p><strong>Ítems</strong></p>"
        f"<ul>{''.join(items_html)}</ul>"
        "</div>"
    )


def _send_web_order_customer_approval_email(
    db: Session,
    order: models.WebOrder,
    settings: models.PosSettings,
    *,
    sale: Optional[models.Sale] = None,
) -> None:
    if order.customer_approval_email_sent_at is not None:
        return
    recipient = (order.customer_email or "").strip()
    if not recipient:
        order.customer_approval_email_sent_at = datetime.utcnow()
        order.customer_approval_email_last_error = "Orden sin correo de cliente."
        db.add(order)
        db.commit()
        return

    payment_labels = _payment_method_labels_by_slug(db, order.tenant_id)
    attachments: list[tuple[str, bytes, str]] = []
    if sale:
        try:
            invoice_pdf = ticket_renderer.render_sale_ticket_pdf(
                sale,
                settings=settings,
                mode=ticket_renderer.INVOICE_MODE,
                payment_method_labels=payment_labels,
            )
            attachments.append(
                (
                    f"factura_{sale.sale_number or sale.id}.pdf",
                    invoice_pdf,
                    "application/pdf",
                )
            )
        except Exception:
            logger.exception("No se pudo adjuntar factura PDF para la orden web %s", order.id)

    subject = f"Pago aprobado - Pedido {order.document_number or order.id}"
    body_html = _build_web_order_approved_customer_html(order, sale=sale)
    email_service.send_email(
        recipients=[recipient],
        subject=subject,
        html_body=body_html,
        attachments=attachments,
        smtp_config=_smtp_settings_dict(settings),
    )
    order.customer_approval_email_sent_at = datetime.utcnow()
    order.customer_approval_email_last_error = None
    db.add(order)
    db.commit()


def _send_web_order_internal_approval_email(
    order: models.WebOrder,
    settings: models.PosSettings,
    *,
    sale: Optional[models.Sale],
    conversion_error: Optional[str],
) -> tuple[bool, Optional[str]]:
    recipients = _collect_internal_notification_recipients(settings)
    if not recipients:
        return False, "No hay destinatarios internos configurados."
    subject = f"Nueva venta web aprobada - {order.document_number or order.id}"
    body_html = _build_web_order_approved_internal_html(
        order,
        sale=sale,
        conversion_error=conversion_error,
    )
    email_service.send_email(
        recipients=recipients,
        subject=subject,
        html_body=body_html,
        smtp_config=_smtp_settings_dict(settings),
    )
    return True, None


def _run_web_order_post_approval_flow(db: Session, order: models.WebOrder) -> None:
    if order.payment_status != "approved":
        return

    settings = crud.get_pos_settings(db, tenant_id=order.tenant_id)
    sale: Optional[models.Sale] = None
    conversion_error: Optional[str] = None

    try:
        if order.sale_id is None:
            crud.convert_web_order_to_sale(
                db,
                order,
                schemas.WebOrderConvertToSaleRequest(note="Conversión automática al aprobar pago Mercado Pago"),
                actor_user_id=None,
            )
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        if refreshed:
            order = refreshed
    except Exception as exc:
        conversion_error = str(exc)
        logger.exception("No se pudo convertir la orden web %s a venta automáticamente", order.id)

    if order.sale_id is not None:
        sale = crud.get_sale(db, order.sale_id, tenant_id=order.tenant_id)

    try:
        _send_web_order_customer_approval_email(db, order, settings, sale=sale)
    except Exception as exc:
        logger.exception("No se pudo enviar correo de aprobación al cliente para orden web %s", order.id)
        order.customer_approval_email_last_error = str(exc)
        db.add(order)
        db.commit()

    if order.internal_approval_email_sent_at is None:
        try:
            _sent, note = _send_web_order_internal_approval_email(
                order,
                settings,
                sale=sale,
                conversion_error=conversion_error,
            )
            order.internal_approval_email_sent_at = datetime.utcnow()
            order.internal_approval_email_last_error = note
            db.add(order)
            db.commit()
        except Exception as exc:
            logger.exception("No se pudo enviar correo interno de orden aprobada %s", order.id)
            order.internal_approval_email_last_error = str(exc)
            db.add(order)
            db.commit()


def _build_checkout_back_urls(
    order_id: int,
    *,
    order_access_token: Optional[str] = None,
) -> Optional[dict[str, str]]:
    checkout_base = _get_checkout_base_url()
    if not checkout_base:
        return None
    encoded_order_id = urllib_parse.quote(str(order_id))
    access_query = (
        f"&accessToken={urllib_parse.quote(order_access_token)}"
        if order_access_token
        else ""
    )
    candidate_back_urls = {
        "success": f"{checkout_base}/pago/resultado?orderId={encoded_order_id}&payment=success{access_query}",
        "failure": f"{checkout_base}/pago/resultado?orderId={encoded_order_id}&payment=failure{access_query}",
        "pending": f"{checkout_base}/pago/resultado?orderId={encoded_order_id}&payment=pending{access_query}",
    }
    if all(_is_valid_public_back_url(url) for url in candidate_back_urls.values()):
        return candidate_back_urls
    return None


def _build_payer_payload(
    order: models.WebOrder,
    payer_input: Optional[schemas.MercadoPagoPayerInput],
) -> dict[str, Any]:
    payer: dict[str, Any] = {}
    if payer_input:
        if payer_input.email:
            payer["email"] = payer_input.email.strip()
        if payer_input.first_name:
            payer["name"] = payer_input.first_name.strip()
        if payer_input.last_name:
            payer["surname"] = payer_input.last_name.strip()
        if (
            payer_input.identification
            and payer_input.identification.type
            and payer_input.identification.number
        ):
            payer["identification"] = {
                "type": payer_input.identification.type.strip(),
                "number": payer_input.identification.number.strip(),
            }
    if not payer.get("email") and order.customer_email:
        payer["email"] = order.customer_email
    return payer


def _create_checkout_preference_for_order(
    order: models.WebOrder,
    *,
    payer_input: Optional[schemas.MercadoPagoPayerInput] = None,
    order_access_token: Optional[str] = None,
) -> schemas.WebMercadoPagoCheckoutCreateResponse:
    if order.status in {"cancelled", "refunded", "fulfilled"}:
        raise HTTPException(status_code=400, detail="La orden no admite nuevos pagos")
    if order.payment_status == "approved":
        raise HTTPException(status_code=400, detail="La orden ya tiene un pago aprobado")
    if not order.items:
        raise HTTPException(status_code=400, detail="La orden no tiene items para pagar")

    token = _get_mercadopago_access_token()
    back_urls = _build_checkout_back_urls(order.id, order_access_token=order_access_token)
    payer = _build_payer_payload(order, payer_input)

    preference_payload: dict[str, Any] = {
        "items": _build_checkout_items(order),
        "payer": payer if payer else None,
        "notification_url": _get_webhook_url(),
        "external_reference": f"web-order:{order.id}",
        "metadata": {
            "order_id": str(order.id),
            "tenant_id": str(order.tenant_id or ""),
            "account_id": str(order.account_id),
            "document_number": order.document_number or "",
        },
    }
    if back_urls:
        preference_payload["back_urls"] = back_urls
        preference_payload["auto_return"] = "approved"
    if order.currency:
        preference_payload["items"] = [
            {
                **entry,
                "currency_id": order.currency,
            }
            for entry in preference_payload["items"]
        ]

    preference_payload = {key: value for key, value in preference_payload.items() if value is not None}
    preference = _mercadopago_request(
        "POST",
        "/checkout/preferences",
        access_token=token,
        payload=preference_payload,
    )
    preference_id = str(preference.get("id") or "").strip()
    if not preference_id:
        raise HTTPException(status_code=502, detail="Mercado Pago no devolvió preference id")

    return schemas.WebMercadoPagoCheckoutCreateResponse(
        order_id=order.id,
        provider="mercadopago",
        preference_id=preference_id,
        init_point=preference.get("init_point"),
        sandbox_init_point=preference.get("sandbox_init_point"),
        public_key=_get_mercadopago_public_key(),
        order_access_token=order_access_token,
    )


def _build_guest_order_access_token(order: models.WebOrder) -> str:
    return create_access_token(
        user_id=order.id,
        role="WebGuestOrder",
        ttl=WEB_GUEST_ORDER_TOKEN_TTL_SECONDS,
        subject_type="web-guest-order",
    )


def _create_guest_order(
    db: Session,
    payload: schemas.WebGuestMercadoPagoCheckoutCreateRequest,
) -> models.WebOrder:
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    if not payload.items:
        raise HTTPException(status_code=400, detail="Debes incluir productos para continuar.")

    guest_account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant_id)
    item_inputs = [item for item in payload.items if item.quantity > 0]
    if not item_inputs:
        raise HTTPException(status_code=400, detail="El checkout no tiene items válidos.")

    product_ids = list({int(item.product_id) for item in item_inputs})
    qty_by_product = crud._get_web_cart_stock_snapshot(db, tenant_id, product_ids)

    subtotal_base = 0.0
    line_items_payload: list[dict[str, Any]] = []
    for item_input in item_inputs:
        product = crud.get_product(db, int(item_input.product_id), tenant_id=tenant_id)
        if not product or not product.active or not product.web_published:
            raise HTTPException(
                status_code=400,
                detail=f"Producto {item_input.product_id} no disponible para checkout web.",
            )
        stock_status = crud.resolve_web_product_stock_status(
            product,
            qty_by_product.get(product.id, 0.0),
        )
        if stock_status == "out_of_stock" and product.web_visible_when_out_of_stock is False:
            raise HTTPException(
                status_code=400,
                detail=f"Producto {product.name} no disponible por stock.",
            )

        quantity = float(item_input.quantity or 0.0)
        if quantity <= 0:
            continue
        unit_price = float(crud.resolve_web_product_sale_price(product) or 0.0)
        line_total = unit_price * quantity
        subtotal_base += line_total
        line_items_payload.append(
            {
                "product_id": product.id,
                "product_name_snapshot": product.name,
                "product_sku_snapshot": product.sku,
                "product_barcode_snapshot": product.barcode,
                "unit_price_snapshot": unit_price,
                "quantity": quantity,
                "line_discount_value": 0.0,
                "line_total": line_total,
            }
        )

    if subtotal_base <= 0 or not line_items_payload:
        raise HTTPException(status_code=400, detail="No se pudo construir una orden válida.")

    customer_name = (payload.customer_name or "").strip() or str(payload.customer_email)
    customer_email = str(payload.customer_email).strip().lower()
    customer_phone = (payload.customer_phone or "").strip() or None
    customer_tax_id = (payload.customer_tax_id or "").strip() or None
    customer_address = (payload.customer_address or "").strip() or None
    subtotal_amount = round(subtotal_base, 2)
    currency = "COP"

    crud.expire_stale_web_orders(db, tenant_id=tenant_id)
    if _is_guest_order_reuse_enabled():
        reusable = crud.find_reusable_pending_web_order(
            db,
            tenant_id=tenant_id,
            account_id=guest_account.id,
            customer_email=customer_email,
            currency=currency,
            subtotal=subtotal_amount,
            discount_amount=0.0,
            total=subtotal_amount,
            item_signature=crud.build_web_order_item_signature(line_items_payload),
        )
        if reusable:
            if reusable.status == "payment_failed":
                crud._transition_web_order_status(
                    db,
                    reusable,
                    to_status="pending_payment",
                    note="Reintento de pago en checkout invitado",
                    actor_type="guest",
                )
            reusable.customer_name = customer_name
            reusable.customer_email = customer_email
            reusable.customer_phone = customer_phone
            reusable.customer_tax_id = customer_tax_id
            reusable.customer_address = customer_address
            reusable.notes = ((payload.notes or "").strip() or reusable.notes)
            reusable.updated_at = datetime.utcnow()
            crud._create_web_order_status_log(
                db,
                reusable,
                from_status=reusable.status,
                to_status=reusable.status,
                note="Orden invitada reutilizada para reintento de pago",
                actor_type="guest",
            )
            db.commit()
            stored = crud.get_backoffice_web_order(db, reusable.id, tenant_id=tenant_id)
            if not stored:
                raise HTTPException(status_code=500, detail="No se pudo recuperar la orden invitada.")
            return stored

    number = crud.get_next_web_order_number(db, tenant_id=tenant_id)
    document_number = f"OW-{number:06d}"
    order = models.WebOrder(
        tenant_id=tenant_id,
        web_order_number=number,
        document_number=document_number,
        account_id=guest_account.id,
        pos_customer_id=None,
        status="pending_payment",
        payment_status="pending",
        fulfillment_status="pending",
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        customer_tax_id=customer_tax_id,
        customer_address=customer_address,
        subtotal=subtotal_amount,
        discount_amount=0.0,
        shipping_amount=0.0,
        total=subtotal_amount,
        currency=currency,
        notes=((payload.notes or "").strip() or None),
        submitted_at=datetime.utcnow(),
    )
    db.add(order)
    db.flush()
    for line in line_items_payload:
        db.add(
            models.WebOrderItem(
                tenant_id=order.tenant_id,
                web_order_id=order.id,
                product_id=int(line["product_id"]),
                product_name_snapshot=str(line["product_name_snapshot"]),
                product_sku_snapshot=line.get("product_sku_snapshot"),
                product_barcode_snapshot=line.get("product_barcode_snapshot"),
                unit_price_snapshot=float(line["unit_price_snapshot"]),
                quantity=float(line["quantity"]),
                line_discount_value=float(line["line_discount_value"]),
                line_total=float(line["line_total"]),
            )
        )
    crud._create_web_order_status_log(
        db,
        order,
        from_status=None,
        to_status=order.status,
        note="Orden creada desde checkout invitado",
        actor_type="guest",
    )
    db.commit()
    stored = crud.get_backoffice_web_order(db, order.id, tenant_id=tenant_id)
    if not stored:
        raise HTTPException(status_code=500, detail="No se pudo recuperar la orden invitada.")
    return stored


def _require_guest_order_access_token(
    order_id: int,
    access_token: str,
) -> None:
    token = (access_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token de acceso de invitado requerido.")
    try:
        payload = verify_access_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if payload.get("kind") != "web-guest-order":
        raise HTTPException(status_code=401, detail="Token invitado inválido.")
    try:
        token_order_id = int(payload.get("sub"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Token invitado inválido.") from exc
    if token_order_id != int(order_id):
        raise HTTPException(status_code=403, detail="Token invitado no corresponde a la orden.")


def _process_payment_notification(db: Session, payment_id: str) -> schemas.WebOrderRead:
    token = _get_mercadopago_access_token()
    payment_data = _mercadopago_request("GET", f"/v1/payments/{urllib_parse.quote(str(payment_id))}", access_token=token)

    order_id = _extract_order_id(payment_data)
    if not order_id:
        raise HTTPException(status_code=400, detail="No se pudo resolver la orden para el pago")

    tenant_id = _extract_tenant_id(payment_data)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada para notificación")

    provider_reference = str(payment_data.get("id") or "").strip()
    if not provider_reference:
        raise HTTPException(status_code=400, detail="Pago sin identificador válido")

    status = _normalize_payment_status(payment_data.get("status"))
    if status in {"failed", "cancelled"} and order.payment_status == "approved":
        return crud._serialize_web_order(order)
    if status == "pending" and order.payment_status == "approved":
        return crud._serialize_web_order(order)

    payload = schemas.WebOrderPaymentRecordRequest(
        method=str(payment_data.get("payment_method_id") or payment_data.get("payment_type_id") or "mercadopago"),
        amount=float(payment_data.get("transaction_amount") or 0.0),
        provider="mercadopago",
        provider_reference=provider_reference,
        status=status,
        note=f"Webhook Mercado Pago ({status})",
        raw_payload=payment_data,
    )
    updated = crud.record_web_order_payment(db, order, payload, actor_user_id=None)
    refreshed = crud.get_backoffice_web_order(db, updated.id, tenant_id=order.tenant_id)
    if refreshed and refreshed.payment_status == "approved":
        _run_web_order_post_approval_flow(db, refreshed)
        refreshed = crud.get_backoffice_web_order(db, updated.id, tenant_id=order.tenant_id) or refreshed
        return crud._serialize_web_order(refreshed)
    return updated


def _refresh_order_payment_status_from_provider(
    db: Session,
    order: models.WebOrder,
) -> models.WebOrder:
    if not order or order.payment_status == "approved":
        return order
    try:
        token = _get_mercadopago_access_token()
        search = _mercadopago_request(
            "GET",
            f"/v1/payments/search?external_reference={urllib_parse.quote(f'web-order:{order.id}')}&sort=date_created&criteria=desc&limit=1",
            access_token=token,
        )
        results = search.get("results") if isinstance(search, dict) else None
        if not isinstance(results, list) or not results:
            return order
        payment_id = str((results[0] or {}).get("id") or "").strip()
        if not payment_id:
            return order
        _process_payment_notification(db, payment_id)
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        return refreshed or order
    except HTTPException:
        return order
    except Exception:
        logger.exception("No se pudo sincronizar estado de pago Mercado Pago para la orden %s", order.id)
        return order


@router.post("/checkout", response_model=schemas.WebMercadoPagoCheckoutCreateResponse)
def create_mercadopago_checkout(
    payload: schemas.WebMercadoPagoCheckoutCreateRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, payload.order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    return _create_checkout_preference_for_order(order, payer_input=payload.payer)


@router.post("/guest-checkout", response_model=schemas.WebMercadoPagoCheckoutCreateResponse)
def create_guest_mercadopago_checkout(
    payload: schemas.WebGuestMercadoPagoCheckoutCreateRequest,
    db: Session = Depends(get_db),
):
    order = _create_guest_order(db, payload)
    order_access_token = _build_guest_order_access_token(order)
    return _create_checkout_preference_for_order(
        order,
        payer_input=payload.payer,
        order_access_token=order_access_token,
    )


@router.post("/webhook")
async def receive_mercadopago_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_signature: Optional[str] = Header(default=None),
    x_request_id: Optional[str] = Header(default=None),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    params = request.query_params
    data_id = params.get("data.id") or str((body.get("data") or {}).get("id") or "")
    webhook_secret = (os.getenv("MERCADOPAGO_WEBHOOK_SECRET") or "").strip()
    if webhook_secret:
        if not data_id or not x_signature or not x_request_id:
            raise HTTPException(status_code=401, detail="Webhook sin headers de firma")
        if not _is_valid_webhook_signature(
            secret=webhook_secret,
            signature_header=x_signature,
            request_id=x_request_id,
            data_id=data_id,
        ):
            raise HTTPException(status_code=401, detail="Firma de webhook inválida")

    event_type = (
        params.get("type")
        or params.get("topic")
        or str(body.get("type") or body.get("topic") or "")
    ).strip().lower()

    try:
        if event_type in {"payment", "payments"}:
            payment_id = data_id or str((body.get("data") or {}).get("id") or "")
            if not payment_id:
                raise HTTPException(status_code=400, detail="Notificación de pago sin data.id")
            updated = _process_payment_notification(db, payment_id)
            return {"ok": True, "order_id": updated.id, "status": updated.status}

        if event_type in {"merchant_order", "order"}:
            merchant_order_id = data_id or str((body.get("data") or {}).get("id") or "")
            if not merchant_order_id:
                return {"ok": True, "ignored": "merchant_order sin data.id"}
            token = _get_mercadopago_access_token()
            order_data = _mercadopago_request(
                "GET",
                f"/merchant_orders/{urllib_parse.quote(str(merchant_order_id))}",
                access_token=token,
            )
            processed = 0
            for payment in (order_data.get("payments") or []):
                payment_id = str(payment.get("id") or "").strip()
                if not payment_id:
                    continue
                _process_payment_notification(db, payment_id)
                processed += 1
            return {"ok": True, "processed_payments": processed}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error procesando webhook Mercado Pago")
        raise HTTPException(status_code=500, detail="No se pudo procesar webhook") from exc

    return {"ok": True, "ignored": event_type or "unknown"}


@router.get("/orders/{order_id}/status", response_model=schemas.WebMercadoPagoOrderPaymentStatusResponse)
def get_mercadopago_order_status(
    order_id: int,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    order = _refresh_order_payment_status_from_provider(db, order)
    if order and order.payment_status == "approved" and (
        order.customer_approval_email_sent_at is None or order.internal_approval_email_sent_at is None
    ):
        _run_web_order_post_approval_flow(db, order)
        order = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id) or order
    return _build_status_response(order)


@router.get("/guest/orders/{order_id}/status", response_model=schemas.WebMercadoPagoOrderPaymentStatusResponse)
def get_guest_mercadopago_order_status(
    order_id: int,
    access_token: str,
    db: Session = Depends(get_db),
):
    _require_guest_order_access_token(order_id, access_token)
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    order = _refresh_order_payment_status_from_provider(db, order)
    if order and order.payment_status == "approved" and (
        order.customer_approval_email_sent_at is None or order.internal_approval_email_sent_at is None
    ):
        _run_web_order_post_approval_flow(db, order)
        order = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id) or order
    return _build_status_response(order)
