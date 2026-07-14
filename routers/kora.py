from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from math import ceil
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import case, func, or_, true

import crud
import models
import schemas
from database import get_db
from dependencies import (
    get_current_active_user,
    get_current_tenant_id,
    require_permission,
)

class KoraAskContext(BaseModel):
    topic: str | None = None
    path: str | None = None


class KoraAskRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    context: KoraAskContext | None = None


class KoraActionOut(BaseModel):
    label: str
    href: str | None = None


def _dedupe_actions(*actions: KoraActionOut) -> list[KoraActionOut]:
    seen: set[tuple[str, str | None]] = set()
    result: list[KoraActionOut] = []
    for action in actions:
        key = (action.label, action.href)
        if key in seen:
            continue
        seen.add(key)
        result.append(action)
    return result[:4]


class KoraAskResponse(BaseModel):
    handled: bool
    answer: str
    source: Literal["rules-v2", "openai-v2"]
    confidence: float = Field(ge=0, le=1)
    actions: list[KoraActionOut] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    generated_at: datetime


class KoraRestockForecastItem(BaseModel):
    product_id: int
    product_name: str
    sku: str | None = None
    group_name: str | None = None
    price: float = 0
    units_today: float = 0
    qty_on_hand: float
    stock_min: int
    preferred_qty: int
    reorder_point: int
    effective_threshold: int
    threshold_source: Literal["configured", "inferred", "mixed"]
    units_7d: float
    units_lookback: float
    daily_rate: float
    coverage_days: float | None = None
    projected_demand: float
    suggested_qty: int
    urgency: Literal["high", "medium", "low"]
    reason: str
    last_sale_at: datetime | None = None
    last_movement_at: datetime | None = None


class KoraRestockForecastResponse(BaseModel):
    generated_at: datetime
    source: Literal["restock-forecast-v1"]
    mode: Literal["general", "today"]
    state: Literal["alert", "watch", "calm"]
    horizon_days: int
    lookback_days: int
    headline: str
    summary_lines: list[str]
    items: list[KoraRestockForecastItem]
    recommended_actions: list[KoraActionOut]
    conversation_starters: list[str]


router = APIRouter(
    prefix="/kora",
    tags=["kora"],
    dependencies=[Depends(require_permission("dashboard.view"))],
)


ALLOWED_HREFS = {
    "/dashboard",
    "/dashboard/reports",
    "/dashboard/reports/detailed",
    "/dashboard/sales",
    "/dashboard/products",
    "/dashboard/movements",
    "/dashboard/comercio-web",
    "/dashboard/settings",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no"}


def _normalize(value: str) -> str:
    return " ".join(
        (value or "")
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .split()
    )


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return 0.0
    if parsed > 1:
        return 1.0
    return parsed


def _sanitize_actions(raw: object) -> list[KoraActionOut]:
    if not isinstance(raw, list):
        return []
    actions: list[KoraActionOut] = []
    for item in raw[:4]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        href = str(item.get("href") or "").strip() or None
        if not label:
            continue
        if href and not any(href == allowed or href.startswith(f"{allowed}?") for allowed in ALLOWED_HREFS):
            href = None
        actions.append(KoraActionOut(label=label[:80], href=href))
    return actions


def _sanitize_suggestions(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    suggestions: list[str] = []
    for item in raw[:4]:
        text = str(item or "").strip()
        if not text:
            continue
        suggestions.append(text[:180])
    return suggestions


_REPLENISHMENT_REASONS = {"purchase", "transfer_in", "stock_initial"}
_CONSUMPTION_REASONS = {"sale", "transfer_out", "loss", "damage"}


def _normalized_reason(value: object) -> str:
    return str(value or "").strip().lower()


def _is_replenishment_movement(movement: models.InventoryMovement) -> bool:
    qty_delta = float(movement.qty_delta or 0.0)
    if qty_delta <= 0:
        return False
    reason = _normalized_reason(movement.reason)
    reference_type = _normalized_reason(movement.reference_type)
    return reason in _REPLENISHMENT_REASONS or reference_type in _REPLENISHMENT_REASONS


def _is_consumption_movement(movement: models.InventoryMovement) -> bool:
    qty_delta = float(movement.qty_delta or 0.0)
    if qty_delta >= 0:
        return False
    reason = _normalized_reason(movement.reason)
    reference_type = _normalized_reason(movement.reference_type)
    return reason in _CONSUMPTION_REASONS or reference_type in _CONSUMPTION_REASONS


def _format_days(value: float | None) -> str:
    if value is None:
        return "sin cobertura"
    if value < 1:
        return "< 1 día"
    return f"{round(value)} días"


def _reverse_stock_path(
    current_qty: float, movements: list[models.InventoryMovement]
) -> list[tuple[models.InventoryMovement, float, float]]:
    balance = float(current_qty)
    reverse_path: list[tuple[models.InventoryMovement, float, float]] = []
    for movement in reversed(movements):
        before_qty = balance - float(movement.qty_delta or 0.0)
        after_qty = balance
        reverse_path.append((movement, max(before_qty, 0.0), max(after_qty, 0.0)))
        balance = before_qty
    return list(reversed(reverse_path))


def _looks_like_sparse_rotation(
    *,
    units_lookback: float,
    units_7d: float,
    restock_before_levels: list[float],
    low_touch_levels: list[float],
) -> bool:
    if restock_before_levels or low_touch_levels:
        return False
    return units_lookback < 3 and units_7d < 1


def _build_restock_forecast_response(
    *,
    db: Session,
    tenant_id: int | None,
    mode: Literal["general", "today"],
    horizon_days: int,
    lookback_days: int,
) -> KoraRestockForecastResponse:
    now_bogota = datetime.now(ZoneInfo("America/Bogota"))
    day_start = now_bogota.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon_days = max(1, min(int(horizon_days or 1), 14))
    lookback_days = max(7, min(int(lookback_days or 30), 90))
    short_window_days = min(7, lookback_days)
    movement_history_days = min(max(lookback_days * 2, 45), 120)
    max_items = 18

    stock_rows = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
            func.max(models.InventoryMovement.created_at).label("last_movement_at"),
        )
        .filter(models.InventoryMovement.tenant_id == tenant_id if tenant_id is not None else true())
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )

    sale_window_start = now_bogota - timedelta(days=lookback_days)
    short_window_start = now_bogota - timedelta(days=short_window_days)
    sales_rows = (
        db.query(
            models.SaleItem.product_id.label("product_id"),
            func.coalesce(
                func.sum(
                    case((models.Sale.created_at >= sale_window_start, models.SaleItem.quantity), else_=0)
                ),
                0,
            ).label("units_lookback"),
            func.coalesce(
                func.sum(
                    case((models.Sale.created_at >= short_window_start, models.SaleItem.quantity), else_=0)
                ),
                0,
            ).label("units_7d"),
            func.max(models.Sale.created_at).label("last_sale_at"),
        )
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .filter(models.Sale.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.created_at >= sale_window_start)
        .group_by(models.SaleItem.product_id)
        .subquery()
    )

    today_sales_rows = (
        db.query(
            models.SaleItem.product_id.label("product_id"),
            func.coalesce(func.sum(models.SaleItem.quantity), 0).label("units_today"),
            func.max(models.Sale.created_at).label("last_sale_today_at"),
        )
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .filter(models.Sale.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(or_(models.Sale.status.is_(None), models.Sale.status != "voided"))
        .filter(models.Sale.created_at >= day_start)
        .group_by(models.SaleItem.product_id)
        .subquery()
    )

    movement_window_start = now_bogota - timedelta(days=movement_history_days)
    movement_rows = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            models.InventoryMovement.qty_delta.label("qty_delta"),
            models.InventoryMovement.reason.label("reason"),
            models.InventoryMovement.reference_type.label("reference_type"),
            models.InventoryMovement.created_at.label("created_at"),
        )
        .filter(models.InventoryMovement.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(models.InventoryMovement.created_at >= movement_window_start)
        .order_by(
            models.InventoryMovement.product_id.asc(),
            models.InventoryMovement.created_at.asc(),
            models.InventoryMovement.id.asc(),
        )
        .all()
    )
    movements_by_product: dict[int, list[models.InventoryMovement]] = {}
    for row in movement_rows:
        product_id = int(row.product_id or 0)
        movements_by_product.setdefault(product_id, []).append(
            models.InventoryMovement(
                product_id=product_id,
                qty_delta=float(row.qty_delta or 0.0),
                reason=str(row.reason or ""),
                reference_type=str(row.reference_type or ""),
                created_at=row.created_at,
            )
        )

    product_rows = (
        db.query(
            models.Product.id,
            models.Product.name,
            models.Product.sku,
            models.Product.group_name,
            models.Product.price,
            models.Product.stock_min,
            models.Product.preferred_qty,
            models.Product.reorder_point,
            models.Product.low_stock_alert,
            func.coalesce(stock_rows.c.qty_on_hand, 0).label("qty_on_hand"),
            stock_rows.c.last_movement_at.label("last_movement_at"),
            func.coalesce(sales_rows.c.units_lookback, 0).label("units_lookback"),
            func.coalesce(sales_rows.c.units_7d, 0).label("units_7d"),
            sales_rows.c.last_sale_at.label("last_sale_at"),
            func.coalesce(today_sales_rows.c.units_today, 0).label("units_today"),
            today_sales_rows.c.last_sale_today_at.label("last_sale_today_at"),
        )
        .outerjoin(stock_rows, stock_rows.c.product_id == models.Product.id)
        .outerjoin(sales_rows, sales_rows.c.product_id == models.Product.id)
        .outerjoin(today_sales_rows, today_sales_rows.c.product_id == models.Product.id)
        .filter(models.Product.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
        .all()
    )

    scored_items: list[tuple[float, KoraRestockForecastItem]] = []
    for row in product_rows:
        qty = float(row.qty_on_hand or 0.0)
        units_lookback = max(0.0, float(row.units_lookback or 0.0))
        units_7d = max(0.0, float(row.units_7d or 0.0))
        units_today = max(0.0, float(row.units_today or 0.0))
        rate_lookback = units_lookback / lookback_days if lookback_days > 0 else 0.0
        rate_7d = units_7d / short_window_days if short_window_days > 0 else 0.0
        if units_lookback > 0 and units_7d > 0:
            daily_rate = (rate_lookback * 0.35) + (rate_7d * 0.65)
        elif units_lookback > 0:
            daily_rate = rate_lookback
        else:
            daily_rate = rate_7d

        projected_demand = daily_rate * horizon_days
        configured_threshold = max(
            int(row.stock_min or 0) if bool(row.low_stock_alert) else 0,
            int(row.reorder_point or 0),
            int(row.preferred_qty or 0),
        )

        movement_history = movements_by_product.get(int(row.id), [])
        restock_before_levels: list[float] = []
        low_touch_levels: list[float] = []
        stock_path = _reverse_stock_path(qty, movement_history)
        low_touch_cutoff = max(2.0, ceil(max(daily_rate * 1.25, 1.0)))
        for movement, before_qty, after_qty in stock_path:
            if _is_replenishment_movement(movement):
                restock_before_levels.append(before_qty)
            elif _is_consumption_movement(movement) and before_qty <= low_touch_cutoff:
                low_touch_levels.append(before_qty)

        historical_threshold = 0
        if restock_before_levels:
            historical_threshold = max(int(ceil(median(restock_before_levels))), 1)
        elif low_touch_levels:
            historical_threshold = max(int(ceil(median(low_touch_levels))), 1)

        velocity_threshold = 0
        if daily_rate > 0:
            velocity_threshold = max(2, int(ceil(daily_rate * 1.5)))
            if units_lookback >= 8:
                velocity_threshold = max(velocity_threshold, 2)
            if units_lookback >= 15:
                velocity_threshold = max(velocity_threshold, 3)

        inferred_threshold = max(velocity_threshold, historical_threshold)
        if qty <= 1 and daily_rate > 0:
            inferred_threshold = max(inferred_threshold, 2)
        if len(restock_before_levels) >= 3:
            sorted_restock_levels = sorted(restock_before_levels)
            inferred_threshold = max(
                inferred_threshold,
                int(ceil(sorted_restock_levels[len(sorted_restock_levels) // 3])),
            )

        effective_threshold = max(configured_threshold, inferred_threshold)
        if configured_threshold > 0 and inferred_threshold > 0:
            threshold_source: Literal["configured", "inferred", "mixed"] = "mixed"
        elif configured_threshold > 0:
            threshold_source = "configured"
        else:
            threshold_source = "inferred"

        rotation_volume = units_lookback + (units_7d * 1.5)
        sparse_rotation = _looks_like_sparse_rotation(
            units_lookback=units_lookback,
            units_7d=units_7d,
            restock_before_levels=restock_before_levels,
            low_touch_levels=low_touch_levels,
        )
        strong_rotation = rotation_volume >= 6 or units_lookback >= 6 or units_7d >= 2
        has_learned_signal = bool(restock_before_levels or low_touch_levels)
        coverage_days = qty / daily_rate if daily_rate > 0 else None
        today_signal_strength = max(units_today, projected_demand)
        today_conservative_floor = max(2.0, projected_demand + 1.0)
        today_has_minimum_signal = (
            units_today >= 2
            or (coverage_days is not None and coverage_days <= horizon_days)
            or qty <= float(effective_threshold)
            or today_signal_strength >= 3.0
            or strong_rotation
        )
        today_is_still_healthy = (
            mode == "today"
            and coverage_days is not None
            and coverage_days > horizon_days * 2
            and today_signal_strength < 3.0
            and qty > today_conservative_floor
            and not strong_rotation
        )
        today_is_overstocked = (
            mode == "today"
            and coverage_days is not None
            and coverage_days > max(horizon_days * 4, 30)
            and qty > max(10.0, units_today * 8.0)
        )
        if mode == "today" and (not today_has_minimum_signal or today_is_still_healthy or today_is_overstocked):
            continue
        if mode == "today" and units_today <= 0:
            continue
        daily_restock_limit = max(
            2.0,
            float(configured_threshold),
            float(inferred_threshold),
            float(effective_threshold),
            units_today * 1.5,
        )
        if mode == "today" and qty > daily_restock_limit and (coverage_days is None or coverage_days > horizon_days):
            continue
        if sparse_rotation and qty > max(2.0, float(effective_threshold), float(inferred_threshold), float(configured_threshold)):
            continue
        if (
            mode == "general"
            and not has_learned_signal
            and not strong_rotation
            and qty > max(3.0, float(effective_threshold))
            and projected_demand <= qty * 0.5
        ):
            continue

        buffer_target = max(configured_threshold, effective_threshold)
        target_qty = max(projected_demand + buffer_target, float(buffer_target))
        suggested_qty = max(int(ceil(target_qty - qty)), 0)

        urgency = "low"
        reason_parts: list[str] = []
        today_historical_bonus = 0.0
        if qty <= 0 and (units_lookback > 0 or units_today > 0 or buffer_target > 0):
            urgency = "high"
            reason_parts.append("hoy está sin stock")
        elif effective_threshold > 0 and qty <= float(effective_threshold):
            today_threshold_is_too_soft = (
                mode == "today"
                and coverage_days is not None
                and coverage_days > horizon_days * 1.5
                and units_today < 2
                and not strong_rotation
            )
            if today_threshold_is_too_soft:
                urgency = "low"
                reason_parts.append(
                    f"tocó el umbral historico, pero aun tiene cobertura de {_format_days(coverage_days)}"
                )
            else:
                if mode == "today" and coverage_days is not None and coverage_days > horizon_days and qty > 1:
                    urgency = "medium"
                elif qty <= 1 or effective_threshold <= 2 or units_lookback >= 8:
                    urgency = "high"
                else:
                    urgency = "medium"
                if threshold_source == "inferred":
                    reason_parts.append(
                        f"ya tocó el punto de aviso aprendido de los movimientos ({effective_threshold} unidades)"
                    )
                    today_historical_bonus = 3.0 if mode == "today" else 0.0
                elif threshold_source == "mixed":
                    reason_parts.append(
                        f"ya tocó el punto de aviso mezclando configuración y movimientos ({effective_threshold} unidades)"
                    )
                    today_historical_bonus = 5.0 if mode == "today" else 0.0
                else:
                    reason_parts.append(f"ya tocó el umbral de reposición configurado ({effective_threshold} unidades)")
                    today_historical_bonus = 8.0 if mode == "today" else 0.0
        elif coverage_days is not None and coverage_days <= horizon_days:
            urgency = "high"
            reason_parts.append(f"solo alcanza para {_format_days(coverage_days)}")
        elif coverage_days is not None and coverage_days <= horizon_days * 2:
            urgency = "medium"
            reason_parts.append(f"tiene cobertura aproximada de {_format_days(coverage_days)}")
        elif units_lookback > 0:
            urgency = "low"
            reason_parts.append("tiene rotación reciente")

        if projected_demand > qty:
            reason_parts.append(f"para {horizon_days} días faltan ~{max(int(ceil(projected_demand - qty)), 0)} unidades")
        elif buffer_target > qty:
            reason_parts.append(f"está por debajo del nivel objetivo de {buffer_target} unidades")
        elif suggested_qty > 0:
            reason_parts.append(f"conviene subir el stock en {suggested_qty} unidades")

        if units_7d > units_lookback / max(lookback_days / short_window_days, 1):
            reason_parts.append("la rotación viene acelerando")
        elif units_lookback > 0 and units_7d < units_lookback / max(lookback_days / short_window_days, 1) * 0.7:
            reason_parts.append("la rotación viene más suave en la última semana")

        if restock_before_levels:
            reason_parts.append(f"aprendido de {len(restock_before_levels)} reposiciones recientes")
            if median(restock_before_levels) <= 2:
                reason_parts.append("normalmente se repone casi al final del stock")
        elif low_touch_levels:
            reason_parts.append(f"aprendido de {len(low_touch_levels)} momentos de stock apretado")

        if not reason_parts:
            if qty > 0:
                reason_parts.append("no parece urgente, pero conviene vigilarlo")
            else:
                reason_parts.append("sin venta reciente clara para priorizarlo")

        score = 0.0
        score += {"high": 300, "medium": 180, "low": 90}[urgency]
        score += max(projected_demand - qty, 0) * 35
        score += max(buffer_target - qty, 0) * 18
        score += min(units_lookback, 150.0) * 1.5
        score += min(units_7d, 70.0) * 2.5
        if coverage_days is not None:
            score += max(0.0, 20.0 - min(coverage_days, 20.0))
        score += today_historical_bonus

        if units_lookback <= 0 and qty > buffer_target:
            score *= 0.25

        if mode == "today":
            score += units_today * 55
            score += max(daily_restock_limit - qty, 0) * 30
            score += min(units_today, 20.0) * 5
            if units_today >= 3:
                urgency = "high" if qty <= effective_threshold + 2 else urgency
                reason_parts.append(f"hoy se vendieron {units_today:.0f} unidades")
            elif units_today == 1:
                reason_parts.append("hoy tuvo una salida puntual")

        if urgency == "low" and score < 40:
            continue

        scored_items.append(
            (
                score,
                KoraRestockForecastItem(
                    product_id=int(row.id),
                    product_name=str(row.name or "Producto"),
                    sku=str(row.sku or "").strip() or None,
                    group_name=str(row.group_name or "").strip() or None,
                    price=float(row.price or 0.0),
                    units_today=units_today,
                    qty_on_hand=qty,
                    stock_min=int(row.stock_min or 0),
                    preferred_qty=int(row.preferred_qty or 0),
                    reorder_point=int(row.reorder_point or 0),
                    effective_threshold=effective_threshold,
                    threshold_source=threshold_source,
                    units_7d=units_7d,
                    units_lookback=units_lookback,
                    daily_rate=daily_rate,
                    coverage_days=coverage_days,
                    projected_demand=projected_demand,
                    suggested_qty=suggested_qty,
                    urgency=urgency,  # type: ignore[arg-type]
                    reason="; ".join(reason_parts[:3]),
                    last_sale_at=row.last_sale_at,
                    last_movement_at=row.last_movement_at,
                ),
            )
        )

    if mode == "today":
        scored_items = [item for item in scored_items if item[1].urgency == "high"]

    scored_items.sort(key=lambda item: item[0], reverse=True)
    items = [item for _, item in scored_items[:max_items]]

    high_count = sum(1 for item in items if item.urgency == "high")
    medium_count = sum(1 for item in items if item.urgency == "medium")
    low_count = sum(1 for item in items if item.urgency == "low")
    state: Literal["alert", "watch", "calm"]
    if mode == "today":
        state = "alert" if high_count > 0 else "calm"
    elif high_count > 0:
        state = "alert"
    elif medium_count > 0 or low_count > 0:
        state = "watch"
    else:
        state = "calm"

    if mode == "today":
        headline = (
            "Estos son los productos vendidos hoy que ya conviene reponer mañana."
            if items
            else "No veo productos vendidos hoy que ya ameriten reposición mañana."
        )
    else:
        headline = (
            "Estos son los productos con más presión de reposición general."
            if items
            else "No veo señales fuertes de reposición general."
        )

    summary_lines = [
        (
            "Revisé las ventas de hoy y dejé solo los productos que de verdad ameritan reposición para mañana."
            if mode == "today"
            else f"Revisé las ventas de los últimos {lookback_days} días y calculé la cobertura estimada por producto."
        ),
        (
            f"Encontré {high_count} productos urgentes."
            if mode == "today"
            else f"Prioridad: {high_count} críticas, {medium_count} en vigilancia y {low_count} bajas."
        ),
        "Cruzo rotación reciente, stock actual y el patrón de reposición aprendido de movimientos.",
        (
            "Dejé fuera productos que todavía no muestran presión suficiente para reposición inmediata."
            if mode == "today"
            else "Dejé fuera productos con rotación demasiado baja para no llenar la lista con ruido."
        ),
    ]
    if mode == "today":
        summary_lines.append(
            "En este reporte solo muestro urgencias reales: si aún hay cobertura suficiente, no aparece."
        )

    recommended_actions = _dedupe_actions(
        KoraActionOut(label="Abrir Productos", href="/dashboard/products"),
        KoraActionOut(label="Abrir Movimientos", href="/dashboard/movements"),
        KoraActionOut(label="Abrir Reportes", href="/dashboard/reports"),
    )

    conversation_starters = [
        "¿Cuál de estos debería reponer primero?",
        "Muéstrame el detalle completo",
        "¿Qué cambiaría si revisamos 7 días en vez de 30?",
    ]

    return KoraRestockForecastResponse(
        generated_at=datetime.utcnow(),
        source="restock-forecast-v1",
        mode=mode,
        state=state,
        horizon_days=horizon_days,
        lookback_days=lookback_days,
        headline=headline,
        summary_lines=summary_lines,
        items=items,
        recommended_actions=recommended_actions,
        conversation_starters=conversation_starters,
    )


@router.get("/restock-forecast", response_model=KoraRestockForecastResponse)
def get_kora_restock_forecast(
    mode: Literal["general", "today"] = Query(default="general"),
    horizon_days: int = Query(default=2, ge=1, le=14),
    lookback_days: int = Query(default=30, ge=7, le=90),
    db: Session = Depends(get_db),
    tenant_id: int = Depends(get_current_tenant_id),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    return _build_restock_forecast_response(
        db=db,
        tenant_id=tenant_id,
        mode=mode,
        horizon_days=horizon_days,
        lookback_days=lookback_days,
    )


def _rules_response(query: str, normalized: str) -> KoraAskResponse:
    if len(query) < 3:
        return KoraAskResponse(
            handled=False,
            answer="Necesito un poco más de detalle para ayudarte mejor.",
            source="rules-v2",
            confidence=0.22,
            suggestions=[
                "¿Cómo crear un producto?",
                "¿Cuánto vendimos hoy?",
                "¿Cuál fue la última vez que vendimos cable?",
            ],
            generated_at=datetime.utcnow(),
        )

    if "devolucion" in normalized:
        return KoraAskResponse(
            handled=True,
            answer=(
                "Te puedo guiar con devoluciones en el historial de ventas: "
                "abre la venta, valida ítems y usa la opción de devolución confirmada."
            ),
            source="rules-v2",
            confidence=0.72,
            actions=[
                KoraActionOut(
                    label="Abrir historial de ventas",
                    href="/dashboard/sales",
                )
            ],
            suggestions=[
                "¿Qué ventas hubo hoy?",
                "¿Cuáles métodos de pago se usaron el 21 de febrero?",
            ],
            generated_at=datetime.utcnow(),
        )

    if "reporte" in normalized or "informe" in normalized:
        return KoraAskResponse(
            handled=True,
            answer=(
                "Puedo ayudarte con reportes rápidos o detallados. "
                "Si me dices periodo y métrica (ventas, método de pago, producto), te lo estructuro."
            ),
            source="rules-v2",
            confidence=0.66,
            actions=[
                KoraActionOut(label="Abrir Reportes", href="/dashboard/reports"),
                KoraActionOut(
                    label="Abrir Reporte detallado",
                    href="/dashboard/reports/detailed",
                ),
            ],
            suggestions=[
                "¿Cuánto más vendimos que el mes anterior hasta hoy?",
                "¿Cuál es el producto más vendido de este mes?",
            ],
            generated_at=datetime.utcnow(),
        )

    if (
        "producto" in normalized
        or "sku" in normalized
        or "codigo" in normalized
    ):
        return KoraAskResponse(
            handled=True,
            answer=(
                "Para consultas de producto, indícame código/SKU o el nombre. "
                "Ejemplo: 'producto código ABC123' o 'a qué grupo pertenece SKU 100045'."
            ),
            source="rules-v2",
            confidence=0.64,
            actions=[KoraActionOut(label="Abrir Productos", href="/dashboard/products")],
            suggestions=[
                "Producto código ABC123",
                "¿A qué grupo pertenece SKU 100045?",
            ],
            generated_at=datetime.utcnow(),
        )

    return KoraAskResponse(
        handled=False,
        answer=(
            "No encontré una respuesta precisa todavía. "
            "Si reformulas con periodo, métrica y entidad, te respondo mejor."
        ),
        source="rules-v2",
        confidence=0.31,
        suggestions=[
            "¿Cuánto más vendimos que el mes anterior hasta ahora?",
            "¿Qué métodos de pago se usaron el 21 de febrero?",
            "¿Cuál fue la última vez que vendimos cable?",
        ],
        generated_at=datetime.utcnow(),
    )


def _ask_openai(query: str, context: KoraAskContext | None, user: models.PosUser) -> KoraAskResponse | None:
    api_key = (os.getenv("KORA_OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    if not _env_bool("KORA_AI_ENABLED", True):
        return None

    model = (os.getenv("KORA_OPENAI_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = int(os.getenv("KORA_OPENAI_TIMEOUT_SECONDS", "12"))
    endpoint = os.getenv("KORA_OPENAI_ENDPOINT", "https://api.openai.com/v1/chat/completions").strip()

    system_prompt = (
        "Eres KORA, asistente operativo de Metrik POS.\n"
        "Responde SIEMPRE en JSON válido sin texto adicional, con esta forma:\n"
        "{"
        "\"handled\": boolean, "
        "\"answer\": string, "
        "\"confidence\": number, "
        "\"actions\": [{\"label\": string, \"href\": string | null}], "
        "\"suggestions\": [string]"
        "}\n"
        "Reglas:\n"
        "- Español claro, breve y profesional.\n"
        "- No inventes datos numéricos.\n"
        "- Si falta contexto, handled=false y sugiere reformulaciones.\n"
        "- Solo usa href dentro de rutas dashboard internas.\n"
        "- Máximo 4 actions y 4 suggestions."
    )
    user_prompt = (
        f"Usuario: {user.name or 'Operador'}\n"
        f"Rol: {user.role}\n"
        f"Contexto: topic={context.topic if context else ''}, path={context.path if context else ''}\n"
        f"Consulta: {query}\n"
        "Devuelve solo JSON."
    )

    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None

    handled = bool(parsed.get("handled"))
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        return None

    return KoraAskResponse(
        handled=handled,
        answer=answer[:1200],
        source="openai-v2",
        confidence=_safe_float(parsed.get("confidence"), 0.4),
        actions=_sanitize_actions(parsed.get("actions")),
        suggestions=_sanitize_suggestions(parsed.get("suggestions")),
        generated_at=datetime.utcnow(),
    )


@router.post("/ask", response_model=KoraAskResponse)
def ask_kora(
    payload: KoraAskRequest,
    _tenant_id: int = Depends(get_current_tenant_id),
    user: models.PosUser = Depends(get_current_active_user),
):
    query = payload.query.strip()
    normalized = _normalize(query)
    rules = _rules_response(query, normalized)
    ai_min_conf = _safe_float(os.getenv("KORA_AI_MIN_CONFIDENCE", "0.58"), 0.58)

    ai = _ask_openai(query, payload.context, user)
    if not ai:
        return rules

    if ai.handled and ai.confidence >= ai_min_conf:
        return ai

    if rules.handled:
        return rules

    if ai.handled and ai.confidence < ai_min_conf:
        return KoraAskResponse(
            handled=False,
            answer="Puedo ayudarte, pero necesito una reformulación un poco más específica para darte una respuesta confiable.",
            source="openai-v2",
            confidence=ai.confidence,
            actions=ai.actions,
            suggestions=ai.suggestions
            or rules.suggestions,
            generated_at=datetime.utcnow(),
        )

    return ai
