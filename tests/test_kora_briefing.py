from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import schemas
from routers.kora import _build_briefing_response


def _make_summary(*, today_sales: float, today_tickets: int, month_sales: float, month_tickets: int):
    return schemas.DashboardSummary(
        today_sales_total=today_sales,
        today_tickets=today_tickets,
        today_avg_ticket=today_sales / today_tickets if today_tickets else 0.0,
        today_change_count=0,
        today_change_extra_total=0.0,
        today_change_refund_total=0.0,
        month_sales_total=month_sales,
        month_tickets=month_tickets,
        month_avg_ticket=month_sales / month_tickets if month_tickets else 0.0,
        month_change_count=0,
        month_change_extra_total=0.0,
        month_change_refund_total=0.0,
        payment_methods=[],
        last_7_days=[],
        trend_days=[],
    )


def _make_inventory(*, critical_count: int, low_stock_count: int, reorder_count: int):
    status_rows = [
        schemas.InventoryStatusRow(product_id=1, product_name="Producto bajo", qty_on_hand=2, status="low"),
        schemas.InventoryStatusRow(product_id=2, product_name="Producto crítico", qty_on_hand=0, status="critical"),
    ]
    return schemas.InventoryOverview(
        summary=schemas.InventorySummary(
            total_qty=100,
            low_stock_count=low_stock_count,
            critical_count=critical_count,
            anomaly_count=0,
            reorder_count=reorder_count,
        ),
        recent_movements=[],
        status_rows=status_rows,
    )


def test_kora_briefing_alert_prioritizes_sales_drop():
    day_of_month = max(datetime.now(ZoneInfo("America/Bogota")).day, 1)
    summary = _make_summary(
        today_sales=0.0,
        today_tickets=0,
        month_sales=100.0 * day_of_month,
        month_tickets=20 * day_of_month,
    )
    inventory = _make_inventory(critical_count=1, low_stock_count=1, reorder_count=1)
    web_orders = [
        SimpleNamespace(payment_status="pending", fulfillment_status="processing", status="active"),
    ]

    briefing = _build_briefing_response(
        summary=summary,
        inventory=inventory,
        web_orders=web_orders,
        role="Administrador",
    )

    assert briefing.state == "alert"
    assert briefing.role == "Administrador"
    assert briefing.signals[0].key == "sales-drop"
    assert briefing.signals[1].key in {"inventory-critical", "web-queue"}
    assert briefing.conversation_starters


def test_kora_briefing_role_changes_medium_signal_order():
    day_of_month = max(datetime.now(ZoneInfo("America/Bogota")).day, 1)
    summary = _make_summary(
        today_sales=100.0,
        today_tickets=20,
        month_sales=100.0 * day_of_month,
        month_tickets=20 * day_of_month,
    )
    inventory = _make_inventory(critical_count=0, low_stock_count=2, reorder_count=3)
    web_orders = []

    admin_briefing = _build_briefing_response(
        summary=summary,
        inventory=inventory,
        web_orders=web_orders,
        role="Administrador",
    )
    vendor_briefing = _build_briefing_response(
        summary=summary,
        inventory=inventory,
        web_orders=web_orders,
        role="Vendedor",
    )

    assert admin_briefing.state == "watch"
    assert vendor_briefing.state == "watch"
    assert admin_briefing.signals[0].key == "inventory-reorder"
    assert admin_briefing.signals[1].key == "inventory-low"
    assert vendor_briefing.signals[0].key == "inventory-low"
    assert vendor_briefing.signals[1].key == "inventory-reorder"
