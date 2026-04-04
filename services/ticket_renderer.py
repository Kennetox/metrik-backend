from datetime import datetime, timezone
from html import escape as html_escape
import os
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import models
from services.pdf_utils import build_pdf_from_html, build_simple_pdf

TICKET_MODE = "ticket"
THERMAL_TICKET_MODE = "thermal_ticket"
INVOICE_MODE = "invoice"
CLASSIC_INVOICE_MODE = "classic_invoice"  # backward compatibility

FALLBACK_COMPANY = {
    "name": "Kensar Electronic",
    "address": "Cra. 15 #123 - Bogotá",
    "phone": "+57 300 000 0000",
    "tax_id": "NIT 900000000-0",
    "email": "contacto@kensar.com",
    "footer": "Gracias por tu compra.",
    "logo_url": "",
}

SPANISH_WEEKDAYS = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]
SPANISH_MONTHS = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]

CODE39_PATTERNS = {
    "0": "nnnwwnwnn",
    "1": "wnnwnnnnw",
    "2": "nnwwnnnnw",
    "3": "wnwwnnnnn",
    "4": "nnnwwnnnw",
    "5": "wnnwwnnnn",
    "6": "nnwwwnnnn",
    "7": "nnnwnnwnw",
    "8": "wnnwnnwnn",
    "9": "nnwwnnwnn",
    "A": "wnnnnwnnw",
    "B": "nnwnnwnnw",
    "C": "wnwnnwnnn",
    "D": "nnnnwwnnw",
    "E": "wnnnwwnnn",
    "F": "nnwnwwnnn",
    "G": "nnnnnwwnw",
    "H": "wnnnnwwnn",
    "I": "nnwnnwwnn",
    "J": "nnnnwwwnn",
    "K": "wnnnnnnww",
    "L": "nnwnnnnww",
    "M": "wnwnnnnwn",
    "N": "nnnnwnnww",
    "O": "wnnnwnnwn",
    "P": "nnwnwnnwn",
    "Q": "nnnnnnwww",
    "R": "wnnnnnwwn",
    "S": "nnwnnnwwn",
    "T": "nnnnwnwwn",
    "U": "wwnnnnnnw",
    "V": "nwwnnnnnw",
    "W": "wwwnnnnnn",
    "X": "nwnnwnnnw",
    "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn",
    "-": "nwnnnnwnw",
    ".": "wwnnnnwnn",
    " ": "nwwnnnwnn",
    "$": "nwnwnwnnn",
    "/": "nwnwnnnwn",
    "+": "nwnnnwnwn",
    "%": "nnnwnwnwn",
    "*": "nwnnwnwnn",
}

TICKET_STYLE = """
        * { box-sizing: border-box; }
        body {
          font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
          margin: 0;
          padding: 32px;
          background: #0f172a;
          color: #0f172a;
        }
        .wrapper {
          max-width: 780px;
          margin: 0 auto;
          background: #ffffff;
          border-radius: 24px;
          padding: 32px 36px 40px;
          box-shadow: 0 30px 80px rgba(15, 23, 42, 0.2);
        }
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 16px;
          border-bottom: 1px solid #e2e8f0;
          padding-bottom: 18px;
        }
        .logo img {
          max-height: 72px;
          max-width: 220px;
          object-fit: contain;
        }
        .company-info {
          text-align: right;
          color: #475569;
          font-size: 13px;
          line-height: 1.4;
        }
        .receipt-tag {
          margin-top: 12px;
          display: flex;
          gap: 12px;
          flex-wrap: wrap;
        }
        .tag {
          background: #f1f5f9;
          color: #0f172a;
          font-size: 12px;
          padding: 6px 12px;
          border-radius: 999px;
          font-weight: 600;
        }
        .meta-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 16px;
          margin-top: 24px;
        }
        .meta-card {
          background: #f8fafc;
          border: 1px solid #e2e8f0;
          border-radius: 16px;
          padding: 16px;
        }
        .section-title {
          text-transform: uppercase;
          font-size: 11px;
          letter-spacing: 0.08em;
          color: #94a3b8;
          margin-bottom: 6px;
          font-weight: 600;
        }
        .meta-card .row {
          display: flex;
          justify-content: space-between;
          font-size: 13px;
          margin-bottom: 4px;
        }
        .customer-name {
          font-size: 16px;
          font-weight: 600;
          color: #0f172a;
          margin-bottom: 6px;
        }
        .customer-detail {
          font-size: 13px;
          color: #475569;
          margin-bottom: 2px;
        }
        .items-card {
          margin-top: 28px;
          border: 1px solid #e2e8f0;
          border-radius: 20px;
          padding: 20px;
          background: #ffffff;
        }
        table {
          width: 100%;
          border-collapse: collapse;
        }
        thead th {
          text-align: left;
          font-size: 11px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: #94a3b8;
          padding-bottom: 10px;
        }
        tbody td {
          padding: 12px 0;
          border-bottom: 1px solid #eef2ff;
          vertical-align: top;
        }
        tbody tr:last-child td {
          border-bottom: none;
        }
        .item-name {
          font-weight: 600;
          font-size: 14px;
        }
        .item-meta {
          font-size: 12px;
          color: #64748b;
          margin-top: 2px;
        }
        .discount-chip {
          display: inline-flex;
          margin-left: 4px;
          padding: 2px 8px;
          border-radius: 999px;
          background: #fef3c7;
          color: #92400e;
          font-size: 11px;
        }
        .right {
          text-align: right;
          font-weight: 600;
          font-size: 14px;
        }
        .summary-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
          gap: 18px;
          margin-top: 24px;
        }
        .summary-card {
          border-radius: 20px;
          padding: 20px;
          border: 1px solid #0f172a;
          background: linear-gradient(135deg, #0f172a, #1f2937);
          color: #e2e8f0;
        }
        .summary-card.light {
          border: 1px solid #e2e8f0;
          background: #f8fafc;
          color: #0f172a;
        }
        .summary-card .row {
          display: flex;
          justify-content: space-between;
          font-size: 14px;
          margin-bottom: 8px;
        }
        .summary-card .row:last-child {
          margin-bottom: 0;
        }
        .summary-card .row.total {
          font-size: 20px;
          font-weight: 700;
          color: #34d399;
        }
        .notes-card {
          margin-top: 20px;
          border: 1px solid #e2e8f0;
          border-radius: 18px;
          padding: 18px;
          background: #fff7ed;
          color: #7c2d12;
        }
        .footer-note {
          text-align: center;
          margin-top: 24px;
          border-radius: 16px;
          padding: 18px 16px;
          background: #ecfdf5;
          color: #065f46;
          font-size: 13px;
          line-height: 1.5;
        }
        .barcode {
          margin-top: 26px;
          text-align: center;
        }
        .barcode svg {
          width: 80%;
          height: auto;
        }
        .muted {
          color: #94a3b8;
          font-size: 12px;
        }
        @media print {
          body {
            background: #ffffff;
            padding: 0;
          }
          .wrapper {
            box-shadow: none;
            border-radius: 0;
          }
        }
"""

INVOICE_STYLE = """
        @page {
          size: auto;
          margin: 12mm 16mm 14mm 16mm;
          @bottom-right {
            content: "Página " counter(page);
            font-size: 11px;
            color: #94a3b8;
          }
        }
        body {
          font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
          margin: 0;
          background: #f8fafc;
          color: #0f172a;
        }
        .invoice-wrapper {
          max-width: 760px;
          margin: 0 auto;
          background: #ffffff;
          padding: 24px 28px 28px;
          box-shadow: 0 12px 36px rgba(15, 23, 42, 0.08);
          border-radius: 14px;
          page-break-inside: avoid;
        }
        .invoice-header {
          display: flex;
          justify-content: space-between;
          gap: 18px;
          padding-bottom: 14px;
          border-bottom: 1px solid #e2e8f0;
        }
        .invoice-title {
          font-size: 24px;
          letter-spacing: 0.08em;
          font-weight: 700;
        }
        .invoice-company {
          margin-top: 8px;
          font-size: 12px;
          line-height: 1.35;
          color: #475569;
        }
        .invoice-logo img {
          max-width: 120px;
          max-height: 72px;
          object-fit: contain;
        }
        .invoice-meta {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 12px;
          margin-top: 18px;
        }
        .invoice-card {
          border: 1px solid #e2e8f0;
          border-radius: 12px;
          padding: 14px;
          background: #f8fafc;
          page-break-inside: avoid;
        }
        .invoice-card .label,
        .invoice-notes .label {
          font-size: 11px;
          text-transform: uppercase;
          color: #94a3b8;
          letter-spacing: 0.08em;
          margin-bottom: 6px;
          font-weight: 600;
        }
        .invoice-card .value {
          font-size: 13px;
          color: #0f172a;
        }
        .invoice-card .row {
          display: flex;
          justify-content: space-between;
          font-size: 12px;
          margin-bottom: 3px;
        }
        .invoice-card .row:last-child {
          margin-bottom: 0;
        }
        .invoice-table {
          width: 100%;
          border-collapse: collapse;
          margin-top: 18px;
          page-break-inside: avoid;
        }
        .invoice-table thead th {
          background: #e2e8f0;
          padding: 8px 6px;
          font-size: 11px;
          text-transform: uppercase;
          letter-spacing: 0.05em;
          color: #475569;
          text-align: left;
        }
        .invoice-table tbody td {
          padding: 10px 6px;
          border-bottom: 1px solid #e2e8f0;
          font-size: 12px;
        }
        .invoice-table tbody tr:last-child td {
          border-bottom: none;
        }
        .invoice-table td.number {
          text-align: center;
          width: 32px;
        }
        .invoice-table td.right {
          text-align: right;
          white-space: nowrap;
        }
        .invoice-summary {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
          gap: 14px;
          margin-top: 18px;
          page-break-inside: avoid;
        }
        .totals-card,
        .payments-card {
          border: 1px solid #e2e8f0;
          border-radius: 12px;
          padding: 14px;
          background: #ffffff;
          page-break-inside: avoid;
        }
        .totals-card .row,
        .payments-card .row {
          display: flex;
          justify-content: space-between;
          font-size: 13px;
          margin-bottom: 6px;
        }
        .totals-card .row.total {
          font-weight: 700;
          font-size: 16px;
          color: #0f172a;
        }
        .payments-card .row.emphasis {
          font-weight: 600;
        }
        .invoice-notes {
          margin-top: 16px;
          border: 1px solid #e2e8f0;
          border-radius: 10px;
          padding: 14px;
          background: #fff7ed;
          color: #7c2d12;
          font-size: 12px;
          page-break-inside: avoid;
        }
        .invoice-notes .label {
          color: #7c2d12;
        }
        .invoice-footer {
          margin-top: 18px;
          text-align: center;
          font-size: 11px;
          color: #94a3b8;
          page-break-inside: avoid;
        }
"""


def _escape_html(value: Optional[object]) -> str:
    if value is None:
        return ""
    return html_escape(str(value))


def _format_money(value: Optional[float]) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    formatted = (
        f"{amount:,.0f}".replace(",", "_").replace(".", ",").replace("_", ".")
    )
    return f"{sign}$ {formatted}"


def _format_ticket_datetime(value: Optional[datetime]) -> str:
    if not value:
        return ""
    weekday = SPANISH_WEEKDAYS[value.weekday()]
    month = SPANISH_MONTHS[value.month - 1]
    return f"{weekday}, {value.day:02d} de {month} de {value.year} {value.strftime('%H:%M')}"


def _format_bogota_short_datetime(value: Optional[datetime]) -> str:
    if not value:
        return ""
    bogota_tz = ZoneInfo("America/Bogota")
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(bogota_tz)
    meridian = "a. m." if current.hour < 12 else "p. m."
    hour12 = current.hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{current.day}/{current.month:02d}/{str(current.year)[-2:]}, {hour12}:{current.minute:02d} {meridian}"


def _company_profile(settings: Optional[models.PosSettings]):
    profile = dict(FALLBACK_COMPANY)
    if settings:
        if settings.company_name and settings.company_name.strip():
            profile["name"] = settings.company_name.strip()
        if settings.tax_id and settings.tax_id.strip():
            profile["tax_id"] = settings.tax_id.strip()
        if settings.address and settings.address.strip():
            profile["address"] = settings.address.strip()
        if settings.contact_phone and settings.contact_phone.strip():
            profile["phone"] = settings.contact_phone.strip()
        if settings.contact_email and settings.contact_email.strip():
            profile["email"] = settings.contact_email.strip()
        if settings.ticket_footer and settings.ticket_footer.strip():
            profile["footer"] = settings.ticket_footer.strip()
        logo_url = (
            settings.ticket_logo_url
            or settings.logo_url
            or FALLBACK_COMPANY["logo_url"]
        )
        profile["logo_url"] = _resolve_asset_url(logo_url)
    return profile


def _resolve_asset_url(raw_url: Optional[str]) -> str:
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


def _format_quantity(quantity: Optional[float]) -> str:
    try:
        qty = float(quantity or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty.is_integer():
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


def _effective_total(sale: models.Sale) -> float:
    order = getattr(sale, "separated_order", None)
    if order:
        amount = float(order.total_amount or 0.0)
        if amount:
            return amount
    return float(sale.total or 0.0)


def _effective_cart_discount_value(sale: models.Sale) -> float:
    value = float(sale.cart_discount_value or 0.0)
    order = getattr(sale, "separated_order", None)
    if order and abs(value - float(order.balance or 0.0)) < 0.01:
        return 0.0
    return value


def _surcharge_amount(sale: models.Sale) -> float:
    return float(getattr(sale, "surcharge_amount", 0.0) or 0.0)


def _surcharge_label(sale: models.Sale) -> str:
    label = getattr(sale, "surcharge_label", None)
    if isinstance(label, str):
        label = label.strip()
    return label or "Recargo"


def _collect_sale_items(
    sale: models.Sale,
) -> Tuple[List[dict], float, float]:
    items_summary: List[dict] = []
    subtotal = 0.0
    line_discount_total = 0.0
    for item in sale.items or []:
        quantity = float(item.quantity or 0.0)
        base_price = (
            float(item.unit_price_original)
            if item.unit_price_original not in (None, 0)
            else float(item.unit_price or 0.0)
        )
        gross = quantity * base_price
        total = float(item.total or 0.0)
        discount_value = max(0.0, gross - total)
        subtotal += gross
        line_discount_total += discount_value
        items_summary.append(
            {
                "name": item.product_name or "",
                "sku": item.product_sku or "",
                "quantity": quantity,
                "unit_price": base_price,
                "gross": gross,
                "total": total,
                "discount": discount_value,
            }
        )
    return items_summary, subtotal, line_discount_total


def _build_ticket_items_rows(items_summary: List[dict]) -> str:
    if not items_summary:
        return '<tr><td colspan="2" class="muted">Sin artículos</td></tr>'
    rows = []
    for item in items_summary:
        discount_value = float(item["discount"] or 0.0)
        discount_chip = (
            f'<span class="discount-chip">Desc -{_format_money(discount_value)}</span>'
            if discount_value > 0
            else ""
        )
        rows.append(
            "<tr>"
            f"<td><div class=\"item-name\">{_escape_html(item['name'])}</div>"
            f"<div class=\"item-meta\">{_format_quantity(item['quantity'])} x {_format_money(item['unit_price'])} {discount_chip}</div></td>"
            f"<td class=\"right\">{_format_money(item['total'])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _build_invoice_table_rows(items_summary: List[dict]) -> str:
    if not items_summary:
        return (
            "<tr>"
            '<td colspan="5" class="muted">Sin artículos registrados</td>'
            "</tr>"
        )
    rows = []
    for item in items_summary:
        discount_value = float(item["discount"] or 0.0)
        discount_display = (
            f"-{_format_money(discount_value)}"
            if discount_value > 0
            else "0"
        )
        rows.append(
            "<tr>"
            f"<td>{_escape_html(item['name'])}</td>"
            f"<td class=\"right\">{_format_quantity(item['quantity'])}</td>"
            f"<td class=\"right\">{_format_money(item['unit_price'])}</td>"
            f"<td class=\"right\">{discount_display}</td>"
            f"<td class=\"right\">{_format_money(item['total'])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _build_payment_rows(sale: models.Sale) -> str:
    return _build_payment_rows_with_labels(sale, payment_method_labels=None)


def _resolve_payment_label(
    method: Optional[str],
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    normalized = (method or "").strip().lower()
    if payment_method_labels and normalized in payment_method_labels:
        return payment_method_labels[normalized]
    return (method or "").replace("_", " ").title() or "Pago"


def _build_payment_rows_with_labels(
    sale: models.Sale,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    if not sale.payments:
        return '<div class="row"><span>Sin pagos registrados</span><span>$ 0</span></div>'
    rows = []
    for payment in sale.payments:
        label = _resolve_payment_label(payment.method, payment_method_labels)
        rows.append(
            "<div class=\"row\">"
            f"<span>{_escape_html(label)}</span>"
            f"<span>{_format_money(payment.amount)}</span>"
            "</div>"
        )
    return "\n".join(rows)


def _build_thermal_payment_rows(sale: models.Sale) -> str:
    return _build_thermal_payment_rows_with_labels(sale, payment_method_labels=None)


def _build_thermal_payment_rows_with_labels(
    sale: models.Sale,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    if not sale.payments:
        return '<div class="line"><span>Sin pagos registrados</span><span>$ 0</span></div>'
    rows = []
    for payment in sale.payments:
        label = _resolve_payment_label(payment.method, payment_method_labels)
        rows.append(
            "<div class=\"line\">"
            f"<span>{_escape_html(label)}</span>"
            f"<span>{_format_money(payment.amount)}</span>"
            "</div>"
        )
    return "\n".join(rows)


def _build_invoice_payment_rows(
    sale: models.Sale,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    rows = []
    has_mixed_payments = bool(sale.payments and len(sale.payments) > 1)
    row_class = " class=\"mixed-payment-row\"" if has_mixed_payments else ""
    if sale.payments:
        for payment in sale.payments:
            label = _resolve_payment_label(payment.method, payment_method_labels)
            rows.append(
                f"<tr{row_class}>"
                f"<td>{_escape_html(label)}</td>"
                f"<td>{_format_money(payment.amount)}</td>"
                "</tr>"
            )
    else:
        label = _resolve_payment_label(
            sale.main_payment_method or sale.payment_method or "Pago",
            payment_method_labels,
        )
        rows.append(
            "<tr>"
            f"<td>{_escape_html(label)}</td>"
            f"<td>{_format_money(sale.paid_amount)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _cart_discount_meta(sale: models.Sale) -> tuple[str, str]:
    value = _effective_cart_discount_value(sale)
    percent = float(sale.cart_discount_percent or 0.0)
    order = getattr(sale, "separated_order", None)
    if order and abs(value) < 0.01:
        percent = 0.0
    if value:
        sign = "-" if value > 0 else "+"
        display = f"{sign} {_format_money(abs(value))}"
    elif percent:
        sign = "-" if percent > 0 else "+"
        display = f"{sign} {abs(percent):.2f}%"
    else:
        display = _format_money(0)
    return "Desc. carrito", display


def _note_lines(notes: Optional[str]) -> List[str]:
    if not notes or not notes.strip():
        return []
    lines = []
    for raw in notes.splitlines():
        if raw and raw.strip():
            lines.append(raw.strip())
    return lines


def _notes_block(notes: Optional[str]) -> str:
    lines = _note_lines(notes)
    if not lines:
        return ""
    content = "".join(f"<div>{_escape_html(line)}</div>" for line in lines)
    return (
        '<div class="notes-card">'
        '<div class="section-title">Notas de la venta</div>'
        f"{content}"
        "</div>"
    )


def _invoice_notes_block(notes: Optional[str]) -> str:
    lines = _note_lines(notes)
    if not lines:
        return ""
    content = "".join(f"<div>{_escape_html(line)}</div>" for line in lines)
    return (
        '<div class="invoice-notes">'
        '<div class="label">Notas</div>'
        f"{content}"
        "</div>"
    )


def _customer_block(sale: models.Sale) -> str:
    name = sale.customer_name or ""
    if not name.strip():
        return ""
    detail_rows = []
    if sale.customer_tax_id:
        detail_rows.append(
            f"<div class=\"customer-detail\">NIT / ID: {_escape_html(sale.customer_tax_id)}</div>"
        )
    if sale.customer_phone:
        detail_rows.append(
            f"<div class=\"customer-detail\">Teléfono: {_escape_html(sale.customer_phone)}</div>"
        )
    if sale.customer_email:
        detail_rows.append(
            f"<div class=\"customer-detail\">Email: {_escape_html(sale.customer_email)}</div>"
        )
    if sale.customer_address:
        detail_rows.append(
            f"<div class=\"customer-detail\">Dirección: {_escape_html(sale.customer_address)}</div>"
        )
    details_html = "".join(detail_rows)
    return (
        '<div class="meta-card">'
        '<div class="section-title">Cliente</div>'
        f'<div class="customer-name">{_escape_html(name)}</div>'
        f"{details_html}"
        "</div>"
    )


def _footer_lines(footer: str) -> str:
    lines = [
        line.strip()
        for line in footer.splitlines()
        if line and line.strip()
    ]
    if not lines:
        return _escape_html(footer)
    return "".join(f"<div>{_escape_html(line)}</div>" for line in lines)


def _sanitize_code39_value(value: str) -> str:
    allowed = set(CODE39_PATTERNS.keys()) - {"*"}
    filtered = "".join(ch for ch in value.upper() if ch in allowed)
    return filtered or "0"


def _generate_code39_svg(
    value: str,
    height: float = 90.0,
    include_text: bool = True,
    font_size: float = 14.0,
) -> str:
    if not value:
        return ""
    data = _sanitize_code39_value(value)
    encoded = f"*{data}*"
    narrow = 1.5
    wide = narrow * 2.5
    gap = narrow
    x_position = 0.0
    bars = []
    for char in encoded:
        pattern = CODE39_PATTERNS.get(char, CODE39_PATTERNS["-"])
        for idx, module in enumerate(pattern):
            width = narrow if module == "n" else wide
            if idx % 2 == 0:
                bars.append(
                    f'<rect x="{x_position:.2f}" y="0" width="{width:.2f}" height="{height:.2f}" fill="#0f172a" />'
                )
            x_position += width
        x_position += gap
    total_width = max(x_position, 160.0)
    text_space = font_size + 6 if include_text else 0
    view_height = height + text_space
    text_element = ""
    if include_text:
        text_element = (
            f'<text x="{total_width/2:.2f}" y="{height + font_size:.2f}" '
            f'font-size="{font_size}" text-anchor="middle" fill="#0f172a" '
            'font-family="Inter, Arial, sans-serif">'
            f"{_escape_html(data)}</text>"
        )
    bars_html = "".join(bars)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width:.2f}" '
        f'height="{view_height:.2f}" viewBox="0 0 {total_width:.2f} {view_height:.2f}">'
        f"{bars_html}{text_element}</svg>"
    )


CODE128_PATTERNS = [
    "212222",
    "222122",
    "222221",
    "121223",
    "121322",
    "131222",
    "122213",
    "122312",
    "132212",
    "221213",
    "221312",
    "231212",
    "112232",
    "122132",
    "122231",
    "113222",
    "123122",
    "123221",
    "223211",
    "221132",
    "221231",
    "213212",
    "223112",
    "312131",
    "311222",
    "321122",
    "321221",
    "312212",
    "322112",
    "322211",
    "212123",
    "212321",
    "232121",
    "111323",
    "131123",
    "131321",
    "112313",
    "132113",
    "132311",
    "211313",
    "231113",
    "231311",
    "112133",
    "112331",
    "132131",
    "113123",
    "113321",
    "133121",
    "313121",
    "211331",
    "231131",
    "213113",
    "213311",
    "213131",
    "311123",
    "311321",
    "331121",
    "312113",
    "312311",
    "332111",
    "314111",
    "221411",
    "431111",
    "111224",
    "111422",
    "121124",
    "121421",
    "141122",
    "141221",
    "112214",
    "112412",
    "122114",
    "122411",
    "142112",
    "142211",
    "241211",
    "221114",
    "413111",
    "241112",
    "134111",
    "111242",
    "121142",
    "121241",
    "114212",
    "124112",
    "124211",
    "411212",
    "421112",
    "421211",
    "212141",
    "214121",
    "412121",
    "111143",
    "111341",
    "131141",
    "114113",
    "114311",
    "411113",
    "411311",
    "113141",
    "114131",
    "311141",
    "411131",
    "211412",
    "211214",
    "211232",
    "2331112",
]


def _sanitize_code128c_value(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits or "0"


def _generate_code128_svg(
    value: str,
    height: float = 90.0,
    module_width: float = 2.0,
    include_text: bool = True,
    font_size: float = 14.0,
    quiet_zone_modules: int = 10,
) -> str:
    if not value:
        return ""
    data = _sanitize_code128c_value(value)
    if len(data) % 2 == 1:
        data = f"0{data}"

    codes = [105]  # Start Code C
    for idx in range(0, len(data), 2):
        codes.append(int(data[idx : idx + 2]))

    checksum = codes[0]
    for idx in range(1, len(codes)):
        checksum += codes[idx] * idx
    codes.append(checksum % 103)
    codes.append(106)  # Stop

    quiet_zone = module_width * quiet_zone_modules
    x_position = quiet_zone
    bars = []

    for code in codes:
        pattern = CODE128_PATTERNS[code]
        for idx, module in enumerate(pattern):
            width = int(module) * module_width
            if idx % 2 == 0:
                bars.append(
                    f'<rect x="{x_position:.2f}" y="0" width="{width:.2f}" height="{height:.2f}" fill="#0f172a" />'
                )
            x_position += width

    x_position += quiet_zone
    total_width = max(x_position, 160.0)
    text_space = font_size + 6 if include_text else 0
    view_height = height + text_space
    text_element = ""
    if include_text:
        text_element = (
            f'<text x="{total_width/2:.2f}" y="{height + font_size:.2f}" '
            f'font-size="{font_size}" text-anchor="middle" fill="#0f172a" '
            'font-family="Inter, Arial, sans-serif">'
            f"{_escape_html(data)}</text>"
        )
    bars_html = "".join(bars)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width:.2f}" '
        f'height="{view_height:.2f}" viewBox="0 0 {total_width:.2f} {view_height:.2f}">'
        f"{bars_html}{text_element}</svg>"
    )


def _render_modern_ticket_html(
    sale: models.Sale,
    company: dict,
    items_summary: List[dict],
    subtotal: float,
    line_discount_total: float,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    document_number = sale.document_number or f"V-{sale.id:06d}"
    sale_number = sale.sale_number or sale.id
    formatted_date = _format_ticket_datetime(sale.created_at)
    item_rows = _build_ticket_items_rows(items_summary)
    payment_rows = _build_payment_rows_with_labels(
        sale,
        payment_method_labels=payment_method_labels,
    )
    change_amount = float(sale.change_amount or 0.0)
    change_row = ""
    if change_amount:
        change_row = (
            "<div class=\"row\">"
            f"<span>{'Cambio' if change_amount > 0 else 'Saldo'}</span>"
            f"<span>{_format_money(abs(change_amount))}</span>"
            "</div>"
        )
    notes_block = _notes_block(sale.notes)
    customer_block = _customer_block(sale)
    cart_discount_label, cart_discount_display = _cart_discount_meta(sale)
    footer_html = _footer_lines(company["footer"])
    sale_number_str = str(sale_number or "")
    numeric_sale = "".join(ch for ch in sale_number_str if ch.isdigit()) or sale_number_str
    if not numeric_sale.isdigit():
        numeric_sale = ""
    padded_sale = numeric_sale.zfill(6) if numeric_sale else "000000"
    barcode_svg = _generate_code128_svg(padded_sale, height=90.0, module_width=2.0, include_text=True, font_size=14.0, quiet_zone_modules=10)
    total_amount = _effective_total(sale)

    parts: List[str] = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8" />',
        f"<title>Ticket {_escape_html(document_number)}</title>",
        f"<style>{TICKET_STYLE}</style>",
        "</head>",
        "<body>",
        '<div class="wrapper">',
        '<header class="header">',
        '<div class="logo">',
    ]

    if company["logo_url"]:
        parts.append(
            f'<img src="{_escape_html(company["logo_url"])}" alt="Logo" />'
        )
    else:
        parts.append(
            f'<div style="font-weight:700;font-size:18px;color:#0f172a;">{_escape_html(company["name"])}</div>'
        )
    parts.append("</div>")
    parts.append(
        '<div class="company-info">'
        f"<strong>{_escape_html(company['name'])}</strong><br />"
    )
    if company["address"]:
        parts.append(f"{_escape_html(company['address'])}<br />")
    if company["phone"]:
        parts.append(f"{_escape_html(company['phone'])}<br />")
    if company["email"]:
        parts.append(f"{_escape_html(company['email'])}<br />")
    if company["tax_id"]:
        parts.append(f"{_escape_html(company['tax_id'])}")
    parts.append("</div></header>")

    parts.append('<div class="receipt-tag">')
    parts.append(
        f'<span class="tag">Ticket #{_escape_html(str(sale_number))}</span>'
    )
    parts.append(
        f'<span class="tag">Documento: {_escape_html(document_number)}</span>'
    )
    parts.append("</div>")

    parts.append('<div class="meta-grid">')
    parts.append('<div class="meta-card">')
    parts.append('<div class="section-title">Datos de la venta</div>')
    parts.append(
        f'<div class="row"><span>Fecha</span><span>{_escape_html(formatted_date)}</span></div>'
    )
    if sale.vendor_name:
        parts.append(
            f'<div class="row"><span>Vendedor</span><span>{_escape_html(sale.vendor_name)}</span></div>'
        )
    if sale.pos_name:
        parts.append(
            f'<div class="row"><span>Punto de venta</span><span>{_escape_html(sale.pos_name)}</span></div>'
        )
    parts.append("</div>")
    if customer_block:
        parts.append(customer_block)
    parts.append("</div>")

    parts.append('<section class="items-card">')
    parts.append('<div class="section-title">Detalle de productos</div>')
    parts.append("<table>")
    parts.append(
        "<thead><tr><th>Producto</th><th style=\"text-align:right;\">Total</th></tr></thead>"
    )
    parts.append(f"<tbody>{item_rows}</tbody>")
    parts.append("</table></section>")

    parts.append('<section class="summary-grid">')
    parts.append('<div class="summary-card">')
    parts.append(
        '<div class="section-title" style="color:#cbd5f5;">Resumen</div>'
    )
    parts.append(
        f'<div class="row"><span>Subtotal productos</span><span>{_format_money(subtotal)}</span></div>'
    )
    if line_discount_total > 0:
        parts.append(
            f'<div class="row"><span>Desc. artículos</span><span>- {_format_money(line_discount_total)}</span></div>'
        )
    parts.append(
        f'<div class="row"><span>{_escape_html(cart_discount_label)}</span><span>{_escape_html(cart_discount_display)}</span></div>'
    )
    surcharge_amount = _surcharge_amount(sale)
    if surcharge_amount > 0:
        parts.append(
            f'<div class="row"><span>{_escape_html(_surcharge_label(sale))}</span><span>{_format_money(surcharge_amount)}</span></div>'
        )
    parts.append(
        f'<div class="row total"><span>Total cobrado</span><span>{_format_money(total_amount)}</span></div>'
    )
    parts.append("</div>")

    parts.append('<div class="summary-card light">')
    parts.append('<div class="section-title">Pagos registrados</div>')
    parts.append('<div class="payments-list">')
    parts.append(payment_rows)
    if change_row:
        parts.append(change_row)
    parts.append("</div></div></section>")

    if notes_block:
        parts.append(notes_block)

    parts.append(f'<div class="footer-note">{footer_html}</div>')

    if barcode_svg:
        parts.append(f'<div class="barcode">{barcode_svg}</div>')

    parts.append("</div></body></html>")

    return "".join(parts)


def _render_thermal_ticket_html(
    sale: models.Sale,
    company: dict,
    items_summary: List[dict],
    subtotal: float,
    line_discount_total: float,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    document_number = sale.document_number or f"V-{sale.id:06d}"
    formatted_date = _format_bogota_short_datetime(sale.created_at)
    payment_rows = _build_thermal_payment_rows_with_labels(
        sale,
        payment_method_labels=payment_method_labels,
    )
    cart_discount_label, cart_discount_display = _cart_discount_meta(sale)
    if cart_discount_label == "Desc. carrito":
        cart_discount_label = "Descuento carrito"
    total_amount = _effective_total(sale)
    footer_html = _footer_lines(company["footer"])
    change_amount = float(sale.change_amount or 0.0)
    change_row = ""
    if change_amount:
        change_row = (
            "<div class=\"line\">"
            f"<span>{'Cambio' if change_amount > 0 else 'Saldo'}</span>"
            f"<span>{_format_money(abs(change_amount))}</span>"
            "</div>"
        )

    sale_number_str = str(sale.sale_number or sale.id or "")
    numeric_sale = "".join(ch for ch in sale_number_str if ch.isdigit())
    padded_sale = (numeric_sale or "0").zfill(6)
    barcode_svg = _generate_code128_svg(
        padded_sale,
        height=30.0,
        module_width=2.0,
        include_text=True,
        font_size=12.0,
        quiet_zone_modules=10,
    )

    customer_block = ""
    if sale.customer_name and sale.customer_name.strip():
        customer_lines = [
            '<div class="line-title">Cliente</div>',
            f'<div class="customer-name">{_escape_html(sale.customer_name)}</div>',
        ]
        if sale.customer_phone:
            customer_lines.append(
                f'<div class="customer-detail">Tel: {_escape_html(sale.customer_phone)}</div>'
            )
        if sale.customer_email:
            customer_lines.append(
                f'<div class="customer-detail">Email: {_escape_html(sale.customer_email)}</div>'
            )
        if sale.customer_tax_id:
            customer_lines.append(
                f'<div class="customer-detail">NIT / ID: {_escape_html(sale.customer_tax_id)}</div>'
            )
        if sale.customer_address:
            customer_lines.append(
                f'<div class="customer-detail">Dirección: {_escape_html(sale.customer_address)}</div>'
            )
        customer_block = (
            "<div class=\"section\">"
            + "".join(customer_lines)
            + "</div><div class=\"separator\"></div>"
        )

    notes_block = _notes_block(sale.notes)
    if notes_block:
        notes_block = notes_block.replace("notes-card", "thermal-notes")

    items_rows = (
        "".join(
            (
                '<div class="item-row">'
                "<div>"
                f'<div class="item-name">{_escape_html(item["name"])}</div>'
                f'<div class="item-meta">{_format_quantity(item["quantity"])} x {_format_money(item["unit_price"])}'
                + (
                    f' <span class="item-discount">(Desc -{_format_money(item["discount"])})</span>'
                    if float(item["discount"] or 0.0) > 0
                    else ""
                )
                + "</div>"
                "</div>"
                f'<div class="item-total">{_format_money(item["total"])}</div>'
                "</div>"
            )
            for item in items_summary
        )
        if items_summary
        else '<div class="item-row"><div class="item-name">Sin artículos</div></div>'
    )

    surcharge_amount = _surcharge_amount(sale)
    surcharge_row = ""
    if surcharge_amount > 0:
        surcharge_row = (
            "<div class=\"line\">"
            f"<span>{_escape_html(_surcharge_label(sale))}</span>"
            f"<span>+ {_format_money(surcharge_amount)}</span>"
            "</div>"
        )

    html_parts: List[str] = [
        "<!DOCTYPE html><html><head>",
        '<meta charset="utf-8" />',
        f"<title>Ticket {_escape_html(document_number)}</title>",
        "<style>",
        "@page { margin: 4mm; }",
        "* { box-sizing: border-box; }",
        'body { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; width: 80mm; margin: 0 auto; font-size: 13px; color: #0f172a; background: #ffffff; }',
        ".ticket { padding: 3mm 3mm 8mm; }",
        ".logo { text-align: center; margin-bottom: 8px; }",
        ".logo img { max-width: 60mm; max-height: 28mm; object-fit: contain; }",
        "h1 { font-size: 20px; text-align: center; margin: 0; }",
        ".company-info { text-align: center; color: #111827; font-size: 13px; line-height: 1.4; margin-top: 4px; }",
        ".separator { border-top: 1px solid #111827; margin: 10px 0; }",
        ".line-title { font-size: 12px; letter-spacing: 0.08em; color: #111827; text-transform: uppercase; font-weight: 700; margin-bottom: 2px; }",
        ".customer-name { font-weight: 600; font-size: 14px; }",
        ".customer-detail { color: #111827; font-size: 12px; }",
        ".line { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 2px; line-height: 1.4; }",
        ".line span:last-child { min-width: 38mm; text-align: right; font-weight: 700; }",
        ".items { display: flex; flex-direction: column; gap: 6px; }",
        ".item-row { display: flex; justify-content: space-between; gap: 4mm; }",
        ".item-row > div:first-child { max-width: 46mm; }",
        ".item-name { font-weight: 600; }",
        ".item-meta { color: #0f172a; font-size: 11px; }",
        ".item-discount { color: #0f172a; margin-left: 4px; }",
        ".item-total { font-weight: 600; min-width: 25mm; text-align: right; }",
        ".totals { display: flex; justify-content: space-between; font-size: 20px; margin-top: 8px; }",
        ".totals span { font-weight: 800; font-size: 20px; }",
        ".totals strong { font-size: 20px; font-weight: 800; }",
        ".section { margin-bottom: 12px; }",
        ".barcode { margin-top: 16px; text-align: center; }",
        ".barcode svg { width: 96%; height: auto; }",
        ".footer { margin-top: 16px; text-align: center; font-size: 12px; color: #111827; line-height: 1.4; }",
        ".thermal-notes { margin-top: 10px; font-size: 12px; color: #111827; }",
        "</style></head><body><div class=\"ticket\">",
        "<div class=\"logo\">",
    ]
    if company["logo_url"]:
        html_parts.append(f'<img src="{_escape_html(company["logo_url"])}" alt="Logo" />')
    else:
        html_parts.append(f"<strong>{_escape_html(company['name'])}</strong>")
    html_parts.extend(
        [
            "</div>",
            f"<h1>{_escape_html(company['name'])}</h1>",
            "<div class=\"company-info\">",
            f"{_escape_html(company['address'])}<br />",
            f"{_escape_html(company['phone'])} · {_escape_html(company['email'])}<br />",
            f"{_escape_html(company['tax_id'])}<br />",
            "CONSERVA ESTE RECIBO Y EMPAQUE ORIGINAL PARA GARANTÍA",
            "</div>",
            "<div class=\"separator\"></div>",
            customer_block,
            "<div class=\"section\">",
            f"<div class=\"line\"><span>No. Recibo</span><span>{_escape_html(document_number)}</span></div>",
            f"<div class=\"line\"><span>Fecha</span><span>{_escape_html(formatted_date)}</span></div>",
        ]
    )
    if sale.vendor_name:
        html_parts.append(
            f"<div class=\"line\"><span>Usuario</span><span>{_escape_html(sale.vendor_name)}</span></div>"
        )
    if sale.pos_name:
        html_parts.append(
            f"<div class=\"line\"><span>POS</span><span>{_escape_html(sale.pos_name)}</span></div>"
        )
    html_parts.extend(
        [
            "</div>",
            "<div class=\"separator\"></div>",
            "<div class=\"section\">",
            '<div class="line-title">Detalle de productos</div>',
            f"<div class=\"items\">{items_rows}</div>",
            "</div>",
            "<div class=\"separator\"></div>",
            "<div class=\"section\">",
            f"<div class=\"line\"><span>Subtotal</span><span>{_format_money(subtotal)}</span></div>",
        ]
    )
    if line_discount_total > 0:
        html_parts.append(
            f"<div class=\"line\"><span>Descuento artículos</span><span>- {_format_money(line_discount_total)}</span></div>"
        )
    html_parts.extend(
        [
            f"<div class=\"line\"><span>{_escape_html(cart_discount_label)}</span><span>{_escape_html(cart_discount_display)}</span></div>",
            surcharge_row,
            "</div>",
            "<div class=\"separator\"></div>",
            "<div class=\"section\">",
            '<div class="line-title">Pagos recibidos</div>',
            payment_rows,
            change_row,
            "</div>",
            "<div class=\"totals\">",
            "<span>TOTAL</span>",
            f"<strong>{_format_money(total_amount)}</strong>",
            "</div>",
            notes_block,
        ]
    )
    if barcode_svg:
        html_parts.append(f'<div class="barcode">{barcode_svg}</div>')
    html_parts.extend(
        [
            f'<div class="footer">{footer_html}</div>',
            "</div></body></html>",
        ]
    )
    return "".join(html_parts)


def _render_classic_invoice_html(
    sale: models.Sale,
    company: dict,
    items_summary: List[dict],
    subtotal: float,
    line_discount_total: float,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    document_number = sale.document_number or f"V-{sale.id:06d}"
    sale_number = sale.sale_number or sale.id
    formatted_date = _format_ticket_datetime(sale.created_at)
    footer_html = _footer_lines(company["footer"])
    table_rows = _build_invoice_table_rows(items_summary)
    cart_discount_label, cart_discount_display = _cart_discount_meta(sale)
    notes_block = _invoice_notes_block(sale.notes)
    payment_rows = _build_invoice_payment_rows(
        sale,
        payment_method_labels=payment_method_labels,
    )
    paid_amount = float(sale.paid_amount or 0.0)
    total_amount = _effective_total(sale)
    change_amount = float(sale.change_amount or 0.0)
    payment_status = "Pagado" if (total_amount - paid_amount) <= 0.01 else "Pendiente"

    company_lines = [
        _escape_html(company["name"]),
    ]
    for field in ["address", "phone", "email", "tax_id"]:
        if company.get(field):
            company_lines.append(_escape_html(company[field]))
    company_html = "<br />".join(company_lines)

    customer_lines = []
    customer_name = (sale.customer_name or "").strip()
    if customer_name:
        customer_lines.append(_escape_html(customer_name))
    else:
        customer_lines.append("Cliente Final")
    if sale.customer_tax_id:
        customer_lines.append(f"NIT / ID: {_escape_html(sale.customer_tax_id)}")
    if sale.customer_address:
        customer_lines.append(f"Dirección: {_escape_html(sale.customer_address)}")
    if sale.customer_phone:
        customer_lines.append(f"Teléfono: {_escape_html(sale.customer_phone)}")
    if sale.customer_email:
        customer_lines.append(f"Email: {_escape_html(sale.customer_email)}")
    customer_html = "<br />".join(customer_lines)

    surcharge_amount = _surcharge_amount(sale)
    surcharge_row = ""
    if surcharge_amount > 0:
        surcharge_row = (
            "<tr>"
            f"<td>{_escape_html(_surcharge_label(sale))}</td>"
            f"<td>{_format_money(surcharge_amount)}</td>"
            "</tr>"
        )
    line_discount_row = ""
    if line_discount_total > 0:
        line_discount_row = (
            "<tr>"
            f"<td>Descuento artículos</td><td>- {_format_money(line_discount_total)}</td>"
            "</tr>"
        )
    change_row = ""
    if change_amount > 0:
        change_row = (
            "<tr>"
            f"<td>Cambio</td><td>{_format_money(change_amount)}</td>"
            "</tr>"
        )

    parts: List[str] = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8" />',
        f"<title>Factura {_escape_html(document_number)}</title>",
        "<style>",
        "@page { size: A4; margin: 10mm; }",
        "* { box-sizing: border-box; }",
        "html, body { margin: 0; padding: 0; background: #ffffff; }",
        "body { font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif; color: #0f172a; }",
        ".sheet { width: 100%; max-width: 100%; margin: 0; background: #ffffff; padding: 0; border: none; overflow: hidden; }",
        "header { display: grid; grid-template-columns: minmax(0, 1fr) 220px; align-items: start; column-gap: 16px; border-bottom: 2px solid #1f2937; padding-bottom: 12px; margin-bottom: 18px; }",
        ".company h1 { margin: 0; font-size: 22px; letter-spacing: 0.08em; }",
        ".company p { margin: 2px 0; font-size: 12px; word-break: break-word; overflow-wrap: anywhere; }",
        ".company { min-width: 0; }",
        ".meta { font-size: 12px; width: 220px; min-width: 220px; max-width: 220px; justify-self: end; }",
        ".meta div { margin-bottom: 4px; }",
        ".meta .doc-number { font-size: 18px; font-weight: 700; margin-bottom: 8px; }",
        ".meta .meta-row { display: grid; grid-template-columns: 58px 1fr; align-items: start; column-gap: 6px; margin-bottom: 4px; }",
        ".meta .meta-row .label { font-weight: 600; color: #334155; white-space: nowrap; }",
        ".meta .meta-row .value { text-align: right; color: #0f172a; word-break: break-word; overflow-wrap: anywhere; }",
        ".logo img { max-width: 140px; max-height: 60px; object-fit: contain; margin-bottom: 8px; }",
        ".info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 18px; font-size: 12px; }",
        ".info-box { border: 1px solid #d1d5db; padding: 12px; }",
        ".info-box strong { display: block; margin-bottom: 8px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; }",
        "table { width: 100%; max-width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; }",
        "thead { background: #f8fafc; }",
        "th, td { padding: 8px 10px; border: 1px solid #e5e7eb; text-align: left; }",
        "th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #475569; }",
        ".items-table th:nth-child(1), .items-table td:nth-child(1) { width: 46%; text-align: left; }",
        ".items-table th:nth-child(2), .items-table td:nth-child(2) { width: 9%; text-align: right; }",
        ".items-table th:nth-child(3), .items-table td:nth-child(3) { width: 15%; text-align: right; }",
        ".items-table th:nth-child(4), .items-table td:nth-child(4) { width: 12%; text-align: right; }",
        ".items-table th:nth-child(5), .items-table td:nth-child(5) { width: 18%; text-align: right; }",
        ".totals { width: 100%; max-width: 360px; margin-left: auto; margin-top: 16px; font-size: 13px; table-layout: fixed; }",
        ".totals tr td:first-child { width: 42%; text-align: left; padding-left: 12px; white-space: nowrap; }",
        ".totals tr td:last-child { text-align: right; font-weight: 600; }",
        ".totals tr.total td { font-size: 15px; font-weight: 700; }",
        ".payments { margin-top: 20px; width: 100%; max-width: 360px; font-size: 12px; }",
        ".payments th, .payments td { text-align: left; }",
        ".payments td:last-child { text-align: right; }",
        ".payments .mixed-payment-row td { padding-top: 10px; padding-bottom: 10px; }",
        ".payments .mixed-payment-row + .mixed-payment-row td { border-top: 1px dashed #cbd5e1; }",
        ".invoice-notes { margin-top: 16px; border: 1px solid #d1d5db; padding: 10px 12px; font-size: 12px; color: #334155; }",
        ".invoice-notes .label { margin-bottom: 6px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; color: #64748b; font-weight: 700; }",
        ".footer-note { margin-top: 24px; font-size: 11.5px; text-align: center; color: #475569; }",
        "</style>",
        "</head>",
        "<body>",
        '<div class="sheet">',
        "<header>",
        '<div class="company">',
        "<h1>FACTURA</h1>",
        f"<p><strong>{_escape_html(company['name'])}</strong></p>",
        f"<p>{_escape_html(company.get('address') or '')}</p>",
        f"<p>Tel: {_escape_html(company.get('phone') or '')} · Email: {_escape_html(company.get('email') or '')}</p>",
        f"<p>NIT: {_escape_html(company.get('tax_id') or '')}</p>",
        "</div>",
        '<div class="meta">',
    ]

    if company["logo_url"]:
        parts.append(
            f'<div class="logo"><img src="{_escape_html(company["logo_url"])}" alt="Logo" /></div>'
        )
    parts.extend(
        [
            f'<div class="doc-number">{_escape_html(document_number)}</div>',
            '<div class="meta-row"><span class="label">Fecha:</span>'
            f'<span class="value">{_escape_html(formatted_date)}</span></div>',
            '<div class="meta-row"><span class="label">POS:</span>'
            f'<span class="value">{_escape_html(sale.pos_name or "")}</span></div>',
            '<div class="meta-row"><span class="label">Cajero:</span>'
            f'<span class="value">{_escape_html(sale.vendor_name or "")}</span></div>',
            "</div>",
            "</header>",
            '<div class="info-grid">',
            '<div class="info-box">',
            "<strong>Cliente</strong>",
            customer_html,
            "</div>",
            '<div class="info-box">',
            "<strong>Resumen</strong>",
            f"<div>No. venta: {_escape_html(str(sale_number))}</div>",
            f"<div>Documento: {_escape_html(document_number)}</div>",
            f"<div>Estado del pago: {payment_status}</div>",
            "</div>",
            "</div>",
            '<table class="items-table">',
            "<thead><tr><th>Descripción</th><th>Cant.</th><th>Precio</th><th>Desc.</th><th>Total</th></tr></thead>",
            f"<tbody>{table_rows}</tbody>",
            "</table>",
            notes_block,
            '<table class="totals">',
            f"<tr><td>Subtotal</td><td>{_format_money(subtotal)}</td></tr>",
            line_discount_row,
            f"<tr><td>{_escape_html(cart_discount_label)}</td><td>{_escape_html(cart_discount_display)}</td></tr>",
            surcharge_row,
            f"<tr class='total'><td>Total</td><td>{_format_money(total_amount)}</td></tr>",
            "</table>",
            '<table class="payments">',
            "<thead><tr><th>Método</th><th>Monto</th></tr></thead>",
            f"<tbody>{payment_rows}</tbody>",
            f"<tfoot><tr><td><strong>Total pagado</strong></td><td><strong>{_format_money(paid_amount)}</strong></td></tr>{change_row}</tfoot>",
            "</table>",
        ]
    )
    parts.extend(
        [
            f'<div class="footer-note">{footer_html}</div>',
            "</div>",
            "</body>",
            "</html>",
        ]
    )
    return "".join(parts)


def _is_invoice_mode(mode: str) -> bool:
    return mode in {INVOICE_MODE, CLASSIC_INVOICE_MODE}


def render_sale_ticket_html(
    sale: models.Sale,
    settings: Optional[models.PosSettings] = None,
    mode: str = TICKET_MODE,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> str:
    company = _company_profile(settings)
    items_summary, subtotal, line_discount_total = _collect_sale_items(sale)
    if _is_invoice_mode(mode):
        return _render_classic_invoice_html(
            sale,
            company,
            items_summary,
            subtotal,
            line_discount_total,
            payment_method_labels=payment_method_labels,
        )
    if mode == THERMAL_TICKET_MODE:
        return _render_thermal_ticket_html(
            sale,
            company,
            items_summary,
            subtotal,
            line_discount_total,
            payment_method_labels=payment_method_labels,
        )
    return _render_modern_ticket_html(
        sale,
        company,
        items_summary,
        subtotal,
        line_discount_total,
        payment_method_labels=payment_method_labels,
    )


def render_sale_ticket_pdf(
    sale: models.Sale,
    settings: Optional[models.PosSettings] = None,
    mode: str = TICKET_MODE,
    payment_method_labels: Optional[dict[str, str]] = None,
) -> bytes:
    html = render_sale_ticket_html(
        sale,
        settings=settings,
        mode=mode,
        payment_method_labels=payment_method_labels,
    )
    label = "Factura" if _is_invoice_mode(mode) else "Ticket"
    title = f"{label} {sale.document_number or sale.sale_number or sale.id}"
    return build_pdf_from_html(title, html)


def _format_currency(value: float) -> str:
    amount = float(value or 0.0)
    return f"${amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _closure_station_breakdown_rows(closure: models.PosClosure) -> list[dict]:
    raw_rows = getattr(closure, "station_breakdown", None)
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("station_label") or raw.get("station_id") or "Sin estación")
        rows.append(
            {
                "label": label,
                "station_type": str(raw.get("station_type") or "").lower() or None,
                "sales_count": int(raw.get("sales_count") or 0),
                "total_amount": float(raw.get("total_amount") or 0.0),
                "net_amount": float(raw.get("net_amount") or 0.0),
            }
        )
    return rows


def render_closure_html(
    closure: models.PosClosure,
    settings: Optional[models.PosSettings] = None,
) -> str:
    formatted_date = _format_ticket_datetime(closure.closed_at) or html_escape(
        str(closure.closed_at)
    )
    opened_label = (
        _format_ticket_datetime(closure.opened_at)
        if closure.opened_at
        else None
    )
    range_label = (
        f"{html_escape(opened_label)} → {formatted_date}"
        if opened_label
        else None
    )
    pos_name = html_escape(closure.pos_name or "N/A")
    closed_by = html_escape(closure.closed_by_user_name or "N/A")
    closure_label = html_escape(closure.consecutive or f"CL-{closure.id:06d}")
    sales_count = int(closure.sales_count or 0)
    totals = [
        ("Total ventas", closure.total_amount),
        ("Total efectivo", closure.total_cash),
        ("Total tarjeta", closure.total_card),
        ("Total QR", closure.total_qr),
        ("Total Nequi", closure.total_nequi),
        ("Total Daviplata", closure.total_daviplata),
        ("Total crédito", closure.total_credit),
        ("Total devoluciones", closure.total_refunds),
        ("Neto", closure.net_amount),
        ("Diferencia caja", closure.difference),
    ]
    totals_lines = "\n".join(
        f"{html_escape(label)}: {_format_currency(value)}"
        for label, value in totals
        if float(value or 0) != 0 or label == "Total ventas"
    )
    station_breakdown_rows = _closure_station_breakdown_rows(closure)
    has_auxiliary_station = any(
        row.get("station_type") == "tablet" for row in station_breakdown_rows
    )
    station_breakdown_block = ""
    if station_breakdown_rows and has_auxiliary_station:
        lines = "".join(
            "<tr>"
            f"<td style=\"padding:4px 8px 4px 0;\">{html_escape(row['label'])}</td>"
            f"<td style=\"padding:4px 8px; text-align:center;\">{int(row['sales_count'])}</td>"
            f"<td style=\"padding:4px 8px; text-align:right;\">{_format_currency(row['total_amount'])}</td>"
            f"<td style=\"padding:4px 0; text-align:right;\">{_format_currency(row['net_amount'])}</td>"
            "</tr>"
            for row in station_breakdown_rows
        )
        station_breakdown_block = (
            "<p style=\"margin:0 0 8px;\"><strong>Desglose por estación</strong></p>"
            "<table style=\"width:100%; border-collapse:collapse; font-size:12px; margin:0 0 12px;\">"
            "<thead>"
            "<tr>"
            "<th style=\"text-align:left; padding:4px 8px 4px 0;\">Estación</th>"
            "<th style=\"text-align:center; padding:4px 8px;\">Ventas</th>"
            "<th style=\"text-align:right; padding:4px 8px;\">Bruto</th>"
            "<th style=\"text-align:right; padding:4px 0;\">Neto</th>"
            "</tr>"
            "</thead>"
            f"<tbody>{lines}</tbody>"
            "</table>"
        )

    return f"""
    <div style="font-family: Arial, sans-serif; color:#111827;">
      <p style="margin:0 0 16px;">
        <strong>Total ventas del dia:</strong> {_format_currency(closure.total_amount)}<br/>
        <strong>Ventas incluidas:</strong> {sales_count}<br/>
        <strong>POS:</strong> {pos_name}<br/>
        <strong>Cerrado por:</strong> {closed_by}<br/>
        <strong>Fecha de cierre:</strong> {formatted_date}
        {f"<br/><strong>Periodo:</strong> {range_label}" if range_label else ""}
      </p>
      <p style="margin:0 0 12px;"><strong>Reporte:</strong> {closure_label}</p>
      <pre style="font-family: Arial, sans-serif; margin:0 0 16px; white-space:pre-wrap;">{totals_lines}</pre>
      {station_breakdown_block}
      <p style="margin:0;"><strong>Notas:</strong> {html_escape(closure.notes or 'Sin notas')}</p>
      <p style="margin:8px 0 0; color:#6b7280;">Adjunto: Reporte Z en PDF.</p>
    </div>
    """


def render_closure_pdf(
    closure: models.PosClosure,
    settings: Optional[models.PosSettings] = None,
) -> bytes:
    profile = _company_profile(settings)
    logo_url = profile.get("logo_url") or ""
    company_name = html_escape(profile.get("name") or "Metrik POS")
    address = html_escape(profile.get("address") or "")
    tax_id = html_escape(profile.get("tax_id") or "")
    formatted_date = _format_ticket_datetime(closure.closed_at) or html_escape(
        str(closure.closed_at)
    )
    opened_label = (
        _format_ticket_datetime(closure.opened_at)
        if closure.opened_at
        else None
    )
    range_label = f"{opened_label} → {formatted_date}" if opened_label else ""
    pos_name = html_escape(closure.pos_name or "N/A")
    closed_by = html_escape(closure.closed_by_user_name or "N/A")
    closure_label = html_escape(closure.consecutive or f"CL-{closure.id:06d}")
    sales_count = int(closure.sales_count or 0)

    payment_rows = [
        ("Efectivo", closure.total_cash),
        ("Tarjeta", closure.total_card),
        ("QR", closure.total_qr),
        ("Nequi", closure.total_nequi),
        ("Daviplata", closure.total_daviplata),
        ("Crédito", closure.total_credit),
    ]
    payment_lines = "\n".join(
        "<tr>"
        f"<td style=\"padding:4px 0;\">{html_escape(label)}</td>"
        f"<td style=\"padding:4px 0; text-align:right;\">{_format_currency(value)}</td>"
        "</tr>"
        for label, value in payment_rows
        if float(value or 0) != 0
    )

    totals_lines = "\n".join(
        "<tr>"
        f"<td style=\"padding:4px 0;\">{html_escape(label)}</td>"
        f"<td style=\"padding:4px 0; text-align:right;\">{_format_currency(value)}</td>"
        "</tr>"
        for label, value in [
            ("Total ventas", closure.total_amount),
            ("Devoluciones", closure.total_refunds),
            ("Neto", closure.net_amount),
            ("Diferencia", closure.difference),
        ]
    )
    station_breakdown_rows = _closure_station_breakdown_rows(closure)
    has_auxiliary_station = any(
        row.get("station_type") == "tablet" for row in station_breakdown_rows
    )
    station_breakdown_lines = "".join(
        "<tr>"
        f"<td style=\"padding:4px 0;\">{html_escape(row['label'])}</td>"
        f"<td style=\"padding:4px 0; text-align:center;\">{int(row['sales_count'])}</td>"
        f"<td style=\"padding:4px 0; text-align:right;\">{_format_currency(row['total_amount'])}</td>"
        f"<td style=\"padding:4px 0; text-align:right;\">{_format_currency(row['net_amount'])}</td>"
        "</tr>"
        for row in station_breakdown_rows
    )

    logo_block = ""
    if logo_url:
        logo_block = (
            f"<img src=\"{html_escape(logo_url)}\" "
            "style=\"height:64px; max-width:140px;\"/>"
        )

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color:#0f172a;">
        <table width="100%" cellspacing="0" cellpadding="0" style="width:100%;">
          <tr>
            <td style="padding:24px;">
              <table width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td>
                    <div style="font-size:18px; font-weight:700;">Informe Z</div>
                    <div style="font-size:16px; font-weight:600;">{company_name}</div>
                    {f"<div style='font-size:12px; color:#475569;'>{address}</div>" if address else ""}
                    {f"<div style='font-size:12px; color:#475569;'>NIT: {tax_id}</div>" if tax_id else ""}
                  </td>
                  <td align="right">{logo_block}</td>
                </tr>
              </table>
              <div style="height:16px;"></div>
              <table width="100%" cellspacing="0" cellpadding="0" style="font-size:12px; color:#334155;">
                <tr>
                  <td><strong>POS:</strong> {pos_name}</td>
                  <td align="right"><strong>Fecha de cierre:</strong> {formatted_date}</td>
                </tr>
                {f"<tr><td colspan='2'><strong>Periodo:</strong> {range_label}</td></tr>" if range_label else ""}
                <tr>
                  <td><strong>Cerrado por:</strong> {closed_by}</td>
                  <td align="right"><strong>Ventas incluidas:</strong> {sales_count}</td>
                </tr>
              </table>
              <div style="height:12px;"></div>
              <div style="border-top:1px solid #cbd5f5; padding-top:10px;"></div>
              <div style="font-weight:600; margin:8px 0;">Resumen</div>
              <table width="100%" cellspacing="0" cellpadding="0" style="font-size:12px;">
                {totals_lines}
              </table>
              {"<div style='height:8px;'></div><div style='font-weight:600; margin:8px 0;'>Desglose por estación</div><table width='100%' cellspacing='0' cellpadding='0' style='font-size:12px;'><thead><tr><th align='left' style='padding:4px 0;'>Estación</th><th align='center' style='padding:4px 0;'>Ventas</th><th align='right' style='padding:4px 0;'>Bruto</th><th align='right' style='padding:4px 0;'>Neto</th></tr></thead><tbody>" + station_breakdown_lines + "</tbody></table>" if station_breakdown_lines and has_auxiliary_station else ""}
              {"<div style='height:8px;'></div><div style='font-weight:600; margin:8px 0;'>Tipos de pago</div><table width='100%' cellspacing='0' cellpadding='0' style='font-size:12px;'>" + payment_lines + "</table>" if payment_lines else ""}
              <div style="height:12px;"></div>
              <div style="border-top:1px solid #cbd5f5; padding-top:10px; font-size:12px;">
                <strong>Notas:</strong> {html_escape(closure.notes or 'Sin notas')}
              </div>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    return build_pdf_from_html(
        f"Reporte Z {closure.consecutive or f'CL-{closure.id:06d}'}",
        html,
    )


def _format_report_datetime(value: Optional[datetime]) -> str:
    if not value:
        return "N/A"
    return _format_ticket_datetime(value) or html_escape(str(value))


def _render_report_header(
    title: str,
    closure: models.PosClosure,
    settings: Optional[models.PosSettings] = None,
) -> str:
    profile = _company_profile(settings)
    company_name = html_escape(profile.get("name") or "Metrik POS")
    address = html_escape(profile.get("address") or "")
    closure_label = html_escape(closure.consecutive or f"CL-{closure.id:06d}")
    opened_label = _format_report_datetime(closure.opened_at)
    closed_label = _format_report_datetime(closure.closed_at)
    period_label = (
        f"{opened_label} - {closed_label}" if closure.opened_at else closed_label
    )
    return f"""
      <div style="border-bottom:1px solid #dbe1ec; padding-bottom:10px; margin-bottom:12px;">
        <div style="font-size:18px; font-weight:700; color:#0f172a;">{html_escape(title)}</div>
        <div style="font-size:13px; color:#334155;">{company_name}</div>
        {f"<div style='font-size:11px; color:#64748b;'>{address}</div>" if address else ""}
        <div style="font-size:11px; color:#334155; margin-top:8px;">
          <strong>Cierre:</strong> {closure_label}<br/>
          <strong>POS:</strong> {html_escape(closure.pos_name or "N/A")}<br/>
          <strong>Periodo:</strong> {period_label}
        </div>
      </div>
    """


def render_closure_products_detail_pdf(
    closure: models.PosClosure,
    settings: Optional[models.PosSettings] = None,
) -> bytes:
    rows: List[str] = []
    for sale in sorted(closure.sales or [], key=lambda value: value.created_at or datetime.min):
        document = sale.document_number or (
            f"V-{int(sale.sale_number):06d}" if sale.sale_number else f"#{sale.id}"
        )
        created_label = _format_report_datetime(sale.created_at)
        for item in sale.items or []:
            quantity = float(item.quantity or 0.0)
            unit_price = float(item.unit_price or 0.0)
            line_total = float(item.total or (quantity * unit_price))
            rows.append(
                "<tr>"
                f"<td>{html_escape(created_label)}</td>"
                f"<td>{html_escape(document)}</td>"
                f"<td>{html_escape(item.product_name or 'Producto')}</td>"
                f"<td>{html_escape(item.product_sku or '-')}</td>"
                f"<td style='text-align:right;'>{_format_currency(unit_price)}</td>"
                f"<td style='text-align:right;'>{int(quantity) if quantity.is_integer() else quantity:g}</td>"
                f"<td style='text-align:right;'>{_format_currency(line_total)}</td>"
                "</tr>"
            )

    if not rows:
        rows.append(
            "<tr><td colspan='7' style='text-align:center; color:#64748b; padding:16px;'>"
            "No hay productos vendidos en este cierre.</td></tr>"
        )

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color:#0f172a; font-size:12px;">
        <div style="padding:20px 24px;">
          {_render_report_header("Productos vendidos (detalle)", closure, settings=settings)}
          <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">
            <thead>
              <tr style="background:#f8fafc;">
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:left;">Fecha</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:left;">Ticket</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:left;">Producto</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:left;">SKU</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:right;">P. unitario</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:right;">Cant.</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:right;">Total</th>
              </tr>
            </thead>
            <tbody>
              {"".join(rows)}
            </tbody>
          </table>
        </div>
      </body>
    </html>
    """
    return build_pdf_from_html(
        f"Productos vendidos {closure.consecutive or f'CL-{closure.id:06d}'}",
        html,
    )


def render_closure_hourly_sales_pdf(
    closure: models.PosClosure,
    settings: Optional[models.PosSettings] = None,
) -> bytes:
    hour_map: dict[int, dict[str, float]] = {}
    for sale in closure.sales or []:
        created_at = sale.created_at or closure.closed_at
        if not created_at:
            continue
        hour = int(created_at.hour)
        if hour not in hour_map:
            hour_map[hour] = {"tickets": 0.0, "total": 0.0}
        hour_map[hour]["tickets"] += 1
        hour_map[hour]["total"] += float(sale.total or 0.0)

    rows: List[str] = []
    for hour in sorted(hour_map.keys()):
        tickets = int(hour_map[hour]["tickets"])
        total = float(hour_map[hour]["total"])
        end_hour = (hour + 1) % 24
        rows.append(
            "<tr>"
            f"<td>{hour:02d}:00 - {end_hour:02d}:00</td>"
            f"<td style='text-align:right;'>{tickets}</td>"
            f"<td style='text-align:right;'>{_format_currency(total)}</td>"
            "</tr>"
        )

    if not rows:
        rows.append(
            "<tr><td colspan='3' style='text-align:center; color:#64748b; padding:16px;'>"
            "No hay ventas en este cierre.</td></tr>"
        )

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color:#0f172a; font-size:12px;">
        <div style="padding:20px 24px;">
          {_render_report_header("Ventas por hora", closure, settings=settings)}
          <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">
            <thead>
              <tr style="background:#f8fafc;">
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:left;">Hora</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:right;">Tickets</th>
                <th style="border:1px solid #dbe1ec; padding:6px; text-align:right;">Ventas</th>
              </tr>
            </thead>
            <tbody>
              {"".join(rows)}
            </tbody>
          </table>
        </div>
      </body>
    </html>
    """
    return build_pdf_from_html(
        f"Ventas por hora {closure.consecutive or f'CL-{closure.id:06d}'}",
        html,
    )
