"""Kora analysis and notification producer for unpublished web opportunities."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

import crud, models
from database import SessionLocal
from services import tenant_modules
from services.user_notifications import distribute_notification


logger = logging.getLogger("kensar.kora_web_opportunities")
BOGOTA_TZ = ZoneInfo("America/Bogota")
DEFAULT_MIN_WEB_SALE_PRICE_COP = 10_000.0


def _minimum_web_sale_price() -> float:
    raw = (os.getenv("KORA_WEB_MIN_PRODUCT_PRICE_COP") or "").strip()
    if not raw:
        return DEFAULT_MIN_WEB_SALE_PRICE_COP
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid KORA_WEB_MIN_PRODUCT_PRICE_COP=%r; using %.0f",
            raw,
            DEFAULT_MIN_WEB_SALE_PRICE_COP,
        )
        return DEFAULT_MIN_WEB_SALE_PRICE_COP


def _normalize_group_name(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _normalize_category_key(value: str | None) -> str:
    return (value or "").strip().lower()


def _format_cop(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


@dataclass(frozen=True)
class WebOpportunityItem:
    product_id: int
    product_name: str
    sku: str | None
    group_name: str | None
    sale_price: float
    suggested_category_key: str
    suggested_category_name: str
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
    minimum_sale_price: float
    eligible_group_count: int
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
    minimum_sale_price: float | None = None,
    reference_time: datetime | None = None,
) -> WebOpportunityAnalysis:
    """Ranks active, in-stock and unpublished products using recent POS sales."""

    lookback_days = max(14, min(int(lookback_days or 30), 90))
    max_items = max(1, min(int(max_items or 8), 20))
    effective_minimum_price = max(
        0.0,
        float(minimum_sale_price)
        if minimum_sale_price is not None
        else _minimum_web_sale_price(),
    )
    generated_at = reference_time or datetime.utcnow()
    query_now = generated_at.replace(tzinfo=None) if generated_at.tzinfo else generated_at
    lookback_start = query_now - timedelta(days=lookback_days)
    recent_start = query_now - timedelta(days=min(7, lookback_days))
    metrics: dict[int, dict[str, object]] = {}

    active_category_rows = (
        db.query(models.WebCatalogCategory.key, models.WebCatalogCategory.name)
        .filter(models.WebCatalogCategory.tenant_id == tenant_id)
        .filter(models.WebCatalogCategory.is_active.is_(True))
        .all()
    )
    active_categories = {
        _normalize_category_key(row.key): str(row.name)
        for row in active_category_rows
        if _normalize_category_key(row.key)
    }

    published_group_rows = (
        db.query(
            models.Product.group_name.label("group_name"),
            models.Product.web_category_key.label("category_key"),
            models.WebCatalogCategory.name.label("category_name"),
            func.count(models.Product.id).label("published_count"),
        )
        .join(
            models.WebCatalogCategory,
            and_(
                models.WebCatalogCategory.tenant_id == models.Product.tenant_id,
                models.WebCatalogCategory.key == models.Product.web_category_key,
            ),
        )
        .filter(models.Product.tenant_id == tenant_id)
        .filter(models.Product.active.is_(True))
        .filter(models.Product.service.is_(False))
        .filter(models.Product.web_published.is_(True))
        .filter(models.Product.group_name.isnot(None))
        .filter(func.trim(models.Product.group_name) != "")
        .filter(models.WebCatalogCategory.is_active.is_(True))
        .group_by(
            models.Product.group_name,
            models.Product.web_category_key,
            models.WebCatalogCategory.name,
        )
        .all()
    )
    eligible_group_categories: dict[str, tuple[str, str, int]] = {}
    for row in published_group_rows:
        normalized_group = _normalize_group_name(row.group_name)
        if not normalized_group:
            continue
        published_count = int(row.published_count or 0)
        current = eligible_group_categories.get(normalized_group)
        if current is None or published_count > current[2]:
            eligible_group_categories[normalized_group] = (
                str(row.category_key),
                str(row.category_name),
                published_count,
            )

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
            minimum_sale_price=effective_minimum_price,
            eligible_group_count=len(eligible_group_categories),
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
        normalized_group = _normalize_group_name(product.group_name)
        category_suggestion = eligible_group_categories.get(normalized_group)
        if category_suggestion is None:
            continue
        current_category_key = _normalize_category_key(product.web_category_key)
        if current_category_key in active_categories:
            category_suggestion = (
                current_category_key,
                active_categories[current_category_key],
                category_suggestion[2],
            )
        sale_price = crud.resolve_web_product_sale_price(product)
        if sale_price < effective_minimum_price:
            continue
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
        if current_category_key not in active_categories:
            missing_fields.append("categoría")
        if not product.web_short_description:
            missing_fields.append("descripción")
        readiness_score = 3 - len(missing_fields)
        score = (
            (min(units_7d, 10.0) * 2.0)
            + (min(units_lookback, 30.0) * 0.75)
            + min(revenue / 50_000.0, 50.0)
            + min(sale_price / 100_000.0, 10.0)
            + min(qty_on_hand, 20.0) * 0.2
            + (readiness_score * 2.0)
        )
        suggested_category_key, suggested_category_name, _ = category_suggestion
        reason = (
            f"Vendió {units_lookback:g} unidades en {lookback_days} días"
            f" ({units_7d:g} en los últimos 7), tiene {qty_on_hand:g} disponibles y "
            f"su grupo ya participa en {suggested_category_name}."
        )
        candidates.append(
            WebOpportunityItem(
                product_id=int(product.id),
                product_name=str(product.web_name or product.name),
                sku=product.sku,
                group_name=product.group_name,
                sale_price=round(sale_price, 2),
                suggested_category_key=suggested_category_key,
                suggested_category_name=suggested_category_name,
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
            minimum_sale_price=effective_minimum_price,
            eligible_group_count=len(eligible_group_categories),
            headline=(
                "Revisé las ventas, pero no encontré productos no publicados que cumplan todos "
                f"los criterios: grupos ya presentes en la web, precio mínimo de "
                f"{_format_cop(effective_minimum_price)} COP, stock disponible y rotación suficiente."
            ),
            items=(),
        )
    return WebOpportunityAnalysis(
        generated_at=generated_at,
        state="opportunities",
        lookback_days=lookback_days,
        analyzed_product_count=len(metrics),
        minimum_sale_price=effective_minimum_price,
        eligible_group_count=len(eligible_group_categories),
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
        f"{item.product_name} (${_format_cop(item.sale_price)}; {item.units_lookback:g} uds.)"
        for item in top_items
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
        dedupe_key=f"kora:web-opportunities:v2:{dedupe_suffix}",
        payload={
            "radar_version": 2,
            "generated_at": analysis.generated_at.isoformat(),
            "headline": analysis.headline,
            "lookback_days": analysis.lookback_days,
            "analyzed_product_count": analysis.analyzed_product_count,
            "minimum_sale_price": analysis.minimum_sale_price,
            "eligible_group_count": analysis.eligible_group_count,
            "product_ids": [item.product_id for item in analysis.items],
            "opportunities": [
                {
                    "product_id": item.product_id,
                    "product_name": item.product_name,
                    "sku": item.sku,
                    "group_name": item.group_name,
                    "sale_price": item.sale_price,
                    "suggested_category_key": item.suggested_category_key,
                    "suggested_category_name": item.suggested_category_name,
                    "qty_on_hand": item.qty_on_hand,
                    "units_7d": item.units_7d,
                    "units_lookback": item.units_lookback,
                    "revenue_lookback": item.revenue_lookback,
                    "missing_web_fields": list(item.missing_web_fields),
                    "reason": item.reason,
                }
                for item in analysis.items
            ],
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
