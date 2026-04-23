import hashlib
import hmac
import json
import logging
import os
import base64
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from routers.web_customers import require_web_customer_auth
from services import email as email_service
from services import ticket_renderer
from services.payments.routing import resolve_provider_for_method
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
CHECKOUT_CONTEXT_NOTE_MARKER = "CHECKOUT_CONTEXT_JSON:"


def _get_mercadopago_provider():
    # Lazy import avoids module cycle during app startup.
    from services.payments.registry import get_provider

    provider = get_provider("mercadopago")
    if provider is None:
        raise HTTPException(status_code=503, detail="Proveedor Mercado Pago no disponible")
    return provider


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


def _get_hidden_personalization_service_skus() -> set[str]:
    raw = (os.getenv("WEB_HIDDEN_PERSONALIZATION_SERVICE_SKUS") or "").strip()
    if not raw:
        return {"3740", "3741"}
    return {token.strip() for token in raw.split(",") if token.strip()}


def _is_hidden_personalization_service_allowed_for_checkout(
    product: Optional[models.Product],
    checkout_context: Optional[dict[str, Any]],
) -> bool:
    if not product:
        return False
    if product.web_published:
        return False
    if not bool(product.service):
        return False
    if not isinstance(checkout_context, dict):
        return False
    personalization = checkout_context.get("personalization")
    if not isinstance(personalization, dict):
        return False
    sku = (product.sku or "").strip()
    if not sku:
        return False
    return sku in _get_hidden_personalization_service_skus()


def _get_mercadopago_env_label() -> str:
    return (os.getenv("MERCADOPAGO_ENV") or "unknown").strip().lower() or "unknown"


def _mask_email(value: Optional[str]) -> str:
    email = (value or "").strip().lower()
    if not email or "@" not in email:
        return "-"
    local, domain = email.split("@", 1)
    if not local:
        return f"***@{domain}"
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[:2] + "*" * max(1, len(local) - 2)
    return f"{masked_local}@{domain}"


def _mask_document(value: Optional[str]) -> str:
    raw = (value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return "-"
    if len(digits) <= 2:
        return "*" * len(digits)
    return "*" * max(0, len(digits) - 2) + digits[-2:]


def _sanitize_payer_for_logs(payer_input: Optional[schemas.MercadoPagoPayerInput]) -> dict[str, Any]:
    if not payer_input:
        return {
            "has_payer": False,
            "email": "-",
            "first_name_present": False,
            "last_name_present": False,
            "identification_type": "-",
            "identification_number_masked": "-",
        }
    identification = payer_input.identification
    return {
        "has_payer": True,
        "email": _mask_email(payer_input.email),
        "first_name_present": bool((payer_input.first_name or "").strip()),
        "last_name_present": bool((payer_input.last_name or "").strip()),
        "identification_type": (identification.type.strip() if identification and identification.type else "-"),
        "identification_number_masked": _mask_document(identification.number if identification else None),
    }


def _log_checkout_attempt(
    *,
    flow: str,
    order_id: Optional[int],
    customer_email: Optional[str],
    customer_tax_id: Optional[str],
    customer_address: Optional[str],
    items_count: Optional[int],
    payer_input: Optional[schemas.MercadoPagoPayerInput],
) -> None:
    payer_data = _sanitize_payer_for_logs(payer_input)
    logger.info(
        "Checkout attempt | env=%s flow=%s order_id=%s customer_email=%s customer_tax_id=%s has_address=%s items_count=%s payer=%s",
        _get_mercadopago_env_label(),
        flow,
        order_id if order_id is not None else "-",
        _mask_email(customer_email),
        _mask_document(customer_tax_id),
        bool((customer_address or "").strip()),
        items_count if items_count is not None else "-",
        json.dumps(payer_data, ensure_ascii=True),
    )


def _localize_mercadopago_error_detail(detail: str) -> str:
    text = (detail or "").strip()
    if not text:
        return "Error de Mercado Pago."
    lowered = text.lower()
    if "rejected" in lowered or "not_approved" in lowered or "cc_rejected" in lowered:
        return "Pago rechazado por Mercado Pago."
    if "invalid" in lowered:
        return "Solicitud inválida enviada a Mercado Pago."
    if "unauthorized" in lowered:
        return "Credenciales inválidas de Mercado Pago."
    if "forbidden" in lowered:
        return "Mercado Pago rechazó la operación."
    if "not found" in lowered:
        return "Recurso no encontrado en Mercado Pago."
    if "timeout" in lowered:
        return "Mercado Pago tardó demasiado en responder."
    return text


def _split_order_notes_checkout_context(notes: Optional[str]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    raw_notes = (notes or "").strip()
    if not raw_notes:
        return None, None
    idx = raw_notes.find(CHECKOUT_CONTEXT_NOTE_MARKER)
    if idx < 0:
        return raw_notes, None
    note_text = raw_notes[:idx].strip() or None
    context_raw = raw_notes[idx + len(CHECKOUT_CONTEXT_NOTE_MARKER) :].strip()
    if not context_raw:
        return note_text, None
    try:
        parsed = json.loads(context_raw)
        if isinstance(parsed, dict):
            return note_text, parsed
    except Exception:
        pass
    return note_text, None


def _normalize_checkout_context_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str):
            return value.strip()
        return value
    if isinstance(value, list):
        normalized_list = [
            _normalize_checkout_context_value(entry, depth=depth + 1)
            for entry in value[:120]
        ]
        return [entry for entry in normalized_list if entry not in [None, "", [], {}]]
    if isinstance(value, dict):
        normalized_dict: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                continue
            normalized = _normalize_checkout_context_value(raw_value, depth=depth + 1)
            if normalized in [None, "", [], {}]:
                continue
            normalized_dict[key] = normalized
        return normalized_dict
    return str(value)


def _merge_order_notes_with_checkout_context(
    notes: Optional[str],
    checkout_context: Optional[dict[str, Any]],
) -> Optional[str]:
    note_text, _existing_context = _split_order_notes_checkout_context(notes)
    normalized_ctx = _normalize_checkout_context_value(checkout_context or {}, depth=0)
    if not isinstance(normalized_ctx, dict) or not normalized_ctx:
        return note_text
    context_json = json.dumps(normalized_ctx, ensure_ascii=False, separators=(",", ":"))
    if note_text:
        return f"{note_text}\n\n{CHECKOUT_CONTEXT_NOTE_MARKER}{context_json}"
    return f"{CHECKOUT_CONTEXT_NOTE_MARKER}{context_json}"


def _persist_checkout_context_on_order(
    db: Session,
    order: models.WebOrder,
    *,
    checkout_context: Optional[dict[str, Any]],
) -> models.WebOrder:
    next_notes = _merge_order_notes_with_checkout_context(order.notes, checkout_context)
    if next_notes == order.notes:
        return order
    order.notes = next_notes
    order.updated_at = datetime.utcnow()
    db.add(order)
    db.commit()
    refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
    return refreshed or order


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

    logger.debug(
        "MP request | method=%s url=%s has_payload=%s",
        method.upper(),
        url,
        payload is not None,
    )

    request = urllib_request.Request(url=url, data=body_bytes, headers=headers, method=method.upper())
    try:
        with urllib_request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8")
            logger.debug(
                "MP response | method=%s url=%s status=%s",
                method.upper(),
                url,
                getattr(response, "status", "unknown"),
            )
            return json.loads(raw) if raw else {}
    except urllib_error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {}
        logger.warning(
            "MP error response | method=%s url=%s status=%s",
            method.upper(),
            url,
            exc.code,
        )
        detail = parsed.get("message") or parsed.get("error") or f"Mercado Pago HTTP {exc.code}"
        localized_detail = _localize_mercadopago_error_detail(str(detail))
        raise HTTPException(status_code=400, detail=f"Mercado Pago: {localized_detail}") from exc
    except urllib_error.URLError as exc:
        logger.warning("MP connection error | method=%s url=%s error=%s", method.upper(), url, str(exc))
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


def _normalize_payment_status(status: str | None) -> str:
    if not status:
        return "pending"

    status = status.lower()

    if status in ["approved"]:
        return "approved"

    if status in ["rejected", "chargeback", "in_mediation"]:
        return "failed"

    if status in ["cancelled"]:
        return "cancelled"

    if status in ["refunded", "charged_back"]:
        return "refunded"

    if status in ["in_process", "pending"]:
        return "pending"

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


def _resolve_email_asset_url(raw_url: Optional[str]) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://", "data:")):
        return url
    if url.startswith("//"):
        return f"https:{url}"

    base_candidates = [
        os.getenv("POS_LOGO_BASE_URL"),
        os.getenv("APP_BASE_URL"),
        os.getenv("PUBLIC_APP_URL"),
    ]
    base_url = next((value.strip() for value in base_candidates if value and value.strip()), "")
    if not base_url:
        return url
    return f"{base_url.rstrip('/')}/{url.lstrip('/')}"


def _build_email_logo_footer(settings: Optional[models.PosSettings]) -> str:
    if not settings:
        return ""
    logo_url = _resolve_email_asset_url(settings.ticket_logo_url or settings.logo_url)
    company_name = (settings.company_name or "Kensar Electronic").strip()
    company_address = (settings.address or "").strip()
    company_phone = (settings.contact_phone or "").strip()
    company_tax_id = (settings.tax_id or "").strip()

    info_lines: list[str] = [
        f"<div style='font-weight:700; margin-bottom:4px;'>{escape(company_name)}</div>"
    ]
    if company_address:
        info_lines.append(f"<div><strong>Dirección:</strong> {escape(company_address)}</div>")
    if company_phone:
        info_lines.append(f"<div><strong>Teléfono:</strong> {escape(company_phone)}</div>")
    if company_tax_id:
        info_lines.append(f"<div><strong>NIT:</strong> {escape(company_tax_id)}</div>")

    logo_cell = (
        f"<img src='{escape(logo_url)}' alt='Logo {escape(company_name)}' "
        "style='display:block; max-width:250px; max-height:100px; width:auto; height:auto;'/>"
        if logo_url
        else ""
    )
    if not logo_cell and len(info_lines) <= 1:
        return ""

    return (
        "<div style='margin-top:18px; padding-top:12px; border-top:1px solid #e5e7eb;'>"
        "<table role='presentation' style='width:auto; border-collapse:collapse;'>"
        "<tr>"
        f"<td style='width:1%; white-space:nowrap; vertical-align:top; padding:0 2px 0 0;'>{logo_cell}</td>"
        "<td style='vertical-align:top; font-size:13px; line-height:1.5; color:#374151;'>"
        f"{''.join(info_lines)}"
        "</td>"
        "</tr>"
        "</table>"
        "</div>"
    )


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


def _extract_personalization_preview_images_from_order(
    order: models.WebOrder,
) -> dict[str, str]:
    _note_text, checkout_context = _split_order_notes_checkout_context(order.notes)
    if not isinstance(checkout_context, dict):
        return {}
    personalization = checkout_context.get("personalization")
    if not isinstance(personalization, dict):
        return {}
    image_map = personalization.get("preview_images")
    if not isinstance(image_map, dict):
        return {}

    sanitized: dict[str, str] = {}
    for key in ("front", "left", "right"):
        value = image_map.get(key)
        if not isinstance(value, str):
            continue
        raw = value.strip()
        if not raw.startswith("data:image/"):
            continue
        # Evita inyectar blobs gigantes en el correo.
        if len(raw) > 500_000:
            continue
        sanitized[key] = raw
    return sanitized


def _decode_personalization_data_image(data_url: str) -> tuple[bytes, str, str] | None:
    raw = (data_url or "").strip()
    if not raw.startswith("data:image/"):
        return None
    if ";base64," not in raw:
        return None
    header, encoded = raw.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip().lower()
    if not mime.startswith("image/"):
        return None
    ext = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime, "png")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception:
        return None
    if not content:
        return None
    return content, mime, ext


def _build_personalization_preview_email_assets(
    order: models.WebOrder,
) -> tuple[str, list[email_service.InlineAttachment]]:
    previews = _extract_personalization_preview_images_from_order(order)
    if not previews:
        return "", []

    labels = {
        "front": "Frente",
        "left": "Lateral izquierda",
        "right": "Lateral derecha",
    }
    cards: list[str] = []
    inline_attachments: list[email_service.InlineAttachment] = []
    for key in ("front", "left", "right"):
        src = previews.get(key)
        if not src:
            continue
        decoded = _decode_personalization_data_image(src)
        if not decoded:
            continue
        image_bytes, mime, ext = decoded
        content_id = f"personaliza-{order.id}-{key}"
        inline_attachments.append(
            (
                f"personalizacion-{order.id}-{key}.{ext}",
                image_bytes,
                mime,
                content_id,
            )
        )
        cards.append(
            "<td style='vertical-align:top; width:33.33%; padding:0 6px 8px 0;'>"
            f"<div style='font-size:12px; font-weight:700; color:#334155; margin:0 0 6px 0;'>{escape(labels[key])}</div>"
            f"<img src='cid:{escape(content_id)}' alt='Vista {escape(labels[key])}' "
            "style='display:block; width:100%; max-width:240px; border:1px solid #cbd5e1; border-radius:8px;'/>"
            "</td>"
        )
    if not cards:
        return "", []

    return (
        "<div style='margin:14px 0 10px 0;'>"
        "<p style='margin:0 0 8px 0; font-weight:700;'>Referencia visual de personalización</p>"
        "<table role='presentation' style='width:100%; border-collapse:collapse;'><tr>"
        + "".join(cards)
        + "</tr></table>"
        "</div>",
        inline_attachments,
    )


def _build_web_order_approved_customer_html(
    order: models.WebOrder,
    *,
    sale: Optional[models.Sale] = None,
    settings: Optional[models.PosSettings] = None,
    personalization_previews_html: str = "",
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

    logo_footer = _build_email_logo_footer(settings)
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
        + personalization_previews_html
        + "<p>Gracias por comprar con Kensar Electronic.</p>"
        + logo_footer
        + "</div>"
    )


def _build_web_order_approved_internal_html(
    order: models.WebOrder,
    *,
    sale: Optional[models.Sale],
    conversion_error: Optional[str],
    settings: Optional[models.PosSettings] = None,
    personalization_previews_html: str = "",
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
    logo_footer = _build_email_logo_footer(settings)
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
        + personalization_previews_html
        + logo_footer
        + "</div>"
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
    personalization_previews_html, inline_preview_attachments = _build_personalization_preview_email_assets(order)
    body_html = _build_web_order_approved_customer_html(
        order,
        sale=sale,
        settings=settings,
        personalization_previews_html=personalization_previews_html,
    )
    email_service.send_email(
        recipients=[recipient],
        subject=subject,
        html_body=body_html,
        attachments=attachments,
        inline_attachments=inline_preview_attachments,
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
    personalization_previews_html, inline_preview_attachments = _build_personalization_preview_email_assets(order)
    body_html = _build_web_order_approved_internal_html(
        order,
        sale=sale,
        conversion_error=conversion_error,
        settings=settings,
        personalization_previews_html=personalization_previews_html,
    )
    email_service.send_email(
        recipients=recipients,
        subject=subject,
        html_body=body_html,
        inline_attachments=inline_preview_attachments,
        smtp_config=_smtp_settings_dict(settings),
    )
    return True, None


def _run_web_order_post_approval_flow(db: Session, order: models.WebOrder) -> None:
    if order.payment_status != "approved":
        return

    settings = crud.get_pos_settings(db, tenant_id=order.tenant_id)
    sale: Optional[models.Sale] = None
    conversion_error: Optional[str] = None
    approved_payments = [
        payment
        for payment in (order.payments or [])
        if (payment.status or "").strip().lower() == "approved"
    ]
    provider_label = "Proveedor online"
    if approved_payments:
        latest_approved = sorted(
            approved_payments,
            key=lambda row: row.approved_at or row.created_at or datetime.min,
        )[-1]
        provider_name = (latest_approved.provider or "").strip().lower()
        if provider_name == "mercadopago":
            provider_label = "Mercado Pago"
        elif provider_name == "wompi":
            provider_label = "Wompi"
        elif provider_name:
            provider_label = provider_name.replace("_", " ").title()

    try:
        if order.sale_id is None:
            crud.convert_web_order_to_sale(
                db,
                order,
                schemas.WebOrderConvertToSaleRequest(
                    note=f"Conversión automática al aprobar pago {provider_label}"
                ),
                actor_user_id=None,
            )
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        if refreshed:
            order = refreshed
    except Exception as exc:
        conversion_error = str(exc)
        logger.exception("No se pudo convertir la orden web %s a venta automáticamente", order.id)

    # Cierre defensivo del ciclo de negocio para pagos MP aprobados:
    # si ya existe venta no debe quedar operativamente en estados intermedios.
    approved_mp_payment = any(
        (payment.status == "approved")
        and ((payment.provider or "").strip().lower() == "mercadopago")
        for payment in (order.payments or [])
    )
    if approved_mp_payment and order.sale_id is not None and order.status in {
        "pending_payment",
        "payment_failed",
        "paid",
        "processing",
    }:
        try:
            crud._transition_web_order_status(
                db,
                order,
                to_status="fulfilled",
                note="Cierre automático tras pago Mercado Pago aprobado y venta consolidada",
                actor_type="system",
                actor_user_id=None,
            )
            db.commit()
            refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
            if refreshed:
                order = refreshed
        except ValueError:
            logger.warning(
                "No se pudo cerrar automáticamente la orden web %s tras aprobación de MP (status=%s sale_id=%s)",
                order.id,
                order.status,
                order.sale_id,
            )

    # Cierre operativo: al consolidar pago aprobado, el carrito activo del cliente
    # no debe seguir con ítems pendientes.
    if approved_mp_payment:
        try:
            crud.clear_active_web_cart_if_exists(
                db,
                account_id=order.account_id,
                tenant_id=order.tenant_id,
            )
            refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
            if refreshed:
                order = refreshed
        except Exception:
            logger.exception(
                "No se pudo limpiar el carrito activo para la orden web %s (account_id=%s)",
                order.id,
                order.account_id,
            )

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
        #"payer": payer if payer else None,
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

    init_point = str(preference.get("init_point") or "").strip()
    sandbox_init_point = str(preference.get("sandbox_init_point") or "").strip()
    selected_init_point = init_point or sandbox_init_point
    init_host = ""
    if selected_init_point:
        try:
            init_host = (urllib_parse.urlparse(selected_init_point).hostname or "").strip().lower()
        except Exception:
            init_host = ""

    logger.info(
        "MercadoPago preference created | env=%s order_id=%s preference_id=%s init_host=%s has_init=%s has_sandbox_init=%s",
        _get_mercadopago_env_label(),
        order.id,
        preference_id,
        init_host or "-",
        bool(init_point),
        bool(sandbox_init_point),
    )

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
        if not product or not product.active:
            raise HTTPException(
                status_code=400,
                detail=f"Producto {item_input.product_id} no disponible para checkout web.",
            )
        if not product.web_published and not _is_hidden_personalization_service_allowed_for_checkout(
            product,
            payload.checkout_context if isinstance(payload.checkout_context, dict) else None,
        ):
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
            next_notes_source = ((payload.notes or "").strip() or reusable.notes)
            reusable.notes = _merge_order_notes_with_checkout_context(
                next_notes_source,
                payload.checkout_context if isinstance(payload.checkout_context, dict) else None,
            )
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
        notes=_merge_order_notes_with_checkout_context(
            ((payload.notes or "").strip() or None),
            payload.checkout_context if isinstance(payload.checkout_context, dict) else None,
        ),
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
    if not order and tenant_id is not None:
        # Fallback defensivo: algunos pagos viejos/no estándar pueden no traer
        # metadata de tenant consistente aunque sí apunten a una orden válida.
        order = db.query(models.WebOrder).filter(models.WebOrder.id == order_id).first()
        if order:
            logger.warning(
                "Mercado Pago payment %s resolvió orden %s con tenant_id fallback "
                "(metadata tenant_id=%s, order tenant_id=%s)",
                payment_id,
                order_id,
                tenant_id,
                order.tenant_id,
            )
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada para notificación")

    provider_reference = str(payment_data.get("id") or "").strip()
    if not provider_reference:
        raise HTTPException(status_code=400, detail="Pago sin identificador válido")

    status = _normalize_payment_status(payment_data.get("status"))
    if status in {"failed", "cancelled"} and order.payment_status == "approved":
        _run_web_order_post_approval_flow(db, order)
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        return crud._serialize_web_order(refreshed or order)
    if status == "pending" and order.payment_status == "approved":
        _run_web_order_post_approval_flow(db, order)
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        return crud._serialize_web_order(refreshed or order)

    payload = schemas.WebOrderPaymentRecordRequest(
        method=str(payment_data.get("payment_method_id") or payment_data.get("payment_type_id") or "mercadopago"),
        amount=float(payment_data.get("transaction_amount") or 0.0),
        provider="mercadopago",
        provider_reference=provider_reference,
        status=status,
        note=f"Webhook Mercado Pago ({status})",
        raw_payload=payment_data,
    )
    existing_payment = (
        db.query(models.WebOrderPayment)
        .filter(
            models.WebOrderPayment.web_order_id == order.id,
            models.WebOrderPayment.provider == "mercadopago",
            models.WebOrderPayment.provider_reference == provider_reference,
        )
        .order_by(models.WebOrderPayment.id.desc())
        .first()
    )
    if existing_payment is not None:
        same_status = (existing_payment.status or "").strip().lower() == status
        same_amount = abs(float(existing_payment.amount or 0.0) - float(payload.amount or 0.0)) <= 0.0001
        if same_status and same_amount:
            return crud._serialize_web_order(order)

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
    if not order:
        return order
    if order.payment_status == "approved":
        _run_web_order_post_approval_flow(db, order)
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        return refreshed or order
    try:
        token = _get_mercadopago_access_token()
        search = _mercadopago_request(
            "GET",
            f"/v1/payments/search?external_reference={urllib_parse.quote(f'web-order:{order.id}')}&sort=date_created&criteria=desc&limit=20",
            access_token=token,
        )
        results = search.get("results") if isinstance(search, dict) else None
        if not isinstance(results, list) or not results:
            return order

        selected_payment_id = ""
        fallback_payment_id = ""
        for row in results:
            if not isinstance(row, dict):
                continue
            payment_id = str(row.get("id") or "").strip()
            if not payment_id:
                continue
            if not fallback_payment_id:
                fallback_payment_id = payment_id
            normalized_status = _normalize_payment_status(row.get("status"))
            if normalized_status in {"approved", "failed", "cancelled", "refunded"}:
                selected_payment_id = payment_id
                break

        payment_id_to_process = selected_payment_id or fallback_payment_id
        if not payment_id_to_process:
            return order

        _process_payment_notification(db, payment_id_to_process)
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        if refreshed and refreshed.payment_status == "approved":
            _run_web_order_post_approval_flow(db, refreshed)
            refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id) or refreshed
        return refreshed or order
    except HTTPException as exc:
        logger.warning(
            "No se pudo sincronizar estado de pago para orden %s: %s",
            getattr(order, "id", None),
            getattr(exc, "detail", str(exc)),
        )
        return order
    except Exception:
        logger.exception("No se pudo sincronizar estado de pago Mercado Pago para la orden %s", order.id)
        return order


def _normalize_checkout_result_hint(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"failure", "failed", "rejected", "cancelled", "canceled"}:
        return "failed"
    if normalized in {"success", "approved"}:
        return "approved"
    if normalized in {"pending", "in_process", "inprocess"}:
        return "pending"
    return ""


def _apply_checkout_result_hint(
    db: Session,
    order: models.WebOrder,
    *,
    payment_hint: Optional[str],
) -> models.WebOrder:
    hint = _normalize_checkout_result_hint(payment_hint)
    if hint != "failed":
        return order
    if not order or order.payment_status == "approved":
        return order
    if order.status in {"cancelled", "refunded", "fulfilled"}:
        return order

    provider_reference = f"checkout-result-failure-order-{order.id}"
    payload = schemas.WebOrderPaymentRecordRequest(
        method="mercadopago",
        amount=0.0,
        provider="mercadopago",
        provider_reference=provider_reference,
        status="failed",
        note="Resultado checkout: pago no aprobado",
        raw_payload={
            "source": "checkout_result",
            "payment_hint": payment_hint,
            "recorded_at": datetime.utcnow().isoformat(),
        },
    )
    try:
        crud.record_web_order_payment(db, order, payload, actor_user_id=None)
        refreshed = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        if refreshed:
            logger.info(
                "Checkout result hint aplicado | order_id=%s payment_hint=%s status=%s payment_status=%s",
                refreshed.id,
                payment_hint,
                refreshed.status,
                refreshed.payment_status,
            )
            return refreshed
    except Exception:
        logger.exception(
            "No se pudo aplicar checkout result hint a la orden %s (hint=%s)",
            getattr(order, "id", None),
            payment_hint,
        )
    return order


def refresh_backoffice_order_payment_statuses(
    db: Session,
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
        provider = _get_mercadopago_provider()
        refreshed_orders.append(provider.refresh_order_status(db, order))
    return refreshed_orders


@router.post("/checkout", response_model=schemas.WebMercadoPagoCheckoutCreateResponse)
def create_mercadopago_checkout(
    payload: schemas.WebMercadoPagoCheckoutCreateRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    resolved_provider = resolve_provider_for_method("card")
    if resolved_provider != "mercadopago":
        raise HTTPException(
            status_code=409,
            detail="El método card ya no está asignado a Mercado Pago",
        )
    order = crud.get_web_order(db, payload.order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    if isinstance(payload.checkout_context, dict):
        order = _persist_checkout_context_on_order(
            db,
            order,
            checkout_context=payload.checkout_context,
        )
    _log_checkout_attempt(
        flow="authenticated",
        order_id=order.id,
        customer_email=order.customer_email,
        customer_tax_id=order.customer_tax_id,
        customer_address=order.customer_address,
        items_count=len(order.items or []),
        payer_input=payload.payer,
    )
    provider = _get_mercadopago_provider()
    return provider.create_checkout(db, order, payer_input=payload.payer)


@router.post("/guest-checkout", response_model=schemas.WebMercadoPagoCheckoutCreateResponse)
def create_guest_mercadopago_checkout(
    payload: schemas.WebGuestMercadoPagoCheckoutCreateRequest,
    db: Session = Depends(get_db),
):
    resolved_provider = resolve_provider_for_method("card")
    if resolved_provider != "mercadopago":
        raise HTTPException(
            status_code=409,
            detail="El método card ya no está asignado a Mercado Pago",
        )
    _log_checkout_attempt(
        flow="guest",
        order_id=None,
        customer_email=payload.customer_email,
        customer_tax_id=payload.customer_tax_id,
        customer_address=payload.customer_address,
        items_count=len(payload.items or []),
        payer_input=payload.payer,
    )
    order = _create_guest_order(db, payload)
    order_access_token = _build_guest_order_access_token(order)
    provider = _get_mercadopago_provider()
    return provider.create_checkout(
        db,
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
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="MERCADOPAGO_WEBHOOK_SECRET no configurado")
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
        provider = _get_mercadopago_provider()
        return provider.process_webhook(db, event_type=event_type, data_id=data_id)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error procesando webhook Mercado Pago")
        raise HTTPException(status_code=500, detail="No se pudo procesar webhook") from exc

    return {"ok": True, "ignored": event_type or "unknown"}


@router.get("/orders/{order_id}/status", response_model=schemas.WebMercadoPagoOrderPaymentStatusResponse)
def get_mercadopago_order_status(
    order_id: int,
    payment: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    provider = _get_mercadopago_provider()
    order = provider.refresh_order_status(db, order)
    order = _apply_checkout_result_hint(db, order, payment_hint=payment)
    return _build_status_response(order)


@router.get("/guest/orders/{order_id}/status", response_model=schemas.WebMercadoPagoOrderPaymentStatusResponse)
def get_guest_mercadopago_order_status(
    order_id: int,
    access_token: str,
    payment: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _require_guest_order_access_token(order_id, access_token)
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    provider = _get_mercadopago_provider()
    order = provider.refresh_order_status(db, order)
    order = _apply_checkout_result_hint(db, order, payment_hint=payment)
    return _build_status_response(order)
