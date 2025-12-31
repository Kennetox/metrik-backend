from datetime import datetime
from html import escape as html_escape
from typing import List, Optional, Tuple

import models
from services.pdf_utils import build_pdf_from_html, build_simple_pdf

TICKET_MODE = "ticket"
CLASSIC_INVOICE_MODE = "classic_invoice"

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
          margin: 18mm 20mm 22mm 20mm;
          @bottom-right {
            content: "Página " counter(page);
            font-size: 11px;
            color: #94a3b8;
          }
        }
        body {
          font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
          margin: 0;
          background: #f1f5f9;
          color: #0f172a;
        }
        .invoice-wrapper {
          max-width: 860px;
          margin: 0 auto;
          background: #ffffff;
          padding: 32px 36px 40px;
          box-shadow: 0 18px 60px rgba(15, 23, 42, 0.08);
        }
        .invoice-header {
          display: flex;
          justify-content: space-between;
          gap: 24px;
          padding-bottom: 18px;
          border-bottom: 1px solid #e2e8f0;
        }
        .invoice-title {
          font-size: 26px;
          letter-spacing: 0.08em;
          font-weight: 700;
        }
        .invoice-company {
          margin-top: 12px;
          font-size: 13px;
          line-height: 1.4;
          color: #475569;
        }
        .invoice-logo img {
          max-width: 140px;
          max-height: 90px;
          object-fit: contain;
        }
        .invoice-meta {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
          gap: 16px;
          margin-top: 24px;
        }
        .invoice-card {
          border: 1px solid #e2e8f0;
          border-radius: 14px;
          padding: 16px;
          background: #f8fafc;
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
          font-size: 14px;
          color: #0f172a;
        }
        .invoice-card .row {
          display: flex;
          justify-content: space-between;
          font-size: 13px;
          margin-bottom: 4px;
        }
        .invoice-card .row:last-child {
          margin-bottom: 0;
        }
        .invoice-table {
          width: 100%;
          border-collapse: collapse;
          margin-top: 30px;
        }
        .invoice-table thead th {
          background: #e2e8f0;
          padding: 10px 8px;
          font-size: 12px;
          text-transform: uppercase;
          letter-spacing: 0.05em;
          color: #475569;
          text-align: left;
        }
        .invoice-table tbody td {
          padding: 12px 8px;
          border-bottom: 1px solid #e2e8f0;
          font-size: 13px;
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
          grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 18px;
          margin-top: 24px;
        }
        .totals-card,
        .payments-card {
          border: 1px solid #e2e8f0;
          border-radius: 16px;
          padding: 18px;
          background: #ffffff;
        }
        .totals-card .row,
        .payments-card .row {
          display: flex;
          justify-content: space-between;
          font-size: 14px;
          margin-bottom: 8px;
        }
        .totals-card .row.total {
          font-weight: 700;
          font-size: 18px;
          color: #0f172a;
        }
        .payments-card .row.emphasis {
          font-weight: 600;
        }
        .invoice-notes {
          margin-top: 24px;
          border: 1px solid #e2e8f0;
          border-radius: 12px;
          padding: 16px;
          background: #fff7ed;
          color: #7c2d12;
          font-size: 13px;
        }
        .invoice-notes .label {
          color: #7c2d12;
        }
        .invoice-footer {
          margin-top: 30px;
          text-align: center;
          font-size: 12px;
          color: #94a3b8;
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
        profile["logo_url"] = logo_url or ""
    return profile


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
            "<td class=\"number\">1</td>"
            '<td colspan="5" class="muted">Sin artículos registrados</td>'
            "<td class=\"right\">$ 0</td>"
            "</tr>"
        )
    rows = []
    for index, item in enumerate(items_summary, start=1):
        rows.append(
            "<tr>"
            f"<td class=\"number\">{index}</td>"
            f"<td>{_escape_html(item['name'])}</td>"
            f"<td class=\"right\">{_format_quantity(item['quantity'])}</td>"
            f"<td class=\"right\">{_format_money(item['unit_price'])}</td>"
            '<td class="right">0,00%</td>'
            f"<td class=\"right\">{_format_money(item['discount'])}</td>"
            f"<td class=\"right\">{_format_money(item['total'])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _build_payment_rows(sale: models.Sale) -> str:
    if not sale.payments:
        return '<div class="row"><span>Sin pagos registrados</span><span>$ 0</span></div>'
    rows = []
    for payment in sale.payments:
        label = (payment.method or "").replace("_", " ").title() or "Pago"
        rows.append(
            "<div class=\"row\">"
            f"<span>{_escape_html(label)}</span>"
            f"<span>{_format_money(payment.amount)}</span>"
            "</div>"
        )
    return "\n".join(rows)


def _build_invoice_payment_rows(sale: models.Sale) -> str:
    rows = []
    if sale.payments:
        for payment in sale.payments:
            label = (payment.method or "").replace("_", " ").title() or "Pago"
            rows.append(
                "<div class=\"row\">"
                f"<span>{_escape_html(label)}</span>"
                f"<span>{_format_money(payment.amount)}</span>"
                "</div>"
            )
    else:
        label = (
            (sale.main_payment_method or sale.payment_method or "Pago")
            .replace("_", " ")
            .title()
        )
        rows.append(
            "<div class=\"row\">"
            f"<span>{_escape_html(label)}</span>"
            f"<span>{_format_money(sale.paid_amount)}</span>"
            "</div>"
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


def _render_modern_ticket_html(
    sale: models.Sale,
    company: dict,
    items_summary: List[dict],
    subtotal: float,
    line_discount_total: float,
) -> str:
    document_number = sale.document_number or f"V-{sale.id:06d}"
    sale_number = sale.sale_number or sale.id
    formatted_date = _format_ticket_datetime(sale.created_at)
    item_rows = _build_ticket_items_rows(items_summary)
    payment_rows = _build_payment_rows(sale)
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
    barcode_svg = _generate_code39_svg(str(sale_number))
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


def _render_classic_invoice_html(
    sale: models.Sale,
    company: dict,
    items_summary: List[dict],
    subtotal: float,
    line_discount_total: float,
) -> str:
    document_number = sale.document_number or f"V-{sale.id:06d}"
    sale_number = sale.sale_number or sale.id
    formatted_date = _format_ticket_datetime(sale.created_at)
    due_display = formatted_date
    footer_html = _footer_lines(company["footer"])
    table_rows = _build_invoice_table_rows(items_summary)
    cart_discount_label, cart_discount_display = _cart_discount_meta(sale)
    notes_block = _invoice_notes_block(sale.notes)
    payment_rows = _build_invoice_payment_rows(sale)
    paid_amount = float(sale.paid_amount or 0.0)
    total_amount = _effective_total(sale)
    change_amount = float(sale.change_amount or 0.0)
    balance = max(0.0, total_amount - paid_amount)
    payment_status = "Pagado" if balance <= 0.01 else "Pendiente"

    company_lines = [
        _escape_html(company["name"]),
    ]
    for field in ["address", "phone", "email", "tax_id"]:
        if company.get(field):
            company_lines.append(_escape_html(company[field]))
    company_html = "<br />".join(company_lines)

    customer_lines = []
    if sale.customer_name:
        customer_lines.append(_escape_html(sale.customer_name))
    if sale.customer_tax_id:
        customer_lines.append(f"NIT / ID: {_escape_html(sale.customer_tax_id)}")
    if sale.customer_address:
        customer_lines.append(_escape_html(sale.customer_address))
    if sale.customer_phone:
        customer_lines.append(f"Teléfono: {_escape_html(sale.customer_phone)}")
    if sale.customer_email:
        customer_lines.append(f"Email: {_escape_html(sale.customer_email)}")
    if not customer_lines:
        customer_lines.append("Mostrador / Genérico")
    customer_html = "<br />".join(customer_lines)

    parts: List[str] = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8" />',
        f"<title>Factura {_escape_html(document_number)}</title>",
        f"<style>{INVOICE_STYLE}</style>",
        "</head>",
        "<body>",
        '<div class="invoice-wrapper">',
        '<div class="invoice-header">',
        "<div>",
        '<div class="invoice-title">FACTURA</div>',
        f'<div class="invoice-company">{company_html}</div>',
        "</div>",
    ]

    if company["logo_url"]:
        parts.append(
            f'<div class="invoice-logo"><img src="{_escape_html(company["logo_url"])}" alt="Logo" /></div>'
        )
    else:
        parts.append(
            f'<div class="invoice-logo"><div style="font-size:22px;font-weight:700;">{_escape_html(company["name"])}</div></div>'
        )
    parts.append("</div>")

    parts.append('<div class="invoice-meta">')
    parts.append('<div class="invoice-card">')
    parts.append('<div class="label">Cliente</div>')
    parts.append(f'<div class="value">{customer_html}</div>')
    parts.append("</div>")

    parts.append('<div class="invoice-card">')
    parts.append('<div class="label">Documento</div>')
    parts.append(
        f'<div class="row"><span>Factura N°</span><span>{_escape_html(document_number)}</span></div>'
    )
    parts.append(
        f'<div class="row"><span>Ticket</span><span>{_escape_html(str(sale_number))}</span></div>'
    )
    parts.append(
        f'<div class="row"><span>Fecha</span><span>{_escape_html(formatted_date)}</span></div>'
    )
    parts.append(
        f'<div class="row"><span>Vencimiento</span><span>{_escape_html(due_display)}</span></div>'
    )
    parts.append(
        f'<div class="row"><span>Estado del pago</span><span>{payment_status}</span></div>'
    )
    if sale.pos_name:
        parts.append(
            f'<div class="row"><span>Punto de venta</span><span>{_escape_html(sale.pos_name)}</span></div>'
        )
    if sale.vendor_name:
        parts.append(
            f'<div class="row"><span>Vendedor</span><span>{_escape_html(sale.vendor_name)}</span></div>'
        )
    parts.append("</div>")
    parts.append("</div>")

    parts.append('<table class="invoice-table">')
    parts.append(
        "<thead><tr>"
        "<th>#</th>"
        "<th>Descripción de artículo</th>"
        "<th>Cantidad</th>"
        "<th>Precio</th>"
        "<th>Impuesto</th>"
        "<th>Descuento</th>"
        "<th>Total</th>"
        "</tr></thead>"
    )
    parts.append(f"<tbody>{table_rows}</tbody>")
    parts.append("</table>")

    parts.append('<div class="invoice-summary">')
    parts.append('<div class="totals-card">')
    parts.append('<div class="label">Resumen</div>')
    parts.append(
        f'<div class="row"><span>Subtotal</span><span>{_format_money(subtotal)}</span></div>'
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
        f'<div class="row total"><span>Total</span><span>{_format_money(total_amount)}</span></div>'
    )
    parts.append("</div>")

    parts.append('<div class="payments-card">')
    parts.append('<div class="label">Métodos de pago</div>')
    parts.append(payment_rows)
    parts.append(
        f'<div class="row emphasis"><span>Total pagado</span><span>{_format_money(paid_amount)}</span></div>'
    )
    if change_amount > 0:
        parts.append(
            f'<div class="row"><span>Cambio</span><span>{_format_money(change_amount)}</span></div>'
        )
    parts.append(
        f'<div class="row"><span>Cantidad adeudada</span><span>{_format_money(balance)}</span></div>'
    )
    parts.append("</div>")
    parts.append("</div>")

    if notes_block:
        parts.append(notes_block)

    parts.append(f'<div class="invoice-footer">{footer_html}</div>')
    parts.append("</div></body></html>")

    return "".join(parts)


def render_sale_ticket_html(
    sale: models.Sale,
    settings: Optional[models.PosSettings] = None,
    mode: str = TICKET_MODE,
) -> str:
    company = _company_profile(settings)
    items_summary, subtotal, line_discount_total = _collect_sale_items(sale)
    if mode == CLASSIC_INVOICE_MODE:
        return _render_classic_invoice_html(
            sale,
            company,
            items_summary,
            subtotal,
            line_discount_total,
        )
    return _render_modern_ticket_html(
        sale,
        company,
        items_summary,
        subtotal,
        line_discount_total,
    )


def render_sale_ticket_pdf(
    sale: models.Sale,
    settings: Optional[models.PosSettings] = None,
    mode: str = TICKET_MODE,
) -> bytes:
    html = render_sale_ticket_html(sale, settings=settings, mode=mode)
    label = "Factura" if mode == CLASSIC_INVOICE_MODE else "Ticket"
    title = f"{label} {sale.document_number or sale.sale_number or sale.id}"
    return build_pdf_from_html(title, html)


def _format_currency(value: float) -> str:
    amount = float(value or 0.0)
    return f"${amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def render_closure_html(closure: models.PosClosure) -> str:
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
    rows = "".join(
        f"<tr><td>{html_escape(label)}</td>"
        f"<td style='text-align:right'>{_format_currency(value)}</td></tr>"
        for label, value in totals
    )

    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 520px;">
        <h2>Reporte Z {html_escape(closure.consecutive or f'CL-{closure.id:06d}')}</h2>
        <p><strong>POS:</strong> {html_escape(closure.pos_name or 'N/A')}<br/>
        <strong>Cerrado por:</strong> {html_escape(closure.closed_by_user_name)}<br/>
        <strong>Fecha:</strong> {closure.closed_at}</p>
        <p><strong># ventas incluidas:</strong> {closure.sales_count}</p>
        <table style="width:100%;">{rows}</table>
        <p><strong>Notas:</strong> {html_escape(closure.notes or 'Sin notas')}</p>
    </div>
    """


def render_closure_pdf(closure: models.PosClosure) -> bytes:
    lines = [
        f"POS: {closure.pos_name or 'N/A'}",
        f"Cerrado por: {closure.closed_by_user_name}",
        f"Fecha: {closure.closed_at}",
        f"Ventas incluidas: {closure.sales_count}",
        "",
        f"Total ventas: {_format_currency(closure.total_amount)}",
        f"Efectivo: {_format_currency(closure.total_cash)}",
        f"Tarjeta: {_format_currency(closure.total_card)}",
        f"QR: {_format_currency(closure.total_qr)}",
        f"Nequi: {_format_currency(closure.total_nequi)}",
        f"Daviplata: {_format_currency(closure.total_daviplata)}",
        f"Crédito: {_format_currency(closure.total_credit)}",
        f"Devoluciones: {_format_currency(closure.total_refunds)}",
        f"Neto: {_format_currency(closure.net_amount)}",
        f"Diferencia: {_format_currency(closure.difference)}",
        "",
        f"Notas: {closure.notes or 'Sin notas'}",
    ]
    return build_simple_pdf(
        f"Reporte Z {closure.consecutive or f'CL-{closure.id:06d}'}",
        lines,
    )
