from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db
from dependencies import require_permission


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
    return float(sale.paid_amount or sale.total or 0.0)


def _summarize_sales(totals_by_day: dict, tickets_by_day: dict, start_date: datetime.date):
    total_net = totals_by_day.get(start_date, 0.0)
    tickets = tickets_by_day.get(start_date, 0)
    avg_ticket = total_net / tickets if tickets > 0 else 0.0
    return total_net, tickets, avg_ticket


@router.get("/summary", response_model=schemas.DashboardSummary)
def get_dashboard_summary(db: Session = Depends(get_db)):
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
    seven_days_ago_bogota = today_start_bogota - timedelta(days=6)

    month_start_utc = month_start_bogota.astimezone(timezone.utc).replace(tzinfo=None)
    seven_days_ago_utc = seven_days_ago_bogota.astimezone(timezone.utc).replace(tzinfo=None)

    sales_month = (
        db.query(models.Sale)
        .filter(models.Sale.created_at >= month_start_utc)
        .all()
    )
    returns_month = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.created_at >= month_start_utc)
        .filter(models.SaleReturn.status == "confirmed")
        .all()
    )
    changes_month = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.created_at >= month_start_utc)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments_month = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.paid_at >= month_start_utc)
        .all()
    )

    totals_by_day = defaultdict(float)
    tickets_by_day = defaultdict(int)
    refunds_by_day = defaultdict(float)
    change_extra_by_day = defaultdict(float)
    change_refund_by_day = defaultdict(float)

    for sale in sales_month:
        day = _to_bogota_date(sale.created_at, bogota_tz)
        cash_total = _sale_cash_total(sale)
        if cash_total > 0:
            totals_by_day[day] += cash_total
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
        for payment in sale.payments:
            method = payment.method or "DESCONOCIDO"
            payment_totals[method] += float(payment.amount or 0.0)
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

    # --- Últimos 7 días (incluye hoy) ---
    trend_map = {}
    for offset in range(7):
        day = (today_start_bogota.date() - timedelta(days=offset))
        trend_map[day] = {"total": 0.0, "tickets": tickets_by_day.get(day, 0)}
        trend_map[day]["total"] = (
            totals_by_day.get(day, 0.0)
            + change_extra_by_day.get(day, 0.0)
            - refunds_by_day.get(day, 0.0)
            - change_refund_by_day.get(day, 0.0)
        )

    last_7_days: List[schemas.SalesTrendPoint] = []
    for day in sorted(trend_map.keys()):
        stats = trend_map[day]
        day_dt = datetime(day.year, day.month, day.day, tzinfo=bogota_tz)
        last_7_days.append(
            schemas.SalesTrendPoint(
                date=day_dt,
                total=float(stats["total"]),
                tickets=int(stats["tickets"]),
            )
        )



    return schemas.DashboardSummary(
        today_sales_total=today_sales_total,
        today_tickets=today_tickets,
        today_avg_ticket=today_avg_ticket,
        month_sales_total=month_sales_total,
        month_tickets=month_tickets,
        month_avg_ticket=month_avg_ticket,
        payment_methods=payment_methods,
        last_7_days=last_7_days,
    )


@router.get(
    "/monthly-sales",
    response_model=List[schemas.MonthlySalesPoint],
)
def get_monthly_sales(
    year: Optional[int] = None,
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
        .filter(models.Sale.created_at >= year_start)
        .filter(models.Sale.created_at < year_end)
        .all()
    )
    returns_year = (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.created_at >= year_start)
        .filter(models.SaleReturn.created_at < year_end)
        .filter(models.SaleReturn.status == "confirmed")
        .all()
    )
    changes_year = (
        db.query(models.SaleChange)
        .filter(models.SaleChange.created_at >= year_start)
        .filter(models.SaleChange.created_at < year_end)
        .filter(models.SaleChange.status == "confirmed")
        .all()
    )
    separated_payments_year = (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.paid_at >= year_start)
        .filter(models.SeparatedOrderPayment.paid_at < year_end)
        .all()
    )

    monthly = {month: {"total": 0.0, "tickets": 0} for month in range(1, 13)}

    for sale in sales_year:
        net_total = _sale_cash_total(sale)
        if net_total <= 0:
            continue
        month = _to_bogota_date(sale.created_at, bogota_tz).month
        monthly[month]["total"] += net_total
        monthly[month]["tickets"] += 1

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
