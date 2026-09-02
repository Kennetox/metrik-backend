"""Scheduled operational alerts for separated orders and web home content."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

import models
from database import SessionLocal
from services import kora_stock_sanitization, tenant_modules
from services.user_notifications import NotificationDistributionResult, distribute_notification


logger = logging.getLogger("kensar.operational_notifications")
BOGOTA_TZ = ZoneInfo("America/Bogota")
SEPARATED_DUE_SOON_DAYS = 3
SLIDER_CHANGE_SOON_DAYS = 35
SLIDER_RENEW_DAYS = 45
VIDEO_CHANGE_SOON_DAYS = 21
VIDEO_RENEW_DAYS = 28


def _format_cop(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


def _bogota_now(reference_time: datetime | None) -> datetime:
    current = reference_time or datetime.now(BOGOTA_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=BOGOTA_TZ)
    return current.astimezone(BOGOTA_TZ)


def _database_now(reference_time: datetime | None) -> datetime:
    if reference_time is None:
        return datetime.utcnow()
    if reference_time.tzinfo is None:
        return reference_time
    return reference_time.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def dispatch_separated_order_notifications(
    db: Session,
    *,
    tenant_id: int,
    reference_time: datetime | None = None,
) -> NotificationDistributionResult | None:
    """Sends one daily summary when open separated orders require follow-up."""

    now = _database_now(reference_time)
    due_limit = now + timedelta(days=SEPARATED_DUE_SOON_DAYS)
    rows = (
        db.query(models.SeparatedOrder)
        .join(models.Sale, models.Sale.id == models.SeparatedOrder.sale_id)
        .filter(
            models.SeparatedOrder.tenant_id == tenant_id,
            models.SeparatedOrder.status == "reservado",
            models.SeparatedOrder.balance > 0.01,
            models.SeparatedOrder.due_date.isnot(None),
            models.SeparatedOrder.due_date <= due_limit,
            ~models.Sale.status.in_(["voided", "cancelled"]),
        )
        .order_by(models.SeparatedOrder.due_date.asc(), models.SeparatedOrder.id.asc())
        .all()
    )
    if not rows:
        return None

    overdue = [row for row in rows if row.due_date and row.due_date < now]
    due_soon = [row for row in rows if row not in overdue]
    total_balance = sum(float(row.balance or 0) for row in rows)
    parts: list[str] = []
    if overdue:
        parts.append(f"{len(overdue)} vencido{'s' if len(overdue) != 1 else ''}")
    if due_soon:
        parts.append(f"{len(due_soon)} por vencer")
    summary = " y ".join(parts)
    message = (
        f"Hay {summary}, con un saldo pendiente total de ${_format_cop(total_balance)}. "
        "Revisa los clientes y fechas para hacer seguimiento."
    )
    local_now = _bogota_now(reference_time)
    payload_orders = []
    for row in rows:
        is_overdue = bool(row.due_date and row.due_date < now)
        day_distance = abs((row.due_date.date() - now.date()).days) if row.due_date else 0
        payload_orders.append(
            {
                "id": int(row.id),
                "document_number": row.sale_document_number,
                "customer_name": row.customer_name or "Cliente sin nombre",
                "customer_phone": row.customer_phone,
                "balance": round(float(row.balance or 0), 2),
                "due_date": row.due_date.isoformat() if row.due_date else None,
                "status": "overdue" if is_overdue else "due_soon",
                "day_distance": day_distance,
            }
        )
    return distribute_notification(
        db,
        tenant_id=tenant_id,
        source="sistema",
        category="separated_follow_up",
        severity="critical" if overdue else "warning",
        module_id="documents",
        required_permission="documents.separated_orders",
        title="Separados que requieren seguimiento",
        message=message,
        action_label="Revisar separados",
        action_href="/dashboard",
        dedupe_key=f"operations:separated:{local_now.date().isoformat()}",
        supersede_dedupe_prefix="operations:separated:",
        payload={
            "generated_at": now.isoformat(),
            "overdue_count": len(overdue),
            "due_soon_count": len(due_soon),
            "total_balance": round(total_balance, 2),
            "due_soon_days": SEPARATED_DUE_SOON_DAYS,
            "orders": payload_orders,
        },
        expires_at=now + timedelta(hours=36),
    )


def _content_item(*, kind: str, slot: int, updated_at: datetime | None, now: datetime) -> dict:
    age_days = max(0, (now - updated_at).days) if updated_at else None
    renew_days = SLIDER_RENEW_DAYS if kind == "slider" else VIDEO_RENEW_DAYS
    return {
        "kind": kind,
        "slot": int(slot),
        "content_updated_at": updated_at.isoformat() if updated_at else None,
        "age_days": age_days,
        "status": "renew" if age_days is None or age_days >= renew_days else "change_soon",
    }


def dispatch_web_content_renewal_notifications(
    db: Session,
    *,
    tenant_id: int,
    reference_time: datetime | None = None,
) -> NotificationDistributionResult | None:
    """Sends one weekly digest for enabled home assets approaching renewal."""

    now = _database_now(reference_time)
    slider_limit = now - timedelta(days=SLIDER_CHANGE_SOON_DAYS)
    video_limit = now - timedelta(days=VIDEO_CHANGE_SOON_DAYS)
    sliders = (
        db.query(models.WebCatalogHomeSlider)
        .filter(
            models.WebCatalogHomeSlider.tenant_id == tenant_id,
            models.WebCatalogHomeSlider.enabled.is_(True),
            models.WebCatalogHomeSlider.image_url.isnot(None),
        )
        .order_by(models.WebCatalogHomeSlider.slot.asc())
        .all()
    )


    videos = (
        db.query(models.WebCatalogHomeVideo)
        .filter(
            models.WebCatalogHomeVideo.tenant_id == tenant_id,
            models.WebCatalogHomeVideo.enabled.is_(True),
            models.WebCatalogHomeVideo.video_url.isnot(None),
        )
        .order_by(models.WebCatalogHomeVideo.slot.asc())
        .all()
    )
    content = [
        _content_item(kind="slider", slot=row.slot, updated_at=row.content_updated_at, now=now)
        for row in sliders
        if row.content_updated_at is None or row.content_updated_at <= slider_limit
    ]
    content.extend(
        _content_item(kind="video", slot=row.slot, updated_at=row.content_updated_at, now=now)
        for row in videos
        if row.content_updated_at is None or row.content_updated_at <= video_limit
    )
    if not content:
        return None

    renew_count = sum(1 for item in content if item["status"] == "renew")
    soon_count = len(content) - renew_count
    parts: list[str] = []
    if renew_count:
        parts.append(f"{renew_count} para renovar")
    if soon_count:
        parts.append(f"{soon_count} para cambiar pronto")
    local_now = _bogota_now(reference_time)
    iso_year, iso_week, _ = local_now.isocalendar()
    return distribute_notification(
        db,
        tenant_id=tenant_id,
        source="Comercio Web",
        category="web_content_renewal",
        severity="warning" if renew_count else "info",
        module_id="commerce_web",
        required_permission="commerce_web.manage",
        title="Contenido del inicio pendiente por renovar",
        message=f"Encontré {' y '.join(parts)} entre los sliders y videos activos de la tienda.",
        action_label="Revisar contenido",
        action_href="/dashboard/comercio-web",
        dedupe_key=f"operations:web-content:{iso_year}-W{iso_week:02d}",
        supersede_dedupe_prefix="operations:web-content:",
        payload={
            "generated_at": now.isoformat(),
            "renew_count": renew_count,
            "change_soon_count": soon_count,
            "content": content,
        },
        expires_at=now + timedelta(days=8),
    )


def dispatch_stock_sanitization_notifications(
    db: Session,
    *,
    tenant_id: int,
    reference_time: datetime | None = None,
) -> NotificationDistributionResult | None:
    """Offers one ready stock-cleanup plan when the operation has capacity."""

    context = kora_stock_sanitization.read_operational_context(
        db,
        tenant_id=tenant_id,
        reference_time=reference_time,
    )
    if not context.automatic_plan_allowed:
        return None
    requested_count = 8 if (context.available_people or 0) == 1 else 12
    if (context.available_people or 0) >= 3:
        requested_count = 15
    result = kora_stock_sanitization.retrieve_or_create_plan(
        db,
        tenant_id=tenant_id,
        requested_count=requested_count,
        trigger="automatic",
        reference_time=reference_time,
    )
    plan = result.plan
    if plan is None:
        return None
    plan_payload = json.loads(
        json.dumps(
            kora_stock_sanitization.serialize_plan(plan),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
    )
    receiving_text = (
        f" Hay {context.open_receiving_count} recepción activa y reservé dos personas para ella."
        if context.open_receiving_count
        else " No hay recepciones abiertas."
    )
    scheduled_text = (
        f"Según el horario, hay {context.scheduled_people} personas en turno"
        if context.scheduled_people is not None
        else "No pude confirmar el turno publicado"
    )
    available_text = (
        f" y quedan {context.available_people} con capacidad estimada"
        if context.available_people is not None
        else ""
    )
    message = (
        f"{scheduled_text}{available_text}; hubo {context.sales_count_30m} venta"
        f"{'s' if context.sales_count_30m != 1 else ''} en los últimos 30 minutos."
        f"{receiving_text} Preparé {plan.selected_count} productos para revisar en Metrik Stock."
    )
    return distribute_notification(
        db,
        tenant_id=tenant_id,
        source="kora",
        category="stock_sanitization",
        severity="info",
        module_id="movements",
        required_permission="movements.view",
        title="Oportunidad para sanear el inventario",
        message=message,
        action_label="Ver lista propuesta",
        action_href=f"/dashboard/movements?kora_plan={plan.id}",
        dedupe_key=f"kora:stock-sanitization:{plan.id}",
        supersede_dedupe_prefix="kora:stock-sanitization:",
        payload={
            "generated_at": context.generated_at.isoformat(),
            "plan_id": int(plan.id),
            "plan_code": plan.code,
            "selected_count": int(plan.selected_count),
            "negative_sku_count": int(plan.negative_sku_count),
            "plan": plan_payload,
        },
        expires_at=plan.expires_at,
    )


def run_operational_notification_dispatch(
    reference_time: datetime | None = None,
) -> dict[str, int | str]:
    """Evaluates active tenants; dedupe keeps repeated scheduler runs idempotent."""

    db = SessionLocal()
    result: dict[str, int | str] = {
        "status": "ok",
        "tenants_checked": 0,
        "notifications_created": 0,
        "failed": 0,
    }
    try:
        tenants = db.query(models.Tenant).filter(models.Tenant.is_active.is_(True)).all()
        for tenant in tenants:
            result["tenants_checked"] = int(result["tenants_checked"]) + 1
            try:
                separated = dispatch_separated_order_notifications(
                    db, tenant_id=int(tenant.id), reference_time=reference_time
                )
                if separated:
                    result["notifications_created"] = int(result["notifications_created"]) + separated.created_count
                if tenant_modules.is_module_enabled(tenant.enabled_modules, "commerce_web"):
                    content = dispatch_web_content_renewal_notifications(
                        db, tenant_id=int(tenant.id), reference_time=reference_time
                    )
                    if content:
                        result["notifications_created"] = int(result["notifications_created"]) + content.created_count
                stock_plan = dispatch_stock_sanitization_notifications(
                    db, tenant_id=int(tenant.id), reference_time=reference_time
                )
                if stock_plan:
                    result["notifications_created"] = int(result["notifications_created"]) + stock_plan.created_count
            except Exception:
                db.rollback()
                result["failed"] = int(result["failed"]) + 1
                logger.exception("No se pudieron generar avisos operativos (tenant_id=%s)", tenant.id)
        return result
    finally:
        db.close()
