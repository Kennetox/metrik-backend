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
    day_count = len(daily_sorted)
    monthly_data: list[dict] = []
    total_month = 0.0
    total_month_tickets = 0
    best_day = {"label": "-", "sub": "", "total": 0.0}

    for point in daily_sorted:
        day_date = point.date
        weekday = WEEKDAY_SHORT[day_date.weekday()]
        label = f"{day_date.day:02d}"
        total = float(point.total)
        tickets = int(point.tickets)
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

    annual_bars = _build_bar_rows(annual_data, annual_max, is_daily=False)
    monthly_bars = _build_bar_rows(monthly_data, monthly_max, is_daily=True)

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
      .bars {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(22px, 1fr)); align-items: end; gap: 4px; min-height: 165px; }}
      .bar-item {{ text-align: center; }}
      .bar-value {{ min-height: 24px; color: #334155; font-size: 10px; line-height: 1.1; }}
      .bar-value span {{ font-size: 9px; color: #64748b; }}
      .bar {{ width: 100%; border-radius: 4px 4px 0 0; background: #334155; }}
      .bar-label {{ font-size: 10px; font-weight: 700; color: #334155; margin-top: 4px; }}
      .bar-sub {{ font-size: 9px; color: #64748b; }}
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
      <div class=\"chart\"><div class=\"bars\">{annual_bars}</div></div>
    </section>

    <section class=\"section\">
      <h2>Resumen mensual ({month_label} {year})</h2>
      <div class=\"cards\">
        <div class=\"card\"><div class=\"label\">Venta del mes</div><div class=\"value\">{_format_money(total_month)}</div><div class=\"note\">{_format_percent(month_change)} vs mes anterior</div></div>
        <div class=\"card\"><div class=\"label\">Día líder</div><div class=\"value\">{escape(str(best_day['label']))} {escape(str(best_day['sub']))}</div><div class=\"note\">{_format_money(float(best_day['total']))}</div></div>
        <div class=\"card\"><div class=\"label\">Tickets del mes</div><div class=\"value\">{_format_count(total_month_tickets)}</div><div class=\"note\">Movimientos positivos</div></div>
        <div class=\"card\"><div class=\"label\">Promedio diario</div><div class=\"value\">{_format_money(avg_daily)}</div><div class=\"note\">{max(1, day_count)} días</div></div>
      </div>
      <div class=\"chart\"><div class=\"bars\">{monthly_bars}</div></div>
    </section>

    <section class=\"section\">
      <h2>KPIs de abajo</h2>
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
