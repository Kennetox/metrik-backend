from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, joinedload

import crud
import models
from database import SessionLocal
from routers import dashboard as dashboard_router
from services import email as email_service
from services import pdf_utils

logger = logging.getLogger("kensar.monthly_report_email")

BOGOTA_TZ = ZoneInfo("America/Bogota")
MONTH_SHORT = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
WEEKDAY_SHORT = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]


def _month_period(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _previous_month_from_now(now_bogota: datetime) -> tuple[int, int]:
    year = now_bogota.year
    month = now_bogota.month - 1
    if month < 1:
        month = 12
        year -= 1
    return year, month


def _format_money(value: float) -> str:
    return f"${int(round(value or 0)):,}".replace(",", ".")


def _format_count(value: int) -> str:
    return f"{int(value or 0):,}".replace(",", ".")


def _format_compact_money(value: float) -> str:
    raw = float(value or 0.0)
    absolute = abs(raw)
    if absolute >= 1_000_000:
        compact = f"{raw / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"${compact}M"
    if absolute >= 1_000:
        compact = f"{raw / 1_000:.0f}"
        return f"${compact}k"
    return f"${int(round(raw))}"


def _format_percent(value: Optional[float]) -> str:
    if value is None:
        return "Sin base comparativa"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%".replace(".", ",")


def _normalize_recipients(settings: models.PosSettings) -> list[str]:
    values: list[str] = []

    def _append(raw: str | None):
        if not raw:
            return
        for token in str(raw).replace("\n", ",").split(","):
            email = token.strip()
            if email:
                values.append(email)

    closure = settings.closure_email_recipients
    if isinstance(closure, list):
        for item in closure:
            _append(str(item) if item is not None else "")
    elif isinstance(closure, str):
        _append(closure)

    _append(settings.contact_email)

    deduped: list[str] = []
    seen: set[str] = set()
    for email in values:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(email)
    return deduped


def _build_top_lists(db: Session, tenant_id: int, start_utc: datetime, end_utc: datetime) -> tuple[list[dict], list[dict]]:
    sales = (
        db.query(models.Sale)
        .options(joinedload(models.Sale.items))
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= start_utc)
        .filter(models.Sale.created_at < end_utc)
        .filter(models.Sale.status != "voided")
        .all()
    )

    if not sales:
        return [], []

    product_ids: set[int] = set()
    for sale in sales:
        for item in sale.items or []:
            if item.product_id:
                product_ids.add(int(item.product_id))

    group_by_product: dict[int, str] = {}
    if product_ids:
        products = (
            db.query(models.Product)
            .filter(models.Product.tenant_id == tenant_id)
            .filter(models.Product.id.in_(list(product_ids)))
            .all()
        )
        for product in products:
            group_by_product[int(product.id)] = (product.group_name or "").strip()

    top_products_map: dict[str, dict] = {}
    top_groups_map: dict[str, dict] = {}

    for sale in sales:
        for item in sale.items or []:
            quantity = float(item.quantity or 0.0)
            if quantity <= 0:
                continue
            line_total = float(item.total or (float(item.unit_price or 0.0) * quantity) or 0.0)
            if line_total <= 0:
                continue
            name = (item.product_name or "Producto sin nombre").strip() or "Producto sin nombre"
            product_key = f"{item.product_id or 0}:{name.lower()}"
            product_row = top_products_map.get(product_key)
            if product_row is None:
                product_row = {"name": name, "units": 0.0, "total": 0.0}
                top_products_map[product_key] = product_row
            product_row["units"] += quantity
            product_row["total"] += line_total

            group_name = group_by_product.get(int(item.product_id or 0), "") or "Sin grupo"
            group_row = top_groups_map.get(group_name)
            if group_row is None:
                group_row = {"name": group_name, "units": 0.0, "total": 0.0}
                top_groups_map[group_name] = group_row
            group_row["units"] += quantity
            group_row["total"] += line_total

    top_products = sorted(top_products_map.values(), key=lambda row: float(row["total"]), reverse=True)[:5]
    top_groups = sorted(top_groups_map.values(), key=lambda row: float(row["total"]), reverse=True)[:5]
    return top_products, top_groups


def _monthly_change_percent(month_total: float, previous_total: float) -> Optional[float]:
    if previous_total <= 0:
        return None
    return ((month_total - previous_total) / previous_total) * 100.0


def _build_bar_rows(series: list[dict], max_value: float, is_daily: bool) -> str:
    rows: list[str] = []
    for entry in series:
        value = float(entry["total"])
        tickets = int(entry["tickets"])
        ratio = 0 if max_value <= 0 else max(0.0, min(1.0, value / max_value))
        height = max(2, int(120 * ratio)) if value > 0 else 2
        label = escape(str(entry["label"]))
        sublabel = escape(str(entry["sub"] if is_daily else ""))
        top_label = escape(_format_money(value))
        rows.append(
            f"""
            <div class=\"bar-item\">
              <div class=\"bar-value\">{top_label}<br/><span>{tickets}</span></div>
              <div class=\"bar\" style=\"height:{height}px\"></div>
              <div class=\"bar-label\">{label}</div>
              <div class=\"bar-sub\">{sublabel}</div>
            </div>
            """
        )
    return "".join(rows)


def _render_quick_report_html(
    *,
    company_name: str,
    generated_at: datetime,
    year: int,
    month: int,
    monthly_series: list,
    daily_series: list,
    top_products: list[dict],
    top_groups: list[dict],
) -> str:
    month_label = MONTH_SHORT[month - 1]
    generated_label = generated_at.strftime("%d/%m/%Y, %I:%M %p").lower().replace("am", "a.m.").replace("pm", "p.m.")

    monthly_by_month = {int(point.month): point for point in monthly_series}
    annual_data: list[dict] = []
    total_year = 0.0
    total_year_tickets = 0
    best_month = {"label": "-", "total": 0.0}

    for month_idx in range(1, 13):
        point = monthly_by_month.get(month_idx)
        total = float(point.total if point else 0.0)
        tickets = int(point.tickets if point else 0)
        annual_data.append({"label": MONTH_SHORT[month_idx - 1], "sub": "", "total": total, "tickets": tickets})
        total_year += total
        total_year_tickets += tickets
        if total > float(best_month["total"]):
            best_month = {"label": MONTH_SHORT[month_idx - 1], "total": total}

    daily_sorted = sorted(daily_series, key=lambda row: row.date)
    month_start, month_end = _month_period(year, month)
    day_count = month_end.day
    monthly_data: list[dict] = []
    total_month = 0.0
    total_month_tickets = 0
    best_day = {"label": "-", "sub": "", "total": 0.0}

    daily_by_day: dict[int, tuple[float, int]] = {}
    for point in daily_sorted:
        day = int(point.date.day)
        daily_by_day[day] = (float(point.total), int(point.tickets))

    for day in range(1, day_count + 1):
        day_date = date(year, month, day)
        weekday = WEEKDAY_SHORT[day_date.weekday()]
        total, tickets = daily_by_day.get(day, (0.0, 0))
        label = f"{day:02d}"
        monthly_data.append({"label": label, "sub": weekday, "total": total, "tickets": tickets})
        total_month += total
        total_month_tickets += tickets
        if total > float(best_day["total"]):
            best_day = {"label": label, "sub": weekday, "total": total}

    avg_month = total_year / max(1, month)
    avg_daily = total_month / max(1, day_count)

    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year = year - 1
    if prev_year == year:
        previous_total = float(monthly_by_month.get(prev_month).total if monthly_by_month.get(prev_month) else 0.0)
    else:
        previous_series = monthly_series if prev_year == year else []
        previous_total = float(previous_series[prev_month - 1].total) if previous_series and len(previous_series) >= prev_month else 0.0

    month_change = _monthly_change_percent(total_month, previous_total)

    annual_max = max([entry["total"] for entry in annual_data] + [1.0])
    monthly_max = max([entry["total"] for entry in monthly_data] + [1.0])
    chart_ticks = [1, 0.66, 0.33, 0]

    annual_chart = {
        "width": 980,
        "height": 164,
        "top_padding": 30,
        "chart_height": 104,
        "baseline_y": 134,
        "left_padding": 16,
    }
    annual_chart["inner_width"] = annual_chart["width"] - annual_chart["left_padding"] * 2
    annual_chart["slot_width"] = annual_chart["inner_width"] / 12
    annual_chart["bar_width"] = max(26, annual_chart["slot_width"] - 14)

    daily_chart = {
        "width": max(980, len(monthly_data) * 34),
        "height": 174,
        "top_padding": 30,
        "chart_height": 102,
        "baseline_y": 132,
        "left_padding": 8,
    }
    daily_chart["inner_width"] = daily_chart["width"] - daily_chart["left_padding"] * 2
    daily_chart["slot_width"] = daily_chart["inner_width"] / max(len(monthly_data), 1)
    daily_chart["bar_width"] = max(10, daily_chart["slot_width"] - 4)

    annual_grid = "".join(
        f'<line x1="0" y1="{annual_chart["top_padding"] + (1 - tick) * annual_chart["chart_height"]}" '
        f'x2="{annual_chart["width"]}" y2="{annual_chart["top_padding"] + (1 - tick) * annual_chart["chart_height"]}" '
        'stroke="#cbd5e1" stroke-dasharray="4 4" />'
        for tick in chart_ticks
    )
    annual_average_line = ""
    if avg_month > 0:
        avg_y = annual_chart["baseline_y"] - (avg_month / annual_max) * annual_chart["chart_height"]
        annual_average_line = (
            f'<line x1="0" y1="{avg_y}" x2="{annual_chart["width"]}" y2="{avg_y}" '
            'stroke="#10b981" stroke-opacity="0.45" stroke-dasharray="6 6" />'
        )
    annual_bars = []
    for index, month_entry in enumerate(annual_data):
        has_sales = float(month_entry["total"]) > 0
        bar_height = (
            max(24, (float(month_entry["total"]) / annual_max) * annual_chart["chart_height"])
            if has_sales
            else 6
        )
        x = annual_chart["left_padding"] + index * annual_chart["slot_width"] + (
            annual_chart["slot_width"] - annual_chart["bar_width"]
        ) / 2
        y = annual_chart["baseline_y"] - bar_height
        label_x = x + annual_chart["bar_width"] / 2
        annual_bars.append(
            f"""
            <g>
              <text x="{label_x}" y="{max(14, y - 20)}" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">{escape(_format_compact_money(float(month_entry["total"])))}</text>
              <text x="{label_x}" y="{max(25, y - 6)}" text-anchor="middle" font-size="11" font-weight="700" fill="#334155">{escape(str(int(month_entry["tickets"])))}</text>
              <rect x="{x}" y="{y}" width="{annual_chart["bar_width"]}" height="{bar_height}" fill="#334155" />
              <text x="{label_x}" y="{annual_chart["baseline_y"] + 16}" text-anchor="middle" font-size="14" font-weight="700" fill="#334155">{escape(str(month_entry["label"]))}</text>
            </g>
            """
        )
    annual_bars_svg = "".join(annual_bars)

    daily_grid = "".join(
        f'<line x1="0" y1="{daily_chart["top_padding"] + (1 - tick) * daily_chart["chart_height"]}" '
        f'x2="{daily_chart["width"]}" y2="{daily_chart["top_padding"] + (1 - tick) * daily_chart["chart_height"]}" '
        'stroke="#cbd5e1" stroke-dasharray="4 4" />'
        for tick in chart_ticks
    )
    daily_average_line = ""
    if avg_daily > 0:
        avg_y = daily_chart["baseline_y"] - (avg_daily / monthly_max) * daily_chart["chart_height"]
        daily_average_line = (
            f'<line x1="0" y1="{avg_y}" x2="{daily_chart["width"]}" y2="{avg_y}" '
            'stroke="#10b981" stroke-opacity="0.45" stroke-dasharray="6 6" />'
        )
    daily_bars = []
    for index, day_entry in enumerate(monthly_data):
        has_sales = float(day_entry["total"]) > 0
        bar_height = (
            max(22, (float(day_entry["total"]) / monthly_max) * daily_chart["chart_height"])
            if has_sales
            else 3
        )
        x = daily_chart["left_padding"] + index * daily_chart["slot_width"] + (
            daily_chart["slot_width"] - daily_chart["bar_width"]
        ) / 2
        y = daily_chart["baseline_y"] - bar_height
        label_x = x + daily_chart["bar_width"] / 2
        daily_bars.append(
            f"""
            <g>
              <text x="{label_x}" y="{max(14, y - 20)}" text-anchor="middle" font-size="9" font-weight="700" fill="#475569">{escape(_format_compact_money(float(day_entry["total"])))}</text>
              <text x="{label_x}" y="{max(24, y - 7)}" text-anchor="middle" font-size="9" font-weight="700" fill="#334155">{escape(str(int(day_entry["tickets"])))}</text>
              <rect x="{x}" y="{y}" width="{daily_chart["bar_width"]}" height="{bar_height}" fill="#334155" />
              <text x="{label_x}" y="{daily_chart["baseline_y"] + 14}" text-anchor="middle" font-size="11" font-weight="700" fill="#334155">{escape(str(day_entry["label"]))}</text>
              <text x="{label_x}" y="{daily_chart["baseline_y"] + 26}" text-anchor="middle" font-size="9" font-weight="600" fill="#64748b">{escape(str(day_entry["sub"]))}</text>
            </g>
            """
        )
    daily_bars_svg = "".join(daily_bars)

    def render_rank_rows(rows: list[dict]) -> str:
        if not rows:
            return '<div class="rank-empty">Sin datos en el periodo.</div>'
        return "".join(
            f"""
            <div class=\"rank-row\">
              <div class=\"rank-main\">{escape(str(row['name']))}</div>
              <div class=\"rank-sub\">{_format_count(int(round(float(row['units']))))} unidades</div>
              <div class=\"rank-total\">{_format_money(float(row['total']))}</div>
            </div>
            """
            for row in rows
        )

    return f"""
<!doctype html>
<html lang=\"es\">
  <head>
    <meta charset=\"utf-8\" />
    <title>Reporte rapido mensual/anual</title>
    <style>
      @page {{ size: A4 portrait; margin: 20mm 12mm; }}
      body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #0f172a; font-size: 11px; margin: 0; }}
      .header h1 {{ margin: 0; font-size: 24px; }}
      .meta {{ margin-top: 4px; color: #475569; font-size: 11px; }}
      .section {{ margin-top: 12px; border: 1px solid #cbd5e1; border-radius: 12px; padding: 10px; page-break-inside: avoid; }}
      .section h2 {{ margin: 0 0 8px; font-size: 19px; }}
      .cards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-bottom: 8px; }}
      .card {{ border: 1px solid #cbd5e1; border-radius: 12px; padding: 8px 10px; background: #f8fafc; }}
      .card .label {{ font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: .05em; }}
      .card .value {{ font-size: 20px; font-weight: 700; margin-top: 2px; }}
      .card .note {{ margin-top: 3px; color: #64748b; font-size: 10px; }}
      .chart {{ border: 1px solid #cbd5e1; border-radius: 14px; background: #f8fafc; padding: 8px; overflow: hidden; }}
      .chart svg {{ width: 100%; display: block; }}
      .kpi-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
      .rank-title {{ color: #059669; font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 6px; }}
      .rank-row {{ border: 1px solid #cbd5e1; border-radius: 10px; background: #f8fafc; padding: 8px 9px; margin-bottom: 6px; }}
      .rank-main {{ font-size: 15px; font-weight: 700; line-height: 1.15; }}
      .rank-sub {{ font-size: 10px; color: #64748b; margin-top: 1px; }}
      .rank-total {{ font-size: 16px; font-weight: 700; text-align: right; margin-top: -18px; }}
      .rank-empty {{ border: 1px dashed #94a3b8; border-radius: 10px; color: #64748b; padding: 12px; text-align: center; }}
    </style>
  </head>
  <body>
    <div class=\"header\">
      <h1>Reporte rapido mensual/anual</h1>
      <div class=\"meta\">Empresa: {escape(company_name or 'Kensar')}<br/>Generado: {escape(generated_label)}<br/>Año: {year} · Mes: {month_label} {year}</div>
    </div>

    <section class=\"section\">
      <h2>Resumen anual ({year})</h2>
      <div class=\"cards\">
        <div class=\"card\"><div class=\"label\">Venta total</div><div class=\"value\">{_format_money(total_year)}</div><div class=\"note\">Sin base comparativa</div></div>
        <div class=\"card\"><div class=\"label\">Mes líder</div><div class=\"value\">{escape(str(best_month['label']))}</div><div class=\"note\">{_format_money(float(best_month['total']))}</div></div>
        <div class=\"card\"><div class=\"label\">Tickets del año</div><div class=\"value\">{_format_count(total_year_tickets)}</div><div class=\"note\">Movimientos positivos</div></div>
        <div class=\"card\"><div class=\"label\">Promedio mensual</div><div class=\"value\">{_format_money(avg_month)}</div><div class=\"note\">Corte acumulado</div></div>
      </div>
      <div class=\"chart\">
        <svg viewBox="0 0 {annual_chart["width"]} {annual_chart["height"]}" width="100%">
          {annual_grid}
          {annual_average_line}
          {annual_bars_svg}
        </svg>
      </div>
    </section>

    <section class=\"section\">
      <h2>Resumen mensual ({month_label} {year})</h2>
      <div class=\"cards\">
        <div class=\"card\"><div class=\"label\">Venta del mes</div><div class=\"value\">{_format_money(total_month)}</div><div class=\"note\">{_format_percent(month_change)} vs mes anterior</div></div>
        <div class=\"card\"><div class=\"label\">Día líder</div><div class=\"value\">{escape(str(best_day['label']))} {escape(str(best_day['sub']))}</div><div class=\"note\">{_format_money(float(best_day['total']))}</div></div>
        <div class=\"card\"><div class=\"label\">Tickets del mes</div><div class=\"value\">{_format_count(total_month_tickets)}</div><div class=\"note\">Movimientos positivos</div></div>
        <div class=\"card\"><div class=\"label\">Promedio diario</div><div class=\"value\">{_format_money(avg_daily)}</div><div class=\"note\">{max(1, day_count)} días</div></div>
      </div>
      <div class=\"chart\">
        <svg viewBox="0 0 {daily_chart["width"]} {daily_chart["height"]}" width="100%">
          {daily_grid}
          {daily_average_line}
          {daily_bars_svg}
        </svg>
      </div>
    </section>

    <section class=\"section\">
      <div class=\"kpi-grid\">
        <div>
          <div class=\"rank-title\">Top productos</div>
          {render_rank_rows(top_products)}
        </div>
        <div>
          <div class=\"rank-title\">Top grupos</div>
          {render_rank_rows(top_groups)}
        </div>
      </div>
    </section>
  </body>
</html>
"""


def send_monthly_quick_report(
    db: Session,
    *,
    tenant_id: int,
    year: Optional[int] = None,
    month: Optional[int] = None,
    force: bool = True,
    trigger: str = "manual",
) -> dict:
    now_bogota = datetime.now(BOGOTA_TZ)
    if year is None or month is None:
        year, month = _previous_month_from_now(now_bogota)

    if month < 1 or month > 12:
        raise ValueError("Mes inválido para envío de reporte")
    if year < 2000 or year > 2200:
        raise ValueError("Año inválido para envío de reporte")

    if not pdf_utils.can_render_html_pdf():
        raise RuntimeError(
            "El servidor no tiene habilitada la generación de PDF HTML (WeasyPrint no disponible)."
        )

    settings = crud.get_pos_settings(db, tenant_id=tenant_id)
    notifications = settings.notifications or {}
    monthly_enabled = bool(notifications.get("monthly_report_email", False))

    recipients = _normalize_recipients(settings)
    if not recipients:
        raise ValueError("No hay destinatarios configurados para el reporte mensual")

    if trigger == "auto":
        already_sent = (
            db.query(models.MonthlyReportDispatch)
            .filter(models.MonthlyReportDispatch.tenant_id == tenant_id)
            .filter(models.MonthlyReportDispatch.report_year == year)
            .filter(models.MonthlyReportDispatch.report_month == month)
            .filter(models.MonthlyReportDispatch.trigger == "auto")
            .filter(models.MonthlyReportDispatch.status == "sent")
            .first()
        )
        if already_sent and not force:
            return {
                "status": "skipped",
                "detail": "Reporte automático ya enviado para este periodo",
                "period_year": year,
                "period_month": month,
                "recipients": recipients,
            }

    if trigger == "auto" and not monthly_enabled:
        return {
            "status": "skipped",
            "detail": "Envío mensual por correo está desactivado",
            "period_year": year,
            "period_month": month,
            "recipients": recipients,
        }

    period_start, period_end = _month_period(year, month)

    monthly_series = dashboard_router.get_monthly_sales(year=year, tenant_id=tenant_id, db=db)
    daily_series = dashboard_router.get_daily_sales(
        date_from=period_start,
        date_to=period_end,
        tenant_id=tenant_id,
        db=db,
    )

    start_utc = datetime(period_start.year, period_start.month, period_start.day, tzinfo=BOGOTA_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (
        datetime(period_end.year, period_end.month, period_end.day, tzinfo=BOGOTA_TZ) + timedelta(days=1)
    ).astimezone(timezone.utc).replace(tzinfo=None)

    top_products, top_groups = _build_top_lists(db, tenant_id, start_utc, end_utc)

    html = _render_quick_report_html(
        company_name=settings.company_name or "Kensar",
        generated_at=now_bogota,
        year=year,
        month=month,
        monthly_series=monthly_series,
        daily_series=daily_series,
        top_products=top_products,
        top_groups=top_groups,
    )

    subject = f"Reporte rápido mensual/anual - {MONTH_SHORT[month - 1]} {year}"
    pdf_filename = f"reporte_rapido_{year}_{str(month).zfill(2)}.pdf"
    pdf_bytes = pdf_utils.build_pdf_from_html(subject, html)

    dispatch = models.MonthlyReportDispatch(
        tenant_id=tenant_id,
        report_year=year,
        report_month=month,
        trigger=trigger,
        status="pending",
        recipients=recipients,
        subject=subject,
        created_at=datetime.utcnow(),
    )
    db.add(dispatch)
    db.flush()

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            html_body=(
                "<p>Adjuntamos el reporte rápido mensual/anual generado automáticamente desde Kensar.</p>"
            ),
            attachments=[
                (
                    pdf_filename,
                    pdf_bytes,
                    "application/pdf",
                )
            ],
            smtp_config=settings,
        )
        dispatch.status = "sent"
        dispatch.error = None
        db.commit()
        return {
            "status": "sent",
            "period_year": year,
            "period_month": month,
            "recipients": recipients,
        }
    except Exception as exc:
        dispatch.status = "failed"
        dispatch.error = str(exc)
        db.commit()
        raise


def run_auto_monthly_dispatch(reference_time: Optional[datetime] = None) -> dict:
    now_bogota = (reference_time or datetime.now(BOGOTA_TZ)).astimezone(BOGOTA_TZ)

    # Ventana de envío automático: día 1 a 3, después de las 08:00 de Bogotá.
    if now_bogota.day > 3 or now_bogota.hour < 8:
        return {"status": "idle"}

    year, month = _previous_month_from_now(now_bogota)

    db = SessionLocal()
    sent = 0
    skipped = 0
    failed = 0
    try:
        tenants = (
            db.query(models.Tenant)
            .filter(models.Tenant.is_active.is_(True))
            .all()
        )
        for tenant in tenants:
            tenant_id = int(tenant.id)
            settings = crud.get_pos_settings(db, tenant_id=tenant_id)
            notifications = settings.notifications or {}
            if not bool(notifications.get("monthly_report_email", False)):
                skipped += 1
                continue
            try:
                result = send_monthly_quick_report(
                    db,
                    tenant_id=tenant_id,
                    year=year,
                    month=month,
                    force=False,
                    trigger="auto",
                )
                if result.get("status") == "sent":
                    sent += 1
                else:
                    skipped += 1
            except Exception:
                failed += 1
                logger.exception(
                    "No se pudo enviar reporte mensual automático (tenant_id=%s, period=%s-%s)",
                    tenant_id,
                    year,
                    month,
                )
        return {
            "status": "ok",
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
            "period_year": year,
            "period_month": month,
        }
    finally:
        db.close()
