from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from io import BytesIO

import models
import schemas
from database import get_db
from dependencies import get_current_tenant_id, require_permission


router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_permission("dashboard.view"))],
)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_bogota_date(dt: datetime, bogota_tz: ZoneInfo) -> datetime.date:
    return _ensure_utc(dt).astimezone(bogota_tz).date()


def _sale_cash_total(sale: models.Sale) -> float:
    if sale.is_separated:
        return float(sale.paid_amount or 0.0)
    return float(sale.total or 0.0)


def _is_cash_method(method: str | None) -> bool:
    if not method:
        return False
    normalized = method.lower()
    return (
        normalized == "cash"
        or normalized == "efectivo"
        or "cash" in normalized
        or "efectivo" in normalized
    )


def _parse_adjustment_payments(payload: object) -> list[tuple[str, float]]:
    if not isinstance(payload, dict):
        return []
    payments = payload.get("payments")
    if not isinstance(payments, list):
        return []
    results: list[tuple[str, float]] = []
    for entry in payments:
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
        results.append((method, numeric))
    return results


def _collect_sale_adjustments(
    db: Session,
    sale_ids: list[int],
    tenant_id: int,
):
    if not sale_ids:
        return {}, {}
    adjustments_query = (
        db.query(models.DocumentAdjustment)
        .filter(models.DocumentAdjustment.doc_type == "sale")
        .filter(models.DocumentAdjustment.doc_id.in_(sale_ids))
    )
    if tenant_id is not None:
        adjustments_query = adjustments_query.filter(
            models.DocumentAdjustment.tenant_id == tenant_id
        )
    adjustments = adjustments_query.order_by(
        models.DocumentAdjustment.created_at.desc()
    ).all()
    latest_payment_adjustment: dict[int, models.DocumentAdjustment] = {}
    total_delta: dict[int, float] = defaultdict(float)
    for adjustment in adjustments:
        total_delta[adjustment.doc_id] += float(adjustment.total_delta or 0.0)
        if adjustment.doc_id not in latest_payment_adjustment:
            payload_payments = _parse_adjustment_payments(adjustment.payload)
            if payload_payments:
                latest_payment_adjustment[adjustment.doc_id] = adjustment
    return latest_payment_adjustment, total_delta


def _resolve_range_bounds(
    range_key: str,
    bogota_tz: ZoneInfo,
    start_date: date | None = None,
) -> tuple[datetime, datetime]:
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    now_bogota = now_utc.astimezone(bogota_tz)
    if start_date is None:
        start_date = now_bogota.date()
        if range_key == "week":
            diff_to_monday = (start_date.weekday()) % 7
            start_date = start_date - timedelta(days=diff_to_monday)
        elif range_key == "month":
            start_date = start_date.replace(day=1)

    start_dt = datetime(
        start_date.year, start_date.month, start_date.day, tzinfo=bogota_tz
    )
    if range_key == "day":
        end_dt = start_dt + timedelta(days=1) - timedelta(milliseconds=1)
    elif range_key == "week":
        end_dt = start_dt + timedelta(days=7) - timedelta(milliseconds=1)
    elif range_key == "month":
        year = start_date.year + (1 if start_date.month == 12 else 0)
        month = 1 if start_date.month == 12 else start_date.month + 1
        end_dt = datetime(year, month, 1, tzinfo=bogota_tz) - timedelta(
            milliseconds=1
        )
    else:
        raise HTTPException(status_code=400, detail="Rango inválido")
    return start_dt, end_dt


@router.get("/payment-methods", response_model=schemas.PaymentMethodsSummary)
def get_payment_methods_summary(
    range: str = "day",
    start_date: Optional[str] = None,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    bogota_tz = ZoneInfo("America/Bogota")
    parsed_start: date | None = None
    if start_date:
        try:
            parsed_start = date.fromisoformat(start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="start_date debe ser YYYY-MM-DD"
            ) from exc
    range_key = range.lower().strip()
    start_dt, end_dt = _resolve_range_bounds(range_key, bogota_tz, parsed_start)
    start_utc = start_dt.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_dt.astimezone(timezone.utc).replace(tzinfo=None)

    sales = (
        db.query(models.Sale)
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= start_utc)
        .filter(models.Sale.created_at <= end_utc)
        .all()
    )
    returns = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.created_at >= start_utc)
        .filter(models.SaleReturn.created_at <= end_utc)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.SaleReturn.adjustment_reference.is_(None))
        .all()
    )
    changes = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.tenant_id == tenant_id)
        .filter(models.SaleChange.created_at >= start_utc)
        .filter(models.SaleChange.created_at <= end_utc)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.tenant_id == tenant_id)
        .filter(
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            )
        )
        .filter(models.SeparatedOrderPayment.paid_at >= start_utc)
        .filter(models.SeparatedOrderPayment.paid_at <= end_utc)
        .all()
    )

    payment_totals = defaultdict(float)
    payment_ticket_sets = defaultdict(set)
    payment_adjustments, _ = _collect_sale_adjustments(
        db, [sale.id for sale in sales], tenant_id
    )

    for sale in sales:
        sale_total = float(sale.total or 0.0)
        cash_total = _sale_cash_total(sale)
        if sale_total <= 0 or cash_total <= 0:
            continue
        paid_amount = float(sale.paid_amount or 0.0)
        change_amount = float(sale.change_amount or 0.0)
        if change_amount <= 0 and paid_amount > 0:
            change_amount = max(0.0, paid_amount - sale_total)
        adjustment = payment_adjustments.get(sale.id)
        adjusted_payments = (
            _parse_adjustment_payments(adjustment.payload)
            if adjustment
            else []
        )
        change_remaining = 0.0 if adjusted_payments else change_amount
        payment_iter = (
            adjusted_payments
            if adjusted_payments
            else [(p.method, float(p.amount or 0.0)) for p in sale.payments]
        )
        for method, payment_amount in payment_iter:
            method = method or "DESCONOCIDO"
            if payment_amount <= 0:
                continue
            if change_remaining > 0 and _is_cash_method(method):
                applied = min(change_remaining, payment_amount)
                payment_amount = max(0.0, payment_amount - applied)
                change_remaining -= applied
            if payment_amount <= 0:
                continue
            payment_totals[method] += payment_amount
            payment_ticket_sets[method].add(sale.id)

    for payment in separated_payments:
        method = payment.method or "DESCONOCIDO"
        payment_totals[method] += float(payment.amount or 0.0)

    for ret in returns:
        for payment in ret.payments:
            method = payment.method or "DESCONOCIDO"
            payment_totals[method] -= float(payment.amount or 0.0)

    for change in changes:
        for payment in change.payments:
            method = payment.method or "DESCONOCIDO"
            payment_totals[method] += float(payment.amount or 0.0)
        if float(change.refund_due or 0.0) > 0:
            payment_totals["cash"] -= float(change.refund_due or 0.0)

    payment_methods: List[schemas.PaymentMethodSummary] = []
    for method, total in payment_totals.items():
        payment_methods.append(
            schemas.PaymentMethodSummary(
                method=method,
                total=float(total),
                tickets=len(payment_ticket_sets.get(method, set())),
            )
        )

    payment_methods.sort(key=lambda entry: entry.total, reverse=True)
    return schemas.PaymentMethodsSummary(methods=payment_methods)


def _summarize_sales(totals_by_day: dict, tickets_by_day: dict, start_date: datetime.date):
    total_net = totals_by_day.get(start_date, 0.0)
    tickets = tickets_by_day.get(start_date, 0)
    avg_ticket = total_net / tickets if tickets > 0 else 0.0
    return total_net, tickets, avg_ticket


@router.get("/summary", response_model=schemas.DashboardSummary)
def get_dashboard_summary(
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Devuelve los KPIs básicos para el inicio:
    - Ventas de hoy
    - Tickets de hoy
    - Promedio ticket hoy
    - Ventas del mes
    - Tickets del mes
    - Promedio ticket mes
    - Totales por método de pago (en el mes)
    - Ventas últimos 7 días
    """

    bogota_tz = ZoneInfo("America/Bogota")
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    now_bogota = now_utc.astimezone(bogota_tz)
    today_start_bogota = datetime(
        now_bogota.year, now_bogota.month, now_bogota.day, tzinfo=bogota_tz
    )
    month_start_bogota = datetime(
        now_bogota.year, now_bogota.month, 1, tzinfo=bogota_tz
    )
    trend_start_bogota = today_start_bogota - timedelta(days=13)

    month_start_utc = month_start_bogota.astimezone(timezone.utc).replace(tzinfo=None)
    trend_start_utc = trend_start_bogota.astimezone(timezone.utc).replace(tzinfo=None)

    sales_month = (
        db.query(models.Sale)
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= month_start_utc)
        .all()
    )
    returns_month = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.created_at >= month_start_utc)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.SaleReturn.adjustment_reference.is_(None))
        .all()
    )
    changes_month = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.tenant_id == tenant_id)
        .filter(models.SaleChange.created_at >= month_start_utc)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments_month = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.tenant_id == tenant_id)
        .filter(
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            )
        )
        .filter(models.SeparatedOrderPayment.paid_at >= month_start_utc)
        .all()
    )
    sales_trend = (
        db.query(models.Sale)
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= trend_start_utc)
        .all()
    )
    returns_trend = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.created_at >= trend_start_utc)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.SaleReturn.adjustment_reference.is_(None))
        .all()
    )
    changes_trend = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.tenant_id == tenant_id)
        .filter(models.SaleChange.created_at >= trend_start_utc)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments_trend = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.tenant_id == tenant_id)
        .filter(
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            )
        )
        .filter(models.SeparatedOrderPayment.paid_at >= trend_start_utc)
        .all()
    )

    totals_by_day = defaultdict(float)
    tickets_by_day = defaultdict(int)
    refunds_by_day = defaultdict(float)
    change_extra_by_day = defaultdict(float)
    change_refund_by_day = defaultdict(float)
    payment_adjustments, total_delta_by_sale = _collect_sale_adjustments(
        db, [sale.id for sale in sales_month], tenant_id
    )

    for sale in sales_month:
        day = _to_bogota_date(sale.created_at, bogota_tz)
        cash_total = _sale_cash_total(sale)
        delta = float(total_delta_by_sale.get(sale.id, 0.0))
        effective_total = cash_total + delta
        if effective_total > 0:
            totals_by_day[day] += effective_total
            tickets_by_day[day] += 1

    for payment in separated_payments_month:
        day = _to_bogota_date(payment.paid_at, bogota_tz)
        totals_by_day[day] += float(payment.amount or 0.0)

    for ret in returns_month:
        day = _to_bogota_date(ret.created_at, bogota_tz)
        refund_total = sum(float(p.amount or 0.0) for p in ret.payments) or float(ret.total_refund or 0.0)
        refunds_by_day[day] += refund_total

    for change in changes_month:
        day = _to_bogota_date(change.created_at, bogota_tz)
        change_extra_by_day[day] += float(change.extra_payment or 0.0)
        change_refund_by_day[day] += float(change.refund_due or 0.0)

    trend_totals_by_day = defaultdict(float)
    trend_tickets_by_day = defaultdict(int)
    trend_refunds_by_day = defaultdict(float)
    trend_change_extra_by_day = defaultdict(float)
    trend_change_refund_by_day = defaultdict(float)
    _, trend_total_delta_by_sale = _collect_sale_adjustments(
        db, [sale.id for sale in sales_trend], tenant_id
    )

    for sale in sales_trend:
        day = _to_bogota_date(sale.created_at, bogota_tz)
        cash_total = _sale_cash_total(sale)
        delta = float(trend_total_delta_by_sale.get(sale.id, 0.0))
        effective_total = cash_total + delta
        if effective_total > 0:
            trend_totals_by_day[day] += effective_total
            trend_tickets_by_day[day] += 1

    for payment in separated_payments_trend:
        day = _to_bogota_date(payment.paid_at, bogota_tz)
        trend_totals_by_day[day] += float(payment.amount or 0.0)

    for ret in returns_trend:
        day = _to_bogota_date(ret.created_at, bogota_tz)
        refund_total = sum(float(p.amount or 0.0) for p in ret.payments) or float(ret.total_refund or 0.0)
        trend_refunds_by_day[day] += refund_total

    for change in changes_trend:
        day = _to_bogota_date(change.created_at, bogota_tz)
        trend_change_extra_by_day[day] += float(change.extra_payment or 0.0)
        trend_change_refund_by_day[day] += float(change.refund_due or 0.0)

    today_date = today_start_bogota.date()
    today_sales_total = (
        totals_by_day[today_date]
        + change_extra_by_day[today_date]
        - refunds_by_day[today_date]
        - change_refund_by_day[today_date]
    )
    today_tickets = tickets_by_day[today_date]
    today_avg_ticket = (
        today_sales_total / today_tickets if today_tickets > 0 else 0.0
    )

    month_sales_total = 0.0
    month_tickets = 0
    for day in totals_by_day.keys():
        if day >= month_start_bogota.date():
            month_sales_total += (
                totals_by_day[day]
                + change_extra_by_day[day]
                - refunds_by_day[day]
                - change_refund_by_day[day]
            )
            month_tickets += tickets_by_day[day]
    month_avg_ticket = (
        month_sales_total / month_tickets if month_tickets > 0 else 0.0
    )

    # --- Métodos de pago (mes actual, flujo real de caja) ---
    payment_totals = defaultdict(float)
    payment_ticket_sets = defaultdict(set)

    for sale in sales_month:
        sale_total = float(sale.total or 0.0)
        cash_total = _sale_cash_total(sale)
        if sale_total <= 0 or cash_total <= 0:
            continue
        paid_amount = float(sale.paid_amount or 0.0)
        change_amount = float(sale.change_amount or 0.0)
        if change_amount <= 0 and paid_amount > 0:
            change_amount = max(0.0, paid_amount - sale_total)
        adjustment = payment_adjustments.get(sale.id)
        adjusted_payments = (
            _parse_adjustment_payments(adjustment.payload)
            if adjustment
            else []
        )
        change_remaining = 0.0 if adjusted_payments else change_amount
        payment_iter = (
            adjusted_payments
            if adjusted_payments
            else [(p.method, float(p.amount or 0.0)) for p in sale.payments]
        )
        for method, payment_amount in payment_iter:
            method = method or "DESCONOCIDO"
            if payment_amount <= 0:
                continue
            if change_remaining > 0 and _is_cash_method(method):
                applied = min(change_remaining, payment_amount)
                payment_amount = max(0.0, payment_amount - applied)
                change_remaining -= applied
            if payment_amount <= 0:
                continue
            payment_totals[method] += payment_amount
            payment_ticket_sets[method].add(sale.id)

    for payment in separated_payments_month:
        method = payment.method or "DESCONOCIDO"
        payment_totals[method] += float(payment.amount or 0.0)

    for ret in returns_month:
        for payment in ret.payments:
            method = payment.method or "DESCONOCIDO"
            payment_totals[method] -= float(payment.amount or 0.0)

    for change in changes_month:
        for payment in change.payments:
            method = payment.method or "DESCONOCIDO"
            payment_totals[method] += float(payment.amount or 0.0)
        if float(change.refund_due or 0.0) > 0:
            payment_totals["cash"] -= float(change.refund_due or 0.0)

    payment_methods: List[schemas.PaymentMethodSummary] = []
    for method, total in payment_totals.items():
        payment_methods.append(
            schemas.PaymentMethodSummary(
                method=method,
                total=float(total),
                tickets=len(payment_ticket_sets.get(method, set())),
            )
        )

    # --- Últimos 14 días (incluye hoy) ---
    trend_map = {}
    trend_days_count = 14
    for offset in range(trend_days_count):
        day = today_start_bogota.date() - timedelta(days=offset)
        trend_map[day] = {"total": 0.0, "tickets": trend_tickets_by_day.get(day, 0)}
        trend_map[day]["total"] = (
            trend_totals_by_day.get(day, 0.0)
            + trend_change_extra_by_day.get(day, 0.0)
            - trend_refunds_by_day.get(day, 0.0)
            - trend_change_refund_by_day.get(day, 0.0)
        )

    trend_days: List[schemas.SalesTrendPoint] = []
    for day in sorted(trend_map.keys()):
        stats = trend_map[day]
        day_dt = datetime(day.year, day.month, day.day, tzinfo=bogota_tz)
        trend_days.append(
            schemas.SalesTrendPoint(
                date=day_dt,
                total=float(stats["total"]),
                tickets=int(stats["tickets"]),
            )
        )
    last_7_days = trend_days[-7:]



    return schemas.DashboardSummary(
        today_sales_total=today_sales_total,
        today_tickets=today_tickets,
        today_avg_ticket=today_avg_ticket,
        month_sales_total=month_sales_total,
        month_tickets=month_tickets,
        month_avg_ticket=month_avg_ticket,
        payment_methods=payment_methods,
        last_7_days=last_7_days,
        trend_days=trend_days,
    )


@router.post("/documents/export/xlsx")
def export_documents_excel(
    payload: schemas.DocumentExportRequest,
    _: object = Depends(require_permission("dashboard.view")),
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="No hay documentos para exportar")

    workbook = Workbook()
    try:
        workbook.calculation_properties.fullCalcOnLoad = True
    except AttributeError:
        pass
    sheet = workbook.active
    sheet.title = "Documentos"

    headers = [
        "Documento",
        "Tipo",
        "Detalle",
        "Total",
        "Metodo",
        "Cliente",
        "POS",
        "Vendedor",
        "Referencia",
        "Estado",
        "Fecha",
    ]
    sheet.append(headers)

    for item in payload.items:
        sheet.append(
            [
                item.document_number,
                item.doc_type,
                item.detail,
                item.total,
                item.method,
                item.customer,
                item.pos,
                item.vendor,
                item.reference,
                item.status,
                item.created_at,
            ]
        )

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)

    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=documentos.xlsx"},
    )


@router.get(
    "/monthly-sales",
    response_model=List[schemas.MonthlySalesPoint],
)
def get_monthly_sales(
    year: Optional[int] = None,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """Devuelve la sumatoria de ventas netas y tickets por mes."""

    target_year = int(year or datetime.utcnow().year)
    if target_year < 1900 or target_year > 2100:
        target_year = datetime.utcnow().year

    bogota_tz = ZoneInfo("America/Bogota")
    year_start = datetime(target_year, 1, 1, tzinfo=bogota_tz).astimezone(
        timezone.utc
    ).replace(tzinfo=None)
    year_end = datetime(target_year + 1, 1, 1, tzinfo=bogota_tz).astimezone(
        timezone.utc
    ).replace(tzinfo=None)

    sales_year = (
        db.query(models.Sale)
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= year_start)
        .filter(models.Sale.created_at < year_end)
        .all()
    )
    returns_year = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.created_at >= year_start)
        .filter(models.SaleReturn.created_at < year_end)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.SaleReturn.adjustment_reference.is_(None))
        .all()
    )
    changes_year = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.tenant_id == tenant_id)
        .filter(models.SaleChange.created_at >= year_start)
        .filter(models.SaleChange.created_at < year_end)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments_year = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.tenant_id == tenant_id)
        .filter(
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            )
        )
        .filter(models.SeparatedOrderPayment.paid_at >= year_start)
        .filter(models.SeparatedOrderPayment.paid_at < year_end)
        .all()
    )

    monthly = {month: {"total": 0.0, "tickets": 0} for month in range(1, 13)}
    _, total_delta_by_sale = _collect_sale_adjustments(
        db, [sale.id for sale in sales_year], tenant_id
    )

    for sale in sales_year:
        net_total = _sale_cash_total(sale)
        if net_total <= 0:
            continue
        month = _to_bogota_date(sale.created_at, bogota_tz).month
        monthly[month]["total"] += net_total
        monthly[month]["tickets"] += 1
        delta = total_delta_by_sale.get(sale.id, 0.0)
        if delta:
            monthly[month]["total"] += float(delta)

    for payment in separated_payments_year:
        month = _to_bogota_date(payment.paid_at, bogota_tz).month
        monthly[month]["total"] += float(payment.amount or 0.0)

    for ret in returns_year:
        month = _to_bogota_date(ret.created_at, bogota_tz).month
        refund_total = sum(float(p.amount or 0.0) for p in ret.payments) or float(ret.total_refund or 0.0)
        monthly[month]["total"] -= refund_total

    for change in changes_year:
        month = _to_bogota_date(change.created_at, bogota_tz).month
        monthly[month]["total"] += float(change.extra_payment or 0.0)
        monthly[month]["total"] -= float(change.refund_due or 0.0)

    return [
        schemas.MonthlySalesPoint(
            month=month,
            total=float(values["total"]),
            tickets=int(values["tickets"]),
        )
        for month, values in sorted(monthly.items())
    ]


@router.get(
    "/daily-sales",
    response_model=List[schemas.SalesTrendPoint],
)
def get_daily_sales(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """Devuelve ventas netas y tickets por día usando la misma lógica del dashboard."""

    bogota_tz = ZoneInfo("America/Bogota")
    today_bogota = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(bogota_tz).date()

    if date_from is None and date_to is None:
        start_date = today_bogota.replace(day=1)
        end_date = today_bogota
    else:
        start_date = date_from or date_to
        end_date = date_to or date_from

    if start_date is None or end_date is None:
        raise HTTPException(status_code=400, detail="Rango de fechas inválido")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="date_from no puede ser mayor que date_to")
    if (end_date - start_date).days > 400:
        raise HTTPException(status_code=400, detail="Rango máximo permitido: 400 días")

    start_utc = datetime(
        start_date.year, start_date.month, start_date.day, tzinfo=bogota_tz
    ).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (
        datetime(end_date.year, end_date.month, end_date.day, tzinfo=bogota_tz)
        + timedelta(days=1)
    ).astimezone(timezone.utc).replace(tzinfo=None)

    sales = (
        db.query(models.Sale)
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= start_utc)
        .filter(models.Sale.created_at < end_utc)
        .all()
    )
    returns = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.created_at >= start_utc)
        .filter(models.SaleReturn.created_at < end_utc)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.SaleReturn.adjustment_reference.is_(None))
        .all()
    )
    changes = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.tenant_id == tenant_id)
        .filter(models.SaleChange.created_at >= start_utc)
        .filter(models.SaleChange.created_at < end_utc)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.tenant_id == tenant_id)
        .filter(
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            )
        )
        .filter(models.SeparatedOrderPayment.paid_at >= start_utc)
        .filter(models.SeparatedOrderPayment.paid_at < end_utc)
        .all()
    )

    totals_by_day = defaultdict(float)
    tickets_by_day = defaultdict(int)
    refunds_by_day = defaultdict(float)
    change_extra_by_day = defaultdict(float)
    change_refund_by_day = defaultdict(float)
    _, total_delta_by_sale = _collect_sale_adjustments(
        db, [sale.id for sale in sales], tenant_id
    )

    for sale in sales:
        day = _to_bogota_date(sale.created_at, bogota_tz)
        cash_total = _sale_cash_total(sale)
        delta = float(total_delta_by_sale.get(sale.id, 0.0))
        effective_total = cash_total + delta
        if effective_total > 0:
            totals_by_day[day] += effective_total
            tickets_by_day[day] += 1

    for payment in separated_payments:
        day = _to_bogota_date(payment.paid_at, bogota_tz)
        totals_by_day[day] += float(payment.amount or 0.0)

    for ret in returns:
        day = _to_bogota_date(ret.created_at, bogota_tz)
        refund_total = sum(float(p.amount or 0.0) for p in ret.payments) or float(ret.total_refund or 0.0)
        refunds_by_day[day] += refund_total

    for change in changes:
        day = _to_bogota_date(change.created_at, bogota_tz)
        change_extra_by_day[day] += float(change.extra_payment or 0.0)
        change_refund_by_day[day] += float(change.refund_due or 0.0)

    points: List[schemas.SalesTrendPoint] = []
    cursor = start_date
    while cursor <= end_date:
        day_total = (
            totals_by_day[cursor]
            + change_extra_by_day[cursor]
            - refunds_by_day[cursor]
            - change_refund_by_day[cursor]
        )
        day_dt = datetime(cursor.year, cursor.month, cursor.day, tzinfo=bogota_tz)
        points.append(
            schemas.SalesTrendPoint(
                date=day_dt,
                total=float(day_total),
                tickets=int(tickets_by_day[cursor]),
            )
        )
        cursor += timedelta(days=1)

    return points
