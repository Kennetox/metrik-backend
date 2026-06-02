from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, List, Optional, Literal
import base64
import os
import unicodedata
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    Query,
    Response,
)
from sqlalchemy.orm import Session
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import schemas, crud, models
from database import get_db
from dependencies import (
    get_current_active_user,
    require_any_permission,
    require_permission,
    require_role,
)
from services import email as email_service
from services import pdf_utils
from services import permissions as permission_service
from services import ticket_renderer
from services import storage
from services import legacy_imports
from services.password_reset import (
    PASSWORD_RESET_TOKEN_TTL_SECONDS,
    build_reset_link,
    generate_token_and_expiry,
)
from security import verify_access_token


router = APIRouter(
    prefix="/pos",
    tags=["pos"],
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
CHECKOUT_CONTEXT_NOTE_MARKER = "CHECKOUT_CONTEXT_JSON:"


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()


def _sanitize_sale_notes_for_display(notes: Optional[str]) -> str:
    raw_notes = (notes or "").strip()
    if not raw_notes:
        return ""
    marker_index = raw_notes.find(CHECKOUT_CONTEXT_NOTE_MARKER)
    if marker_index >= 0:
        raw_notes = raw_notes[:marker_index].strip()
    return raw_notes


def _sale_contains_required_reason_product(sale_in: schemas.SaleCreate) -> bool:
    for item in sale_in.items or []:
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


def _smtp_settings_dict(settings: models.PosSettings) -> dict:
    return {
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_user": settings.smtp_user,
        "smtp_password": settings.smtp_password,
        "smtp_use_tls": settings.smtp_use_tls,
        "email_from": settings.email_from,
        "company_name": settings.company_name,
    }


def _payment_method_labels_by_slug(
    db: Session,
    tenant_id: Optional[int],
) -> dict[str, str]:
    payment_methods = crud.list_payment_methods(db, tenant_id=tenant_id)
    return {
        (method.slug or "").strip().lower(): method.name.strip()
        for method in payment_methods
        if (method.slug or "").strip() and (method.name or "").strip()
    }


def _format_money_cop(value: float | int | None) -> str:
    amount = float(value or 0.0)
    formatted = f"{amount:,.2f}"
    formatted = formatted.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"${formatted}"


def _format_datetime_bogota(dt_value: Optional[datetime]) -> str:
    if not dt_value:
        return "-"
    bogota_tz = ZoneInfo("America/Bogota")
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(bogota_tz).strftime("%d/%m/%Y %H:%M")


def _payment_method_label_es(method: Optional[str]) -> str:
    normalized = (method or "").strip().lower()
    mapping = {
        "cash": "Efectivo",
        "efectivo": "Efectivo",
        "card": "Tarjeta",
        "tarjeta": "Tarjeta",
        "credit_card": "Tarjeta",
        "debit_card": "Tarjeta débito",
        "debit": "Tarjeta débito",
        "qr": "QR",
        "nequi": "Nequi",
        "daviplata": "Daviplata",
        "transfer": "Transferencia",
        "transferencia": "Transferencia",
        "bank_transfer": "Transferencia",
        "mixed": "Pago mixto",
    }
    if normalized in mapping:
        return mapping[normalized]
    return method.strip().title() if method else "Método"


def _resolve_payment_method_label(
    method: Optional[str],
    custom_labels: Optional[dict[str, str]] = None,
) -> str:
    normalized = (method or "").strip().lower()
    if custom_labels and normalized and normalized in custom_labels:
        return custom_labels[normalized]
    return _payment_method_label_es(method)


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
    logo_url = _resolve_email_asset_url(
        settings.ticket_logo_url or settings.logo_url
    )
    company_name = (settings.company_name or "Kensar Electronic").strip()
    company_address = (settings.address or "").strip()
    company_phone = (settings.contact_phone or "").strip()
    company_tax_id = (settings.tax_id or "").strip()

    info_lines: List[str] = [
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


def _build_sale_email_body(
    sale: models.Sale,
    document_type: Literal["ticket", "invoice"],
    send_both_documents: bool = False,
    message: Optional[str] = None,
    settings: Optional[models.PosSettings] = None,
    payment_labels: Optional[dict[str, str]] = None,
) -> str:
    document_label = "Factura" if document_type == "invoice" else "Ticket"
    document_number = sale.document_number or f"V-{sale.id:06d}"
    sale_number = sale.sale_number or sale.id
    customer_name = sale.customer_name or "Cliente final"
    vendor_name = sale.vendor_name or "No definido"
    customer_tax_id = (sale.customer_tax_id or "").strip()
    customer_phone = (sale.customer_phone or "").strip()
    customer_email = (sale.customer_email or "").strip()
    customer_address = (sale.customer_address or "").strip()

    payments = list(sale.payments or [])
    payment_lines: List[str] = []
    if payments:
        for payment in payments:
            method_label = _resolve_payment_method_label(
                payment.method,
                payment_labels,
            )
            payment_lines.append(
                f"<li><strong>{escape(method_label)}:</strong> "
                f"{_format_money_cop(payment.amount)}</li>"
            )
    else:
        fallback_payment_method = _resolve_payment_method_label(
            sale.payment_method,
            payment_labels,
        )
        payment_lines.append(
            f"<li><strong>{escape(fallback_payment_method)}:</strong> "
            f"{_format_money_cop(sale.paid_amount)}</li>"
        )

    cart_discount_value = float(sale.cart_discount_value or 0.0)
    item_discount_value = sum(
        float(item.line_discount_value or 0.0) for item in (sale.items or [])
    )
    if item_discount_value <= 0:
        item_discount_value = sum(float(item.discount or 0.0) for item in (sale.items or []))
    surcharge_value = float(sale.surcharge_amount or 0.0)
    total_value = float(sale.total or 0.0)
    paid_value = float(sale.paid_amount or 0.0)
    change_value = float(sale.change_amount or 0.0)
    net_value = max(0.0, paid_value - change_value)

    optional_message = (
        f"<p><strong>Mensaje:</strong> {escape(message)}</p>" if message else ""
    )
    surcharge_row = (
        f"<tr><td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:left;'>{escape(sale.surcharge_label or 'Recargo')}</td>"
        f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(surcharge_value)}</td></tr>"
        if surcharge_value > 0
        else ""
    )
    total_discount_value = cart_discount_value + item_discount_value
    cart_discount_row = (
        f"<tr><td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:left;'>Descuento carrito</td>"
        f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(cart_discount_value)}</td></tr>"
        if cart_discount_value > 0
        else ""
    )
    item_discount_row = (
        f"<tr><td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:left;'>Descuento artículos</td>"
        f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(item_discount_value)}</td></tr>"
        if item_discount_value > 0
        else ""
    )
    discounts_total_row = (
        f"<tr><td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:left;'>Descuentos</td>"
        f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(total_discount_value)}</td></tr>"
        if total_discount_value > 0
        else ""
    )
    product_rows: List[str] = []
    for item in list(sale.items or []):
        quantity = float(item.quantity or 0.0)
        unit_price = float(item.unit_price_original or item.unit_price or 0.0)
        line_discount = float(item.line_discount_value or item.discount or 0.0)
        line_total = float(item.total or 0.0)
        line_discount_label = _format_money_cop(line_discount) if line_discount > 0 else "-"
        product_rows.append(
            "<tr>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:left; max-width:380px; word-break:break-word;'>{escape(item.product_name or 'Producto')}</td>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{quantity:g}</td>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(unit_price)}</td>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{line_discount_label}</td>"
            f"<td style='padding:6px 8px; border:1px solid #e5e7eb; text-align:right;'>{_format_money_cop(line_total)}</td>"
            "</tr>"
        )
    if not product_rows:
        product_rows.append(
            "<tr>"
            "<td colspan='5' style='padding:8px; border:1px solid #e5e7eb; text-align:center; color:#6b7280;'>"
            "Sin productos registrados"
            "</td>"
            "</tr>"
        )
    customer_tax_id_row = (
        f"<strong>NIT / ID:</strong> {escape(customer_tax_id)}<br/>"
        if customer_tax_id
        else ""
    )
    customer_phone_row = (
        f"<strong>Teléfono:</strong> {escape(customer_phone)}<br/>"
        if customer_phone
        else ""
    )
    customer_email_row = (
        f"<strong>Correo:</strong> {escape(customer_email)}<br/>"
        if customer_email
        else ""
    )
    customer_address_row = (
        f"<strong>Dirección:</strong> {escape(customer_address)}<br/>"
        if customer_address
        else ""
    )
    sale_notes = _sanitize_sale_notes_for_display(sale.notes)
    sale_notes_block = (
        "<p style='margin: 0 0 14px;'>"
        "<strong>Notas de la venta</strong><br/>"
        f"<span style='white-space: pre-wrap;'>{escape(sale_notes)}</span>"
        "</p>"
        if sale_notes
        else ""
    )
    logo_footer = _build_email_logo_footer(settings)
    attachment_line = (
        "Adjunto encontrarás el PDF de tu ticket y tu factura de venta."
        if send_both_documents
        else f"Adjunto encontrarás el PDF de tu {document_label.lower()} de venta."
    )

    return f"""
<div style="font-family: Arial, sans-serif; font-size: 16px; line-height: 1.45; color: #111827;">
  <p>Hola,</p>
  <p>Te compartimos el resumen de tu compra.</p>
  <p>{attachment_line}</p>
  {optional_message}
  <p>
    <strong>{document_label}:</strong> {escape(document_number)}<br/>
    <strong>No. venta:</strong> {sale_number}<br/>
    <strong>Fecha:</strong> {_format_datetime_bogota(sale.created_at)}<br/>
    <strong>Cliente:</strong> {escape(customer_name)}<br/>
    {customer_tax_id_row}
    {customer_phone_row}
    {customer_email_row}
    {customer_address_row}
    <strong>Vendedor:</strong> {escape(vendor_name)}
  </p>
  {sale_notes_block}
  <p><strong>Resumen de valores</strong></p>
  <table style="border-collapse: collapse; width: 100%; max-width: 420px; margin-bottom: 14px;">
    <tbody>
      <tr>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:left;">Total venta</td>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">{_format_money_cop(total_value)}</td>
      </tr>
      {discounts_total_row}
      {cart_discount_row}
      {item_discount_row}
      {surcharge_row}
      <tr>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:left;">Pagado</td>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">{_format_money_cop(paid_value)}</td>
      </tr>
      <tr>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:left;">Cambio</td>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">{_format_money_cop(change_value)}</td>
      </tr>
      <tr>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:left;"><strong>Neto</strong></td>
        <td style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;"><strong>{_format_money_cop(net_value)}</strong></td>
      </tr>
    </tbody>
  </table>

  <p><strong>Productos</strong></p>
  <table style="border-collapse: collapse; width: 100%; max-width: 760px; margin-bottom: 12px;">
    <thead>
      <tr>
        <th style="padding:6px 8px; border:1px solid #e5e7eb; text-align:left;">Nombre</th>
        <th style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">Cantidad</th>
        <th style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">Precio</th>
        <th style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">Descuento</th>
        <th style="padding:6px 8px; border:1px solid #e5e7eb; text-align:right;">Total</th>
      </tr>
    </thead>
    <tbody>
      {"".join(product_rows)}
    </tbody>
  </table>
  <p><strong>Pagos registrados</strong></p>
  <ul>
    {"".join(payment_lines)}
  </ul>
  <p>Si tienes dudas, responde este correo.</p>
  <p>Gracias por tu compra.</p>
  {logo_footer}
</div>
"""


def _load_qz_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise HTTPException(
            status_code=500,
            detail=f"Falta configurar {name} en el servidor.",
        )
    value = value.replace("\\n", "\n")
    if "-----BEGIN" not in value:
        try:
            value = base64.b64decode(value).decode("utf-8")
        except Exception:
            pass
    return value


def _get_qz_cert() -> str:
    return _load_qz_env("QZ_CERT")


def _get_qz_private_key() -> str:
    return _load_qz_env("QZ_PRIVATE_KEY")


def _get_qz_signature_hash() -> hashes.HashAlgorithm:
    algo = os.getenv("QZ_SIGNATURE_ALGO", "sha256").strip().lower()
    if algo in ("sha1", "sha-1"):
        return hashes.SHA1()
    if algo in ("sha256", "sha-256"):
        return hashes.SHA256()
    raise HTTPException(
        status_code=500,
        detail=f"Algoritmo de firma QZ invalido: {algo}. Usa sha256 o sha1.",
    )


def _sign_qz_payload(payload: str) -> str:
    private_key_pem = _get_qz_private_key()
    try:
        hash_algo = _get_qz_signature_hash()
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        signature = private_key.sign(
            payload.encode("utf-8"),
            padding.PKCS1v15(),
            hash_algo,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo firmar el reto de QZ: {exc}",
        ) from exc
    return base64.b64encode(signature).decode("utf-8")


def _to_bogota_date(dt: datetime) -> datetime.date:
    bogota_tz = ZoneInfo("America/Bogota")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(bogota_tz).date()


def _bogota_today_utc_bounds_naive() -> tuple[datetime, datetime]:
    bogota_tz = ZoneInfo("America/Bogota")
    now_bogota = datetime.now(bogota_tz)
    start_bogota = datetime(
        now_bogota.year,
        now_bogota.month,
        now_bogota.day,
        tzinfo=bogota_tz,
    )
    end_bogota = start_bogota + timedelta(days=1)
    start_utc = start_bogota.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_bogota.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _bogota_date_utc_bounds_naive(value: date) -> tuple[datetime, datetime]:
    bogota_tz = ZoneInfo("America/Bogota")
    start_bogota = datetime(value.year, value.month, value.day, tzinfo=bogota_tz)
    end_bogota = start_bogota + timedelta(days=1)
    start_utc = start_bogota.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_bogota.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _sum_payments(payments: list[tuple[str, float]]) -> float:
    return sum(amount for _, amount in payments)


def _station_to_read(station: models.PosStation) -> schemas.PosStationRead:
    email = station.station_email if station else None
    return schemas.PosStationRead(
        id=station.id,
        label=station.label,
        station_type=(station.station_type or "desktop"),
        parent_station_id=station.parent_station_id,
        parent_station_label=(
            station.parent_station.label
            if getattr(station, "parent_station", None)
            else None
        ),
        station_email=email,
        is_active=bool(station.is_active),
        last_login_at=station.last_login_at,
        bound_device_id=station.bound_device_id,
        bound_device_label=station.bound_device_label,
        bound_at=station.bound_at,
        bound_by_user_id=station.bound_by_user_id,
        bound_by_user_name=station.bound_by_user_name,
        printer_mode=station.printer_mode,
        printer_name=station.printer_name,
        printer_width=station.printer_width,
        printer_auto_open_drawer=station.printer_auto_open_drawer,
        printer_show_drawer_button=station.printer_show_drawer_button,
        created_at=station.created_at,
        updated_at=station.updated_at,
    )


def _station_to_response(
    station: models.PosStation,
    pin_plain: Optional[str] = None,
) -> schemas.PosStationResponse:
    data = _station_to_read(station).model_dump()
    data["pin_plain"] = pin_plain
    return schemas.PosStationResponse(**data)


def _station_printer_config(
    station: models.PosStation,
) -> schemas.PosStationPrinterConfigRead:
    return schemas.PosStationPrinterConfigRead(
        printer_mode=station.printer_mode,
        printer_name=station.printer_name,
        printer_width=station.printer_width,
        printer_auto_open_drawer=station.printer_auto_open_drawer,
        printer_show_drawer_button=station.printer_show_drawer_button,
    )


def _serialize_sale_response(sale: models.Sale) -> schemas.SaleRead:
    sale_schema = schemas.SaleRead.model_validate(sale)
    updates = {}

    order = getattr(sale, "separated_order", None)
    if order:
        updates["is_separated"] = True
        order_total = float(order.total_amount or sale_schema.total or 0.0)
        updates["total"] = order_total
        updates["balance"] = float(order.balance or 0.0)
        updates["initial_payment_amount"] = float(
            order.initial_payment
            or sale_schema.initial_payment_amount
            or 0.0
        )
        updates["initial_payment_method"] = (
            sale.initial_payment_method or sale_schema.initial_payment_method
        )
        cart_value = float(sale.cart_discount_value or 0.0)
        cart_percent = float(sale.cart_discount_percent or 0.0)
        if abs(cart_value - updates["balance"]) < 0.01:
            cart_value = 0.0
            cart_percent = 0.0
        updates["cart_discount_value"] = cart_value
        updates["cart_discount_percent"] = cart_percent
    else:
        updates["balance"] = None
        updates["initial_payment_method"] = (
            sale.initial_payment_method or sale_schema.initial_payment_method
        )
        updates["initial_payment_amount"] = (
            sale.initial_payment_amount or sale_schema.initial_payment_amount
        )

    if not order:
        updates.setdefault("total", sale_schema.total)

    cash_methods = {"cash", "efectivo"}
    has_cash_payment = False
    for payment in getattr(sale, "payments", []) or []:
        method = (payment.method or "").strip().lower()
        if method in cash_methods and float(payment.amount or 0.0) > 0:
            has_cash_payment = True
            break
    if not has_cash_payment:
        method = (sale.main_payment_method or sale.payment_method or "").strip().lower()
        if method in cash_methods and float(sale.paid_amount or 0.0) > 0:
            has_cash_payment = True
    updates["has_cash_payment"] = has_cash_payment

    updates["source_system"] = "metrik"
    updates["is_imported"] = False
    return sale_schema.model_copy(update=updates)


def _serialize_sales_with_adjustments(
    db: Session,
    *,
    sales: list[models.Sale],
    tenant_id: int | None,
    include_adjustments: bool,
) -> list[schemas.SaleRead]:
    base_rows = [_serialize_sale_response(sale) for sale in sales]
    if not include_adjustments or not sales:
        return base_rows

    sale_ids = [sale.id for sale in sales if sale.id is not None]
    latest_payment_adjustment, total_delta_by_sale = crud._collect_sale_adjustments(
        db,
        sale_ids,
        tenant_id=tenant_id,
    )
    if not latest_payment_adjustment and not total_delta_by_sale:
        return base_rows

    out: list[schemas.SaleRead] = []
    for row in base_rows:
        updates: dict[str, Any] = {}
        total_delta = float(total_delta_by_sale.get(row.id, 0.0))
        if abs(total_delta) > 0.0001:
            base_total = float(row.total or row.paid_amount or 0.0)
            updates["total"] = max(0.0, base_total + total_delta)

        adjustment = latest_payment_adjustment.get(row.id)
        if adjustment:
            adjusted_payments = crud._parse_adjustment_payments(adjustment.payload)
            if adjusted_payments:
                adjusted_paid_amount = _sum_payments(adjusted_payments)
                updates["paid_amount"] = adjusted_paid_amount
                updates["payment_method"] = (
                    adjusted_payments[0][0] if adjusted_payments[0][0] else row.payment_method
                )
                updates["payments"] = [
                    schemas.SalePaymentRead(
                        id=max(1, idx + 1),
                        method=method or "unknown",
                        amount=float(amount or 0.0),
                    )
                    for idx, (method, amount) in enumerate(adjusted_payments)
                ]

        if updates:
            merged_payload = row.model_dump()
            merged_payload.update(updates)
            out.append(schemas.SaleRead.model_validate(merged_payload))
        else:
            out.append(row)
    return out


def _build_unified_sales_page(
    *,
    metrik_sales: list[models.Sale] | list[schemas.SaleRead],
    legacy_rows: list[dict[str, Any]],
    skip: int,
    limit: int,
) -> list[schemas.SaleRead]:
    payload: list[dict[str, Any]] = []
    for sale in metrik_sales:
        sale_row = sale.model_dump() if isinstance(sale, schemas.SaleRead) else _serialize_sale_response(sale).model_dump()
        payload.append(sale_row)
    payload.extend(legacy_rows)
    payload.sort(key=lambda row: row.get("created_at") or datetime.min, reverse=True)
    page = payload[skip : skip + limit]
    return [schemas.SaleRead.model_validate(row) for row in page]


@router.get(
    "/payment-methods",
    response_model=List[schemas.PaymentMethodRead],
)
def list_payment_methods(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.payment_methods.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    methods = crud.list_payment_methods(db, tenant_id=tenant_id)
    return methods


@router.post(
    "/payment-methods",
    response_model=schemas.PaymentMethodRead,
    status_code=201,
)
def create_payment_method(
    payload: schemas.PaymentMethodCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    try:
        return crud.create_payment_method(db, payload, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/payment-methods/{method_id}",
    response_model=schemas.PaymentMethodRead,
)
def update_payment_method(
    method_id: int,
    payload: schemas.PaymentMethodUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    method = crud.get_payment_method(db, method_id, tenant_id=tenant_id)
    if not method or method.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Método no encontrado")
    try:
        return crud.update_payment_method(db, method, payload, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch(
    "/payment-methods/{method_id}/toggle",
    response_model=schemas.PaymentMethodRead,
)
def toggle_payment_method(
    method_id: int,
    payload: schemas.PaymentMethodToggleRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    method = crud.get_payment_method(db, method_id, tenant_id=tenant_id)
    if not method or method.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Método no encontrado")
    try:
        return crud.toggle_payment_method(
            db,
            method,
            payload.is_active,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch(
    "/payment-methods/reorder",
    response_model=List[schemas.PaymentMethodRead],
)
def reorder_payment_methods(
    payload: schemas.PaymentMethodReorderRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Debes enviar la nueva orden")
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    try:
        updated = crud.reorder_payment_methods(db, payload.items, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.delete("/payment-methods/{method_id}", status_code=204)
def delete_payment_method(
    method_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    method = crud.get_payment_method(db, method_id, tenant_id=tenant_id)
    if not method or method.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Método no encontrado")
    try:
        crud.delete_payment_method(db, method, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get(
    "/sales/next-number",
    response_model=schemas.NextSaleNumberResponse,
)
def get_next_sale_number(
    pos_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    """Devuelve el siguiente consecutivo disponible."""

    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    next_number = crud.get_next_sale_number(db, pos_id=pos_id, tenant_id=tenant_id)
    return schemas.NextSaleNumberResponse(next_sale_number=next_number)


@router.post(
    "/sales/reserve-number",
    response_model=schemas.SaleNumberReservationResponse,
    status_code=201,
)
def reserve_sale_number(
    payload: schemas.SaleNumberReservationRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    """Reserva un consecutivo para una venta nueva."""
    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        reservation = crud.reserve_sale_number(
            db,
            pos_name=payload.pos_name,
            station_id=payload.station_id,
            reserved_by_user_id=current_user.id,
            min_sale_number=payload.min_sale_number,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.SaleNumberReservationResponse(
        reservation_id=reservation.id,
        sale_number=reservation.sale_number,
        document_number=reservation.document_number,
        status=reservation.status,
    )


@router.post(
    "/sales/reservations/{reservation_id}/cancel",
    response_model=schemas.SaleNumberReservationResponse,
)
def cancel_sale_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        reservation = crud.cancel_sale_reservation(db, reservation_id, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.SaleNumberReservationResponse(
        reservation_id=reservation.id,
        sale_number=reservation.sale_number,
        document_number=reservation.document_number,
        status=reservation.status,
    )


@router.post("/sales", response_model=schemas.SaleRead, status_code=201)
def create_sale(
    sale_in: schemas.SaleCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    """
    Crea una venta en el POS.

    - Debe tener al menos un ítem.
    - paid_amount no puede ser negativo.
    - El detalle de cómo se calculan los totales y se guardan
      los SaleItem y SalePayment está en crud.create_sale.
    """
    if not sale_in.items or len(sale_in.items) == 0:
        raise HTTPException(
            status_code=400,
            detail="La venta debe tener al menos un ítem",
        )

    if sale_in.paid_amount < 0:
        raise HTTPException(
            status_code=400,
            detail="paid_amount no puede ser negativo",
        )
    if (
        FREE_SALE_REASON_REQUIRED
        and _sale_contains_required_reason_product(sale_in)
        and not _has_required_sale_reason(
        sale_in.notes
        )
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
            sale_in,
            created_by_user_id=current_user.id,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "ticket" in message.lower() and "existe" in message.lower() else 400
        raise HTTPException(status_code=status_code, detail=message) from exc

    return _serialize_sale_response(sale)


@router.get("/sales", response_model=List[schemas.SaleRead])
def list_sales(
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    source: str = Query(default="all"),
    include_adjustments: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.sales", "sales_history.view", "reports.view")
    ),
):
    """
    Lista las ventas registradas en el POS.
    Más adelante se puede ampliar con filtros, paginación real, etc.
    """
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
    can_view_history_range = permission_service.role_has_permission(
        matrix, "sales_history.history", current_user.role
    )
    if not can_view_history_range:
        date_from, date_to = _bogota_today_utc_bounds_naive()

    normalized_source = (source or "all").strip().lower()
    include_metrik = normalized_source in {"all", "metrik"}
    include_legacy = normalized_source in {"all", "aronium", "legacy"}
    if normalized_source not in {"all", "metrik", "aronium", "legacy"}:
        raise HTTPException(status_code=400, detail="Filtro source inválido")

    metrik_sales: list[schemas.SaleRead] = []
    legacy_rows: list[dict[str, Any]] = []
    # Bounded window to avoid materializing massive datasets in memory.
    window_size = max(1, min(5000, skip + limit))

    if include_metrik:
        metrik_sales_raw = crud.get_sales(
            db,
            skip=0,
            limit=window_size,
            date_from=date_from,
            date_to=date_to,
            tenant_id=tenant_id,
        )
        metrik_sales = _serialize_sales_with_adjustments(
            db,
            sales=metrik_sales_raw,
            tenant_id=tenant_id,
            include_adjustments=include_adjustments,
        )
    if include_legacy:
        legacy_rows = legacy_imports.map_legacy_sales_to_report_rows(
            db,
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
            offset=0,
            limit=window_size,
        )

    return _build_unified_sales_page(
        metrik_sales=metrik_sales,
        legacy_rows=legacy_rows,
        skip=skip,
        limit=limit,
    )


@router.get("/sales/history", response_model=schemas.SalesHistoryPage)
def list_sales_history(
    skip: int = 0,
    limit: int = Query(default=100, ge=1, le=500),
    from_date: Optional[date] = Query(default=None, alias="date_from"),
    to_date: Optional[date] = Query(default=None, alias="date_to"),
    term: Optional[str] = None,
    customer: Optional[str] = None,
    payment_method: Optional[str] = None,
    pos: Optional[str] = None,
    source: str = Query(default="all"),
    include_adjustments: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.sales", "sales_history.view")
    ),
):
    date_from = None
    date_to = None
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
    can_view_history_range = permission_service.role_has_permission(
        matrix, "sales_history.history", current_user.role
    )
    if can_view_history_range:
        if from_date is not None:
            date_from, _ = _bogota_date_utc_bounds_naive(from_date)
        if to_date is not None:
            _, date_to = _bogota_date_utc_bounds_naive(to_date)
    else:
        date_from, date_to = _bogota_today_utc_bounds_naive()

    normalized_source = (source or "all").strip().lower()
    include_metrik = normalized_source in {"all", "metrik"}
    include_legacy = normalized_source in {"all", "aronium", "legacy"}
    if normalized_source not in {"all", "metrik", "aronium", "legacy"}:
        raise HTTPException(status_code=400, detail="Filtro source inválido")

    sales, total = crud.get_sales_history_page(
        db,
        skip=0,
        limit=max(1, min(5000, skip + limit)),
        date_from=date_from,
        date_to=date_to,
        term=term,
        customer=customer,
        payment_method=payment_method,
        pos_name=pos,
        tenant_id=tenant_id,
    )
    if not include_metrik:
        sales = []
        total = 0
    else:
        sales = _serialize_sales_with_adjustments(
            db,
            sales=sales,
            tenant_id=tenant_id,
            include_adjustments=include_adjustments,
        )

    legacy_rows: list[dict[str, Any]] = []
    legacy_total = 0
    if include_legacy:
        legacy_rows, legacy_total = legacy_imports.get_legacy_sales_history_page(
            db,
            skip=0,
            limit=max(1, min(5000, skip + limit)),
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
            term=term,
            customer=customer,
            payment_method=payment_method,
            pos_name=pos,
        )

    merged_page = _build_unified_sales_page(
        metrik_sales=sales,
        legacy_rows=legacy_rows,
        skip=skip,
        limit=limit,
    )
    total_unified = int((total if include_metrik else 0) + (legacy_total if include_legacy else 0))
    return schemas.SalesHistoryPage(
        total=total_unified,
        skip=skip,
        limit=limit,
        items=merged_page,
    )


@router.get("/sales/{sale_id}", response_model=schemas.SaleRead)
def get_sale(
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.sales", "sales_history.view", "reports.view")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale = crud.get_sale(db, sale_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return _serialize_sale_response(sale)


@router.post(
    "/sales/{sale_id}/void",
    response_model=schemas.SaleVoidResponse,
)
def void_sale(
    sale_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("documents.sales.void")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale = crud.get_sale(db, sale_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    try:
        sale = crud.void_sale(db, sale, current_user, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.refresh(sale)
    return schemas.SaleVoidResponse(
        sale=_serialize_sale_response(sale),
        return_document=None,
    )


@router.post(
    "/documents/{doc_type}/{doc_id}/adjust",
    response_model=schemas.DocumentAdjustmentRead,
    status_code=201,
)
def create_document_adjustment(
    doc_type: str,
    doc_id: int,
    payload: schemas.DocumentAdjustmentCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("documents.sales.adjust")),
):
    if doc_type != "sale":
        raise HTTPException(status_code=400, detail="Tipo de documento no soportado")

    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale = crud.get_sale(db, doc_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    if sale.status == "voided":
        raise HTTPException(status_code=400, detail="La venta está anulada")

    if _to_bogota_date(sale.created_at) != _to_bogota_date(
        datetime.utcnow().replace(tzinfo=timezone.utc)
    ):
        raise HTTPException(
            status_code=400,
            detail="Los ajustes solo se permiten el mismo día de la venta",
        )

    total_delta = float(payload.total_delta or 0.0)
    payment_delta = float(payload.payment_delta or 0.0)
    adjustment_type = payload.adjustment_type
    latest_payment_adjustment, total_delta_by_sale = crud._collect_sale_adjustments(
        db,
        [sale.id],
        tenant_id=tenant_id,
    )
    current_effective_total = max(
        0.0,
        float(sale.total or 0.0) + float(total_delta_by_sale.get(sale.id, 0.0)),
    )
    payload_payments = payload.payload.get("payments") if payload.payload else None
    payments_list: list[tuple[str, float]] = []
    if isinstance(payload_payments, list):
        for entry in payload_payments:
            if not isinstance(entry, dict):
                continue
            method = entry.get("method")
            amount = entry.get("amount")
            if not isinstance(method, str) or not method:
                continue
            try:
                numeric = float(amount or 0.0)
            except (TypeError, ValueError):
                continue
            payments_list.append((method, numeric))

    if adjustment_type == "note":
        if abs(total_delta) > 0.01 or abs(payment_delta) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="Las notas no pueden modificar valores de pago o total",
            )
        if not payload.payload or not payload.payload.get("note"):
            raise HTTPException(
                status_code=400,
                detail="La nota es obligatoria para este ajuste",
            )
    elif adjustment_type == "payment":
        if abs(total_delta) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="El ajuste de pagos no puede modificar el total del documento",
            )
        if current_effective_total <= 0:
            raise HTTPException(
                status_code=400,
                detail="Este documento tiene total en cero. Debes corregir el total antes de ajustar pagos.",
            )
        if not payments_list:
            raise HTTPException(
                status_code=400,
                detail="Debes registrar al menos un pago ajustado.",
            )
    elif adjustment_type == "discount":
        if abs(total_delta - payment_delta) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="El descuento debe cuadrar con el ajuste de pagos",
            )
        if not payments_list:
            raise HTTPException(
                status_code=400,
                detail="Debes ajustar los pagos junto con el descuento",
            )
    elif adjustment_type == "total":
        if abs(payment_delta) > 0.01 or payments_list:
            raise HTTPException(
                status_code=400,
                detail="El ajuste de total no puede modificar pagos",
            )
        next_total = current_effective_total + total_delta
        if next_total < 0:
            raise HTTPException(
                status_code=400,
                detail="El total ajustado no puede ser negativo",
            )
    if payments_list:
        latest_adjustment = latest_payment_adjustment.get(sale.id)
        adjusted_payments = (
            crud._parse_adjustment_payments(latest_adjustment.payload)
            if latest_adjustment
            else []
        )
        original_total = (
            _sum_payments(adjusted_payments)
            if adjusted_payments
            else sum(float(p.amount or 0.0) for p in sale.payments)
        )
        if original_total <= 0:
            original_total = float(sale.paid_amount or sale.total or 0.0)
        expected_total = original_total + payment_delta
        adjusted_total = _sum_payments(payments_list)
        if abs(adjusted_total - expected_total) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="El total de pagos ajustados no cuadra con el ajuste",
            )

    is_post_closure = sale.closure_id is not None
    adjustment = crud.create_document_adjustment(
        db,
        doc_type=doc_type,
        doc_id=doc_id,
        adjustment_type=adjustment_type,
        reason=payload.reason,
        payload=payload.payload or {},
        total_delta=total_delta,
        payment_delta=payment_delta,
        is_post_closure=is_post_closure,
        original_closure_id=sale.closure_id,
        user=current_user,
        tenant_id=sale.tenant_id if sale.tenant_id is not None else tenant_id,
    )
    return adjustment


@router.get(
    "/documents/{doc_type}/{doc_id}/adjustments",
    response_model=List[schemas.DocumentAdjustmentRead],
)
def list_document_adjustments(
    doc_type: str,
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.sales", "sales_history.view", "reports.view")
    ),
):
    if doc_type != "sale":
        raise HTTPException(status_code=400, detail="Tipo de documento no soportado")
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale = crud.get_sale(db, doc_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return crud.list_document_adjustments(
        db,
        doc_type=doc_type,
        doc_id=doc_id,
        tenant_id=tenant_id,
    )


@router.get(
    "/documents/adjustments",
    response_model=List[schemas.DocumentAdjustmentRead],
)
def list_document_adjustments_bulk(
    doc_type: str,
    doc_ids: str,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.sales", "sales_history.view", "reports.view")
    ),
):
    if doc_type != "sale":
        raise HTTPException(status_code=400, detail="Tipo de documento no soportado")
    try:
        ids = [int(item) for item in doc_ids.split(",") if item.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="doc_ids inválidos") from exc
    if not ids:
        return []
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    return crud.list_document_adjustments_for_docs(
        db,
        doc_type=doc_type,
        doc_ids=ids,
        tenant_id=tenant_id,
    )


@router.post("/returns", response_model=schemas.SaleReturnRead, status_code=201)
def create_return(
    return_in: schemas.SaleReturnCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.returns")),
):
    """Registra una devolución parcial o total vinculada a una venta."""

    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        sale_return = crud.create_return(
            db,
            return_in,
            tenant_id=tenant_id,
            created_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return sale_return


@router.post("/changes", response_model=schemas.SaleChangeRead, status_code=201)
def create_change(
    change_in: schemas.SaleChangeCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.returns")),
):
    """Registra un cambio de productos vinculado a una venta."""

    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        sale_change = crud.create_change(
            db,
            change_in,
            tenant_id=tenant_id,
            created_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return sale_change


@router.post(
    "/sales/{sale_id}/email",
    response_model=schemas.EmailSendResponse,
)
def email_sale_ticket(
    sale_id: int,
    email_in: schemas.EmailSendRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.sales")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale = crud.get_sale(db, sale_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    recipients = list(email_in.recipients or [])
    if not recipients:
        raise HTTPException(
            status_code=400, detail="Debe especificar al menos un destinatario"
        )

    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    payment_labels = _payment_method_labels_by_slug(db, tenant_id)
    mode = (
        ticket_renderer.INVOICE_MODE
        if email_in.document_type == "invoice"
        else ticket_renderer.THERMAL_TICKET_MODE
    )
    send_both_documents = bool(email_in.send_both_documents)
    body_html = _build_sale_email_body(
        sale=sale,
        document_type=email_in.document_type,
        send_both_documents=send_both_documents,
        message=email_in.message,
        settings=settings,
        payment_labels=payment_labels,
    )

    attachments = []
    if email_in.attach_pdf:
        if send_both_documents:
            ticket_pdf = ticket_renderer.render_sale_ticket_pdf(
                sale,
                settings=settings,
                mode=ticket_renderer.THERMAL_TICKET_MODE,
                payment_method_labels=payment_labels,
            )
            invoice_pdf = ticket_renderer.render_sale_ticket_pdf(
                sale,
                settings=settings,
                mode=ticket_renderer.INVOICE_MODE,
                payment_method_labels=payment_labels,
            )
            attachments.extend(
                [
                    (
                        f"ticket_{sale.sale_number or sale.id}.pdf",
                        ticket_pdf,
                        "application/pdf",
                    ),
                    (
                        f"factura_{sale.sale_number or sale.id}.pdf",
                        invoice_pdf,
                        "application/pdf",
                    ),
                ]
            )
        else:
            pdf_bytes = ticket_renderer.render_sale_ticket_pdf(
                sale,
                settings=settings,
                mode=mode,
                payment_method_labels=payment_labels,
            )
            filename_prefix = (
                "factura" if email_in.document_type == "invoice" else "ticket"
            )
            attachments.append(
                (
                    f"{filename_prefix}_{sale.sale_number or sale.id}.pdf",
                    pdf_bytes,
                    "application/pdf",
                )
            )

    cc = list(settings.ticket_email_cc or [])

    subject = (
        email_in.subject
        or (
            f"Factura venta #{sale.sale_number or sale.id}"
            if email_in.document_type == "invoice"
            else f"Ticket venta #{sale.sale_number or sale.id}"
        )
    )

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            html_body=body_html,
            cc=cc,
            attachments=attachments,
            smtp_config=_smtp_settings_dict(settings),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return schemas.EmailSendResponse(
        status="sent",
        document_type=email_in.document_type,
        sent_both_documents=send_both_documents,
    )


@router.get(
    "/sales/{sale_id}/document",
    response_model=schemas.SaleDocumentResponse,
)
def get_sale_document(
    sale_id: int,
    document_type: Literal["ticket", "invoice"] = "ticket",
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.sales", "sales_history.view", "reports.view")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale = crud.get_sale(db, sale_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    payment_labels = _payment_method_labels_by_slug(db, tenant_id)
    mode = (
        ticket_renderer.INVOICE_MODE
        if document_type == "invoice"
        else ticket_renderer.TICKET_MODE
    )
    document_html = ticket_renderer.render_sale_ticket_html(
        sale,
        settings=settings,
        mode=mode,
        payment_method_labels=payment_labels,
    )
    prefix = "factura" if document_type == "invoice" else "ticket"
    return schemas.SaleDocumentResponse(
        sale_id=sale.id,
        sale_number=sale.sale_number,
        document_number=sale.document_number,
        document_type=document_type,
        filename=f"{prefix}_{sale.sale_number or sale.id}.html",
        document_html=document_html,
    )


@router.get("/sales/{sale_id}/document-view", response_class=Response)
def view_sale_document(
    sale_id: int,
    document_type: Literal["ticket", "invoice"] = "ticket",
    layout: Literal["modern", "thermal"] = "modern",
    access_token: str = Query(..., min_length=10),
    db: Session = Depends(get_db),
):
    try:
        token_payload = verify_access_token(access_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    session = crud.get_session_by_token(db, access_token)
    if not session or session.revoked_at or session.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")

    user_id = int(token_payload.get("sub", 0) or 0)
    user = db.query(models.PosUser).filter(models.PosUser.id == user_id).first()
    if not user or not user.is_active or user.status != "Activo":
        raise HTTPException(status_code=401, detail="Usuario inactivo")

    tenant_id = crud.resolve_user_tenant_id(db, user)
    sale = crud.get_sale(db, sale_id, tenant_id=tenant_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    payment_labels = _payment_method_labels_by_slug(db, tenant_id)
    if document_type == "invoice":
        mode = ticket_renderer.INVOICE_MODE
    elif layout == "thermal":
        mode = ticket_renderer.THERMAL_TICKET_MODE
    else:
        mode = ticket_renderer.TICKET_MODE
    document_html = ticket_renderer.render_sale_ticket_html(
        sale,
        settings=settings,
        mode=mode,
        payment_method_labels=payment_labels,
    )
    return Response(content=document_html, media_type="text/html; charset=utf-8")


@router.get("/returns", response_model=List[schemas.SaleReturnRead])
def list_returns(
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.returns")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    returns = crud.list_returns(
        db,
        skip=skip,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
        tenant_id=tenant_id,
    )
    return returns


@router.get("/changes", response_model=List[schemas.SaleChangeRead])
def list_changes(
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.returns", "sales_history.view", "reports.view")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    changes = crud.list_changes(
        db,
        skip=skip,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
        tenant_id=tenant_id,
    )
    return changes


@router.get("/returns/{return_id}", response_model=schemas.SaleReturnRead)
def get_return(
    return_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.returns", "sales_history.view")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale_return = crud.get_sale_return(db, return_id, tenant_id=tenant_id)
    if not sale_return:
        raise HTTPException(status_code=404, detail="Devolución no encontrada")
    return sale_return


@router.post(
    "/returns/{return_id}/void",
    response_model=schemas.SaleReturnRead,
)
def void_return(
    return_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.returns.void")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale_return = crud.get_sale_return(db, return_id, tenant_id=tenant_id)
    if not sale_return:
        raise HTTPException(status_code=404, detail="Devolución no encontrada")
    try:
        updated = crud.void_return(db, sale_return, current_user, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.get("/changes/{change_id}", response_model=schemas.SaleChangeRead)
def get_change(
    change_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_any_permission("pos.returns", "sales_history.view", "reports.view")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale_change = crud.get_sale_change(db, change_id, tenant_id=tenant_id)
    if not sale_change:
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    return sale_change


@router.post(
    "/changes/{change_id}/void",
    response_model=schemas.SaleChangeRead,
)
def void_change(
    change_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.changes.void")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    sale_change = crud.get_sale_change(db, change_id, tenant_id=tenant_id)
    if not sale_change:
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    try:
        updated = crud.void_change(db, sale_change, current_user, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.get("/settings", response_model=schemas.PosSettingsRead)
def get_pos_settings(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    return settings


@router.put("/settings", response_model=schemas.PosSettingsRead)
def update_pos_settings(
    settings_in: schemas.PosSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    updated = crud.update_pos_settings(db, settings, settings_in)
    return updated


@router.post("/settings/test-email", response_model=schemas.EmailSendResponse)
def send_settings_test_email(
    payload: schemas.SmtpTestEmailRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    recipients = list(payload.recipients or [])
    if not recipients:
        raise HTTPException(status_code=400, detail="Agrega al menos un destinatario")

    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    smtp_config = {
        "smtp_host": payload.smtp_host or settings.smtp_host,
        "smtp_port": payload.smtp_port or settings.smtp_port,
        "smtp_user": payload.smtp_user or settings.smtp_user,
        "smtp_password": payload.smtp_password or settings.smtp_password,
        "smtp_use_tls": (
            payload.smtp_use_tls
            if payload.smtp_use_tls is not None
            else settings.smtp_use_tls
        ),
        "email_from": payload.email_from or settings.email_from,
        "company_name": settings.company_name,
    }
    subject = payload.subject or "Prueba de correo - Kensar POS"
    message = payload.message or "Este es un correo de prueba del POS."

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            html_body=f"<p>{escape(message)}</p>",
            smtp_config=smtp_config,
        )
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return schemas.EmailSendResponse(status="sent")


@router.post("/contact-request", response_model=schemas.ContactRequestResponse)
def send_public_contact_request(
    payload: schemas.ContactRequestCreate,
    db: Session = Depends(get_db),
):
    settings = crud.get_pos_settings(db)

    query_labels = {
        "soporte_tecnico": "Soporte técnico",
        "consulta_comercial": "Consulta comercial",
        "facturacion": "Facturación y pagos",
        "implementacion": "Implementación y configuración",
        "sugerencia": "Sugerencia / mejora",
        "otro": "Otro",
    }

    query_label = query_labels.get(payload.query_type, "Consulta")
    sender_name = (payload.sender_name or "").strip() or "No informado"
    sender_email = (payload.sender_email or "").strip() or "No informado"
    source = (payload.source or "").strip() or "web_contacto"
    message_clean = payload.message.strip()

    subject = f"[Metrik] Nueva solicitud: {query_label}"
    html_body = f"""
      <div style="font-family:Arial,Helvetica,sans-serif;color:#0f172a;line-height:1.6">
        <h2 style="margin:0 0 12px">Nueva solicitud de contacto</h2>
        <p style="margin:0 0 16px;color:#475569">Formulario web de metrikpos.com</p>
        <table style="border-collapse:collapse;width:100%;max-width:680px">
          <tr>
            <td style="border:1px solid #cbd5e1;padding:8px 10px;font-weight:600;background:#f8fafc">Tipo</td>
            <td style="border:1px solid #cbd5e1;padding:8px 10px">{escape(query_label)}</td>
          </tr>
          <tr>
            <td style="border:1px solid #cbd5e1;padding:8px 10px;font-weight:600;background:#f8fafc">Nombre</td>
            <td style="border:1px solid #cbd5e1;padding:8px 10px">{escape(sender_name)}</td>
          </tr>
          <tr>
            <td style="border:1px solid #cbd5e1;padding:8px 10px;font-weight:600;background:#f8fafc">Correo</td>
            <td style="border:1px solid #cbd5e1;padding:8px 10px">{escape(sender_email)}</td>
          </tr>
          <tr>
            <td style="border:1px solid #cbd5e1;padding:8px 10px;font-weight:600;background:#f8fafc">Origen</td>
            <td style="border:1px solid #cbd5e1;padding:8px 10px">{escape(source)}</td>
          </tr>
        </table>
        <h3 style="margin:16px 0 8px">Mensaje</h3>
        <div style="border:1px solid #cbd5e1;background:#f8fafc;padding:12px;border-radius:8px;white-space:pre-wrap">{escape(message_clean)}</div>
      </div>
    """
    text_body = (
        "Nueva solicitud de contacto\n"
        f"Tipo: {query_label}\n"
        f"Nombre: {sender_name}\n"
        f"Correo: {sender_email}\n"
        f"Origen: {source}\n\n"
        "Mensaje:\n"
        f"{message_clean}"
    )

    try:
        email_service.send_email(
            recipients=["kennethjc2301@gmail.com"],
            cc=["kensarelec@gmail.com"],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            smtp_config=_smtp_settings_dict(settings),
        )
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return schemas.ContactRequestResponse(status="sent")


@router.get("/qz/cert")
def get_qz_certificate(
    db: Session = Depends(get_db),
):
    cert = _get_qz_cert()
    return Response(content=cert, media_type="text/plain")


@router.post("/qz/sign", response_model=schemas.QzSignResponse)
def sign_qz_request(
    payload: schemas.QzSignRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    signature = _sign_qz_payload(payload.data)
    return schemas.QzSignResponse(signature=signature)


@router.post("/logo-upload", response_model=schemas.UploadLogoResponse)
@router.post("/settings/logo", response_model=schemas.UploadLogoResponse)
async def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    try:
        result = await storage.save_pos_logo(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar el logo: {exc}") from exc

    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    settings.logo_url = result.url
    settings.ticket_logo_url = result.url
    db.commit()
    db.refresh(settings)
    return schemas.UploadLogoResponse(url=result.url)


@router.get(
    "/roles/permissions",
    response_model=schemas.RolePermissionMatrix,
)
def get_role_permissions(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    modules = crud.get_role_permissions(db, tenant_id=tenant_id)
    return schemas.RolePermissionMatrix(modules=modules)


@router.put(
    "/roles/permissions",
    response_model=schemas.RolePermissionMatrix,
)
def update_role_permissions(
    payload: schemas.RolePermissionMatrix,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    modules_payload = [module.model_dump() for module in payload.modules]
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    modules = crud.update_role_permissions(db, modules_payload, tenant_id=tenant_id)
    return schemas.RolePermissionMatrix(modules=modules)


@router.get("/profile", response_model=schemas.PosUserProfileRead)
def get_profile(
    current_user: models.PosUser = Depends(get_current_active_user),
):
    return schemas.PosUserProfileRead.model_validate(current_user)


@router.patch("/profile", response_model=schemas.PosUserProfileRead)
def update_profile(
    payload: schemas.PosUserProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="El nombre es obligatorio.")
        data["name"] = name

    for field, value in data.items():
        setattr(current_user, field, value)

    db.commit()
    db.refresh(current_user)
    if current_user.employee_id:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        employee = crud.get_hr_employee(db, current_user.employee_id, tenant_id=tenant_id)
        if employee:
            employee.name = current_user.name
            employee.email = current_user.email
            employee.phone = current_user.phone
            employee.position = current_user.position
            employee.notes = current_user.notes
            employee.avatar_url = current_user.avatar_url
            employee.birth_date = current_user.birth_date
            employee.location = current_user.location
            employee.bio = current_user.bio
            db.commit()
    return schemas.PosUserProfileRead.model_validate(current_user)


@router.post("/profile/avatar", response_model=schemas.UploadAvatarResponse)
async def upload_profile_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        result = await storage.save_user_avatar(file, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar la imagen: {exc}") from exc

    current_user.avatar_url = result.url
    db.commit()
    db.refresh(current_user)
    if current_user.employee_id:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        employee = crud.get_hr_employee(db, current_user.employee_id, tenant_id=tenant_id)
        if employee:
            employee.avatar_url = current_user.avatar_url
            db.commit()
    return schemas.UploadAvatarResponse(url=result.url)


@router.get("/profile/documents", response_model=List[schemas.PosUserDocumentRead])
def list_profile_documents(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    return crud.list_user_documents(db, current_user.id, tenant_id=tenant_id)


@router.post("/profile/documents", response_model=schemas.PosUserDocumentRead, status_code=201)
async def upload_profile_document(
    file: UploadFile = File(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    existing = crud.list_user_documents(db, current_user.id, tenant_id=tenant_id)
    if len(existing) >= 10:
        raise HTTPException(status_code=400, detail="Se alcanzó el límite de 10 documentos.")
    try:
        result = await storage.save_user_document(
            file,
            current_user.id,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar el documento: {exc}") from exc

    doc = crud.create_user_document(
        db,
        user_id=current_user.id,
        file_name=result.filename,
        file_url=result.url,
        file_size=result.size,
        note=note.strip() if note else None,
        tenant_id=tenant_id,
    )
    return doc


@router.delete("/profile/documents/{doc_id}", status_code=204)
def delete_profile_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    deleted = crud.delete_user_document(
        db,
        current_user.id,
        doc_id,
        tenant_id=tenant_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return Response(status_code=204)


@router.get("/users", response_model=List[schemas.PosUserRead])
def list_pos_users(
    status: Optional[str] = None,
    role: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("users.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    users = crud.list_pos_users(db, status=status, role=role, tenant_id=tenant_id)
    return users


@router.post("/users", response_model=schemas.PosUserRead, status_code=201)
def create_pos_user(
    user_in: schemas.PosUserCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("users.manage")),
):
    try:
        tenant_id = crud.resolve_user_tenant_id(db, current_user)
        user = crud.create_pos_user(db, user_in, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user


@router.patch("/users/{user_id}", response_model=schemas.PosUserRead)
def update_pos_user(
    user_id: int,
    user_in: schemas.PosUserUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("users.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    user = crud.get_pos_user(db, user_id, tenant_id=tenant_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    try:
        updated = crud.update_pos_user(db, user, user_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.post("/users/{user_id}/invite")
def invite_pos_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("users.invite")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    user = crud.get_pos_user(db, user_id, tenant_id=tenant_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not user.email:
        raise HTTPException(status_code=400, detail="El usuario no tiene un correo configurado")

    crud.invalidate_password_reset_tokens(db, user.id)
    token, expires_at = generate_token_and_expiry()
    crud.create_password_reset_token(db, user, token, expires_at)

    user.invited_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    reset_link = build_reset_link(token)
    html_body = (
        f"<p>Hola {user.name or user.email},</p>"
        "<p>Has sido invitado a usar el POS Kensar. "
        "Haz clic en el siguiente enlace para configurar tu contraseña:</p>"
        f"<p><a href='{reset_link}' target='_blank'>Configurar contraseña</a></p>"
        f"<p>El enlace expirará en {PASSWORD_RESET_TOKEN_TTL_SECONDS // 60} minutos.</p>"
    )

    try:
        email_service.send_email(
            recipients=[user.email],
            subject="Invitación a Metrik POS",
            html_body=html_body,
            smtp_config=_smtp_settings_dict(settings),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "detail": "Enviamos una invitación al usuario",
        "expires_in": PASSWORD_RESET_TOKEN_TTL_SECONDS,
    }


@router.get(
    "/stations",
    response_model=List[schemas.PosStationRead],
)
def list_pos_stations(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    stations = crud.list_pos_stations(db, tenant_id=tenant_id)
    return [_station_to_read(station) for station in stations]


@router.post(
    "/stations",
    response_model=schemas.PosStationResponse,
    status_code=201,
)
def create_pos_station(
    payload: schemas.PosStationCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    try:
        station, pin_plain = crud.create_pos_station(db, payload, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _station_to_response(station, pin_plain)


@router.put(
    "/stations/{station_id}",
    response_model=schemas.PosStationResponse,
)
def update_pos_station(
    station_id: str,
    payload: schemas.PosStationUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    station, pin_plain = crud.update_pos_station(
        db,
        station,
        payload,
        tenant_id=tenant_id,
    )
    return _station_to_response(station, pin_plain)


@router.get(
    "/stations/{station_id}/closure-scope",
    response_model=schemas.PosClosureStationScopeRead,
)
def get_pos_station_closure_scope(
    station_id: str,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.closures")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    try:
        primary_station_id, scoped = crud.get_closure_station_scope(
            db,
            station_id,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.PosClosureStationScopeRead(
        primary_station_id=primary_station_id,
        station_ids=[item["station_id"] for item in scoped],
        stations=[
            schemas.PosClosureStationScopeItem(
                station_id=item["station_id"],
                station_label=item["station_label"],
                station_type=item["station_type"],
                is_primary=bool(item["is_primary"]),
            )
            for item in scoped
        ],
    )


def _station_notice_to_read(
    notice: models.PosStationNotice,
) -> schemas.PosStationNoticeRead:
    return schemas.PosStationNoticeRead(
        id=notice.id,
        station_id=notice.station_id,
        message=notice.message,
        created_at=notice.created_at,
        created_by_user_name=(
            notice.created_by_user.name if notice.created_by_user else None
        ),
    )


@router.post(
    "/stations/{station_id}/notice",
    response_model=schemas.PosStationNoticeRead,
)
def create_pos_station_notice(
    station_id: str,
    payload: schemas.PosStationNoticeCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    notice = crud.create_pos_station_notice(
        db,
        station=station,
        message=payload.message,
        user=current_user,
    )
    return _station_notice_to_read(notice)


@router.get(
    "/stations/{station_id}/notice",
    response_model=Optional[schemas.PosStationNoticeRead],
)
def get_pos_station_notice(
    station_id: str,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    notice = crud.get_active_pos_station_notice(db, station_id)
    if not notice:
        return None
    return _station_notice_to_read(notice)


@router.delete("/stations/{station_id}/notice/{notice_id}", status_code=204)
def dismiss_pos_station_notice(
    station_id: str,
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    notice = crud.get_pos_station_notice(db, station_id, notice_id, tenant_id=tenant_id)
    if not notice:
        raise HTTPException(status_code=404, detail="Aviso no encontrado")
    crud.dismiss_pos_station_notice(db, notice, current_user)
    return Response(status_code=204)


@router.get(
    "/stations/{station_id}/printer-config",
    response_model=schemas.PosStationPrinterConfigRead,
)
def get_station_printer_config(
    station_id: str,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    return _station_printer_config(station)


@router.put(
    "/stations/{station_id}/printer-config",
    response_model=schemas.PosStationPrinterConfigRead,
)
def update_station_printer_config(
    station_id: str,
    payload: schemas.PosStationPrinterConfigUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    station = crud.update_pos_station_printer_config(db, station, payload)
    return _station_printer_config(station)


@router.delete("/stations/{station_id}", status_code=204)
def deactivate_pos_station(
    station_id: str,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    crud.deactivate_pos_station(db, station)
    return Response(status_code=204)


@router.post(
    "/stations/{station_id}/unbind",
    response_model=schemas.PosStationRead,
)
def unbind_pos_station(
    station_id: str,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    station = crud.get_pos_station(db, station_id, tenant_id=tenant_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    station.bound_device_id = None
    station.bound_device_label = None
    station.bound_at = None
    station.bound_by_user_id = None
    station.bound_by_user_name = None
    db.commit()
    db.refresh(station)
    return _station_to_read(station)


@router.get("/customers", response_model=List[schemas.PosCustomerRead])
def list_pos_customers(
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    include_inactive: bool = False,
    include_web_customers: bool = True,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.customers")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    customers = crud.list_pos_customers(
        db,
        search=search,
        skip=skip,
        limit=limit,
        include_inactive=include_inactive,
        include_web_customers=include_web_customers,
        tenant_id=tenant_id,
    )
    return customers


@router.get("/customers/frequent", response_model=List[schemas.PosCustomerFrequentRead])
def list_pos_frequent_customers(
    min_sales: int = 5,
    limit: int = 12,
    include_web_customers: bool = True,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.customers")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    return crud.list_pos_frequent_customers(
        db,
        min_sales=min_sales,
        limit=limit,
        include_web_customers=include_web_customers,
        tenant_id=tenant_id,
    )


@router.post(
    "/customers",
    response_model=schemas.PosCustomerRead,
    status_code=201,
)
def create_pos_customer(
    customer_in: schemas.PosCustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.customers")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    customer = crud.create_pos_customer(db, customer_in, tenant_id=tenant_id)
    return customer


@router.put("/customers/{customer_id}", response_model=schemas.PosCustomerRead)
def update_pos_customer(
    customer_id: int,
    customer_in: schemas.PosCustomerUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.customers")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    customer = crud.get_pos_customer(db, customer_id, tenant_id=tenant_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    updated = crud.update_pos_customer(db, customer, customer_in)
    return updated


@router.delete("/customers/{customer_id}", status_code=204)
def delete_pos_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.customers")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    customer = crud.get_pos_customer(db, customer_id, tenant_id=tenant_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    crud.soft_delete_pos_customer(db, customer)
    return Response(status_code=204)


@router.post(
    "/closures/preview",
    response_model=schemas.PosClosurePreviewRead,
)
def preview_pos_closure(
    closure_in: schemas.PosClosureCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    try:
        preview = crud.preview_pos_closure(db, closure_in, current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return preview


@router.post(
    "/closures",
    response_model=schemas.PosClosureRead,
    status_code=201,
)
def create_pos_closure(
    closure_in: schemas.PosClosureCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    try:
        closure = crud.create_pos_closure(db, closure_in, current_user)
    except ValueError as exc:
        detail = str(exc)
        if detail == "No hay movimientos pendientes por cerrar":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return closure


@router.post(
    "/closures/{closure_id}/email",
    response_model=schemas.EmailSendResponse,
)
def email_closure_report(
    closure_id: int,
    email_in: schemas.EmailSendRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    closure = crud.get_pos_closure(db, closure_id, tenant_id=tenant_id)
    if not closure:
        raise HTTPException(status_code=404, detail="Cierre no encontrado")

    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    recipients = list(email_in.recipients or [])
    if not recipients:
        recipients = list(settings.closure_email_recipients or [])

    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="Debe indicar destinatarios o configurar correos por defecto",
        )

    closure_html = ticket_renderer.render_closure_html(closure, settings=settings)
    body_parts = []
    if email_in.message:
        body_parts.append(f"<p>{escape(email_in.message)}</p>")
    body_parts.append(closure_html)
    logo_footer = _build_email_logo_footer(settings)
    if logo_footer:
        body_parts.append(logo_footer)

    attachments = []
    if email_in.attach_pdf:
        if not pdf_utils.can_render_html_pdf():
            raise HTTPException(
                status_code=503,
                detail=(
                    "El servidor no tiene habilitada la generacion de PDF HTML "
                    "(dependencias de WeasyPrint faltantes)."
                ),
            )
        pdf_bytes = ticket_renderer.render_closure_pdf(closure, settings=settings)
        attachments.append(
            (
                f"cierre_{closure.consecutive or closure.id}.pdf",
                pdf_bytes,
                "application/pdf",
            )
        )
        if email_in.extra_html_attachments:
            for html_attachment in email_in.extra_html_attachments:
                if not html_attachment.document_html.strip():
                    continue
                filename = (html_attachment.filename or "").strip()
                if not filename:
                    continue
                if not filename.lower().endswith(".pdf"):
                    filename = f"{filename}.pdf"
                pdf_bytes_extra = pdf_utils.build_pdf_from_html(
                    html_attachment.title or filename,
                    html_attachment.document_html,
                )
                attachments.append((filename, pdf_bytes_extra, "application/pdf"))
        else:
            products_pdf = ticket_renderer.render_closure_products_detail_pdf(
                closure, settings=settings
            )
            attachments.append(
                (
                    f"productos_vendidos_detalle_{closure.consecutive or closure.id}.pdf",
                    products_pdf,
                    "application/pdf",
                )
            )
            hourly_pdf = ticket_renderer.render_closure_hourly_sales_pdf(
                closure, settings=settings
            )
            attachments.append(
                (
                    f"ventas_por_hora_{closure.consecutive or closure.id}.pdf",
                    hourly_pdf,
                    "application/pdf",
                )
            )

    subject = (
        email_in.subject
        or (
            f"Cierre del dia {closure.closed_at.strftime('%d/%m/%Y')} - "
            f"{closure.consecutive or f'CL-{closure.id:06d}'}"
        )
    )

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            html_body="".join(body_parts),
            cc=None,
            attachments=attachments,
            smtp_config=_smtp_settings_dict(settings),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return schemas.EmailSendResponse(status="sent")


@router.get(
    "/closures",
    response_model=List[schemas.PosClosureList],
)
def list_pos_closures(
    skip: int = 0,
    limit: int = 100,
    pos_name: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    closures = crud.list_pos_closures(
        db,
        skip=skip,
        limit=limit,
        pos_name=pos_name,
        date_from=date_from,
        date_to=date_to,
        tenant_id=tenant_id,
    )
    return closures


@router.get(
    "/closures/{closure_id}",
    response_model=schemas.PosClosureRead,
)
def get_pos_closure(
    closure_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    closure = crud.get_pos_closure(db, closure_id, tenant_id=tenant_id)
    if not closure:
        raise HTTPException(status_code=404, detail="Cierre no encontrado")
    return closure


@router.delete(
    "/closures/{closure_id}",
    status_code=204,
)
def delete_pos_closure(
    closure_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.closures")),
):
    raise HTTPException(
        status_code=403,
        detail="Los cierres no se pueden eliminar.",
    )
