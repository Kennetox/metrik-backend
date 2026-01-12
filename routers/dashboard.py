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


def _sale_net_total(sale: models.Sale) -> float:
    total = float(sale.total or 0.0)
    refunded = float(sale.refunded_total or 0.0)
    net = total - refunded
    return net if net > 0 else 0.0


def _summarize_sales(sales: List[models.Sale]):
    total_net = 0.0
    tickets = 0
    for sale in sales:
        net = _sale_net_total(sale)
        if net <= 0:
            continue
        total_net += net
        tickets += 1

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

    # Usamos UTC. Si en el futuro quieres zona horaria de Colombia,
    # lo ajustamos aquí.
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    month_start = datetime(now.year, now.month, 1)
    seven_days_ago = today_start - timedelta(days=6)

    sales_month = (
        db.query(models.Sale)
        .filter(models.Sale.created_at >= month_start)
        .all()
    )
    sales_today = [sale for sale in sales_month if sale.created_at >= today_start]
    sales_last_7 = (
        db.query(models.Sale)
        .filter(models.Sale.created_at >= seven_days_ago)
        .all()
    )

    today_sales_total, today_tickets, today_avg_ticket = _summarize_sales(
        sales_today
    )
    month_sales_total, month_tickets, month_avg_ticket = _summarize_sales(
        sales_month
    )

    # --- Métodos de pago (mes actual) ---
    payment_totals = defaultdict(float)
    payment_ticket_sets = defaultdict(set)

    for sale in sales_month:
        sale_total = float(sale.total or 0.0)
        net_total = _sale_net_total(sale)
        if sale_total <= 0:
            continue

        ratio = net_total / sale_total if sale_total > 0 else 0.0
        for payment in sale.payments:
            method = payment.method or "DESCONOCIDO"
            payment_totals[method] += float(payment.amount or 0.0) * ratio
            if net_total > 0:
                payment_ticket_sets[method].add(sale.id)

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
    bogota_tz = ZoneInfo("America/Bogota")
    trend_map = {}
    for sale in sales_last_7:
        created_at = sale.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        day = created_at.astimezone(bogota_tz).date()
        if day not in trend_map:
            trend_map[day] = {"total": 0.0, "tickets": 0}

        net_total = _sale_net_total(sale)
        if net_total <= 0:
            continue

        trend_map[day]["total"] += net_total
        trend_map[day]["tickets"] += 1

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

    year_start = datetime(target_year, 1, 1)
    year_end = datetime(target_year + 1, 1, 1)

    sales_year = (
        db.query(models.Sale)
        .filter(models.Sale.created_at >= year_start)
        .filter(models.Sale.created_at < year_end)
        .all()
    )

    monthly = {month: {"total": 0.0, "tickets": 0} for month in range(1, 13)}

    for sale in sales_year:
        net_total = _sale_net_total(sale)
        if net_total <= 0:
            continue
        month = sale.created_at.month
        monthly[month]["total"] += net_total
        monthly[month]["tickets"] += 1

    return [
        schemas.MonthlySalesPoint(
            month=month,
            total=float(values["total"]),
            tickets=int(values["tickets"]),
        )
        for month, values in sorted(monthly.items())
    ]
