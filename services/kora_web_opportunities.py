"""Kora analysis and notification producer for unpublished web opportunities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

import crud, models
from database import SessionLocal
from services import tenant_modules
from services.user_notifications import distribute_notification


logger = logging.getLogger("kensar.kora_web_opportunities")
BOGOTA_TZ = ZoneInfo("America/Bogota")


@dataclass(frozen=True)
class WebOpportunityItem:
    product_id: int
    product_name: str
    sku: str | None
    group_name: str | None
    qty_on_hand: float
    units_7d: float
    units_lookback: float
    revenue_lookback: float
    last_sale_at: datetime | None
    readiness_score: int
    missing_web_fields: tuple[str, ...]
    score: float
    reason: str


@dataclass(frozen=True)
class WebOpportunityAnalysis:
    generated_at: datetime
    state: Literal["opportunities", "no_sales", "no_candidates"]
    lookback_days: int
    analyzed_product_count: int
    headline: str
    items: tuple[WebOpportunityItem, ...]


@dataclass(frozen=True)
class WebOpportunityDispatchResult:
    analysis: WebOpportunityAnalysis
    recipient_count: int
    created_count: int
    duplicate_count: int


def _merge_metric(
    metrics: dict[int, dict[str, object]],
    *,
    product_id: int | None,
    units_7d: float,
    units_lookback: float,
    revenue_lookback: float,
    last_sale_at: datetime | None,
) -> None:
    if not product_id:
        return
    current = metrics.setdefault(
        int(product_id),
        {
            "units_7d": 0.0,
            "units_lookback": 0.0,
            "revenue_lookback": 0.0,
            "last_sale_at": None,
        },
    )
    current["units_7d"] = float(current["units_7d"]) + max(float(units_7d or 0), 0)
    current["units_lookback"] = float(current["units_lookback"]) + max(
        float(units_lookback or 0), 0
    )
    current["revenue_lookback"] = float(current["revenue_lookback"]) + max(
        float(revenue_lookback or 0), 0
    )
    previous_last_sale = current["last_sale_at"]
    if last_sale_at and (previous_last_sale is None or last_sale_at > previous_last_sale):
        current["last_sale_at"] = last_sale_at


def analyze_web_opportunities(
    db: Session,
    *,
    tenant_id: int,
    lookback_days: int = 30,
    max_items: int = 8,
    reference_time: datetime | None = None,
) -> WebOpportunityAnalysis:
    """Ranks active, in-stock and unpublished products using recent POS sales."""

    lookback_days = max(14, min(int(lookback_days or 30), 90))
    max_items = max(1, min(int(max_items or 8), 20))
    generated_at = reference_time or datetime.utcnow()
    query_now = generated_at.replace(tzinfo=None) if generated_at.tzinfo else generated_at
    lookback_start = query_now - timedelta(days=lookback_days)
    recent_start = query_now - timedelta(days=min(7, lookback_days))
    metrics: dict[int, dict[str, object]] = {}

    sale_rows = (
        db.query(
            models.SaleItem.product_id.label("product_id"),
            func.coalesce(func.sum(models.SaleItem.quantity), 0).label("units_lookback"),
            func.coalesce(
                func.sum(
                    case(
                        (models.Sale.created_at >= recent_start, models.SaleItem.quantity),
                        else_=0,
                    )
                ),
                0,
            ).label("units_7d"),
            func.coalesce(func.sum(models.SaleItem.total), 0).label("revenue_lookback"),
            func.max(models.Sale.created_at).label("last_sale_at"),
        )
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.created_at >= lookback_start)
        .group_by(models.SaleItem.product_id)
        .all()
    )
    for row in sale_rows:
        _merge_metric(
            metrics,
            product_id=row.product_id,
            units_7d=row.units_7d,
            units_lookback=row.units_lookback,
            revenue_lookback=row.revenue_lookback,
            last_sale_at=row.last_sale_at,
        )

    change_rows = (
        db.query(
            models.SaleChangeNewItem.product_id.label("product_id"),
            func.coalesce(func.sum(models.SaleChangeNewItem.quantity), 0).label("units_lookback"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            models.SaleChange.created_at >= recent_start,
                            models.SaleChangeNewItem.quantity,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("units_7d"),
            func.coalesce(func.sum(models.SaleChangeNewItem.total), 0).label("revenue_lookback"),
            func.max(models.SaleChange.created_at).label("last_sale_at"),
        )
        .join(models.SaleChange, models.SaleChange.id == models.SaleChangeNewItem.change_id)
        .filter(models.SaleChange.tenant_id == tenant_id)
        .filter(models.SaleChange.status == "confirmed")
        .filter(models.SaleChange.created_at >= lookback_start)
        .group_by(models.SaleChangeNewItem.product_id)
        .all()
    )
    for row in change_rows:
        _merge_metric(
            metrics,
            product_id=row.product_id,
            units_7d=row.units_7d,
            units_lookback=row.units_lookback,
            revenue_lookback=row.revenue_lookback,
            last_sale_at=row.last_sale_at,
        )

    legacy_rows = (
        db.query(
            models.LegacySaleItem.product_id.label("product_id"),
            func.coalesce(func.sum(models.LegacySaleItem.quantity), 0).label("units_lookback"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            models.LegacySale.created_at >= recent_start,
                            models.LegacySaleItem.quantity,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("units_7d"),
            func.coalesce(func.sum(models.LegacySaleItem.total), 0).label("revenue_lookback"),
            func.max(models.LegacySale.created_at).label("last_sale_at"),
        )
        .join(models.LegacySale, models.LegacySale.id == models.LegacySaleItem.legacy_sale_id)
        .filter(models.LegacySale.tenant_id == tenant_id)
        .filter(~models.LegacySale.status.in_(["voided", "cancelled"]))
        .filter(models.LegacySale.created_at >= lookback_start)
        .filter(models.LegacySaleItem.product_id.isnot(None))
        .group_by(models.LegacySaleItem.product_id)
        .all()
    )
    for row in legacy_rows:
        _merge_metric(
            metrics,
            product_id=row.product_id,
            units_7d=row.units_7d,
            units_lookback=row.units_lookback,
            revenue_lookback=row.revenue_lookback,
            last_sale_at=row.last_sale_at,
        )

    if not metrics:
        return WebOpportunityAnalysis(
            generated_at=generated_at,
            state="no_sales",
            lookback_days=lookback_days,
            analyzed_product_count=0,
            headline="Aún no hay ventas recientes suficientes para detectar oportunidades web.",
            items=(),
        )

    stock_rows = (
        db.query(
            models.InventoryMovement.product_id,
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .filter(models.InventoryMovement.tenant_id == tenant_id)
        .filter(models.InventoryMovement.product_id.in_(metrics.keys()))
        .group_by(models.InventoryMovement.product_id)
        .all()
    )
    stock_by_product = {int(row.product_id): float(row.qty_on_hand or 0) for row in stock_rows}
    products = (
        db.query(models.Product)
        .filter(models.Product.tenant_id == tenant_id)
        .filter(models.Product.id.in_(metrics.keys()))
        .filter(models.Product.active.is_(True))
        .filter(models.Product.service.is_(False))
        .filter(or_(models.Product.web_published.is_(False), models.Product.web_published.is_(None)))
        .all()
    )

    candidates: list[WebOpportunityItem] = []
    for product in products:
        metric = metrics[int(product.id)]
        units_7d = float(metric["units_7d"])
        units_lookback = float(metric["units_lookback"])
        revenue = float(metric["revenue_lookback"])
        qty_on_hand = stock_by_product.get(int(product.id), 0.0)
        if qty_on_hand <= 0:
            continue
        if units_lookback < 2 and units_7d < 2:
            continue

        missing_fields: list[str] = []
        if not (product.image_url or product.image_thumb_url):
            missing_fields.append("imagen")
        if not product.group_name:
            missing_fields.append("categoría")
        if not product.web_short_description:
            missing_fields.append("descripción")
        readiness_score = 3 - len(missing_fields)
        score = (
            (units_7d * 6.0)
            + (units_lookback * 3.0)
            + min(revenue / 100_000.0, 15.0)
            + min(qty_on_hand, 20.0) * 0.2
            + readiness_score
        )
        reason = (
            f"Vendió {units_lookback:g} unidades en {lookback_days} días"
            f" ({units_7d:g} en los últimos 7) y tiene {qty_on_hand:g} disponibles."
        )
        candidates.append(
            WebOpportunityItem(
                product_id=int(product.id),
                product_name=str(product.web_name or product.name),
                sku=product.sku,
                group_name=product.group_name,
                qty_on_hand=round(qty_on_hand, 2),
                units_7d=round(units_7d, 2),
                units_lookback=round(units_lookback, 2),
                revenue_lookback=round(revenue, 2),
                last_sale_at=metric["last_sale_at"],
                readiness_score=readiness_score,
                missing_web_fields=tuple(missing_fields),
                score=round(score, 2),
                reason=reason,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.score,
            item.units_7d,
            item.units_lookback,
            item.revenue_lookback,
        ),
        reverse=True,
    )
    selected = tuple(candidates[:max_items])
    if not selected:
        return WebOpportunityAnalysis(
            generated_at=generated_at,
            state="no_candidates",
            lookback_days=lookback_days,
            analyzed_product_count=len(metrics),
            headline="Revisé las ventas, pero todavía no hay productos con señal y stock suficientes.",
            items=(),
        )
    return WebOpportunityAnalysis(
        generated_at=generated_at,
        state="opportunities",
        lookback_days=lookback_days,
        analyzed_product_count=len(metrics),
        headline=(
            f"Encontré {len(selected)} producto{'s' if len(selected) != 1 else ''} con potencial "
            "para publicar en Comercio Web."
        ),
        items=selected,
    )


def dispatch_web_opportunity_notifications(
    db: Session,
    *,
    tenant_id: int,
    trigger: Literal["weekly", "manual"] = "weekly",
    reference_time: datetime | None = None,
) -> WebOpportunityDispatchResult:
    analysis = analyze_web_opportunities(
        db,
        tenant_id=tenant_id,
        reference_time=reference_time,
    )
    if not analysis.items:
        return WebOpportunityDispatchResult(analysis, 0, 0, 0)

    now_bogota = (reference_time or datetime.now(BOGOTA_TZ))
    if now_bogota.tzinfo is None:
        now_bogota = now_bogota.replace(tzinfo=BOGOTA_TZ)
    else:
        now_bogota = now_bogota.astimezone(BOGOTA_TZ)
    iso_year, iso_week, _ = now_bogota.isocalendar()
    dedupe_suffix = (
        f"{iso_year}-W{iso_week:02d}"
        if trigger == "weekly"
        else f"manual:{now_bogota.date().isoformat()}"
    )
    top_items = analysis.items[:3]
    highlights = ", ".join(
        f"{item.product_name} ({item.units_lookback:g} uds.)" for item in top_items
    )
    message = f"{analysis.headline} Destacan: {highlights}"
    distribution = distribute_notification(
        db,
        tenant_id=tenant_id,
        source="kora",
        category="web_opportunity",
        severity="info",
        module_id="commerce_web",
        required_permission="commerce_web.manage",
        title="Kora encontró oportunidades para la web",
        message=message,
        action_label="Revisar productos",
        action_href="/dashboard/comercio-web",
        dedupe_key=f"kora:web-opportunities:{dedupe_suffix}",
        payload={
            "lookback_days": analysis.lookback_days,
            "product_ids": [item.product_id for item in analysis.items],
            "trigger": trigger,
        },
        expires_at=(analysis.generated_at + timedelta(days=14)),
    )
    return WebOpportunityDispatchResult(
        analysis=analysis,
        recipient_count=distribution.recipient_count,
        created_count=distribution.created_count,
        duplicate_count=distribution.duplicate_count,
    )


def run_weekly_web_opportunity_dispatch(
    reference_time: datetime | None = None,
) -> dict[str, int | str]:
    """Checks every active Commerce Web tenant; dedupe guarantees one weekly digest."""

    db = SessionLocal()
    tenants_checked = 0
    tenants_with_opportunities = 0
    notifications_created = 0
    failed = 0
    try:
        tenants = db.query(models.Tenant).filter(models.Tenant.is_active.is_(True)).all()
        for tenant in tenants:
            if not tenant_modules.is_module_enabled(tenant.enabled_modules, "commerce_web"):
                continue
            tenants_checked += 1
            try:
                result = dispatch_web_opportunity_notifications(
                    db,
                    tenant_id=int(tenant.id),
                    trigger="weekly",
                    reference_time=reference_time,
                )
                if result.analysis.items:
                    tenants_with_opportunities += 1
                notifications_created += result.created_count
            except Exception:
                db.rollback()
                failed += 1
                logger.exception(
                    "No se pudo generar radar web semanal (tenant_id=%s)",
                    tenant.id,
                )
        return {
            "status": "ok",
            "tenants_checked": tenants_checked,
            "tenants_with_opportunities": tenants_with_opportunities,
            "notifications_created": notifications_created,
            "failed": failed,
        }
    finally:
        db.close()
