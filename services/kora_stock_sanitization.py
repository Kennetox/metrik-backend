"""Deterministic stock-sanitization plans prepared by operational Kora.

This module owns selection, operational context and persistence.  It does not
create or apply inventory recounts: a future Metrik Stock hand-off will convert
one ready plan into a device-bound recount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

import models


BOGOTA_TZ = ZoneInfo("America/Bogota")
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_REQUESTED_COUNT = 15
MIN_PLAN_ITEMS = 5
MAX_PLAN_ITEMS = 30
PLAN_TTL_HOURS = 48
RECENT_PLAN_COOLDOWN_DAYS = 7


@dataclass(frozen=True)
class OperationalContext:
    generated_at: datetime
    local_date: date
    scheduled_people: int | None
    scheduled_names: tuple[str, ...]
    schedule_status: Literal["published", "draft"] | None
    reserved_for_sales: int
    reserved_for_receiving: int
    available_people: int | None
    open_receiving_count: int
    open_receiving_codes: tuple[str, ...]
    sales_count_30m: int
    sales_total_30m: float
    workload_state: Literal["quiet", "normal", "busy", "unknown"]
    automatic_plan_allowed: bool
    automatic_reason: str


@dataclass(frozen=True)
class PlanCandidate:
    product: models.Product
    qty_on_hand: float
    units_sold: float
    last_sale_at: datetime | None
    last_movement_at: datetime | None
    last_recount_at: datetime | None
    cost_impact: float
    sale_impact: float
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlanBuildResult:
    state: Literal["ready", "existing", "not_eligible", "no_candidates"]
    plan: models.KoraStockPlan | None
    context: OperationalContext
    message: str


def _utc_naive(reference_time: datetime | None = None) -> datetime:
    value = reference_time or datetime.utcnow()
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _local_now(reference_time: datetime | None = None) -> datetime:
    if reference_time is None:
        return datetime.now(BOGOTA_TZ)
    if reference_time.tzinfo is None:
        return reference_time.replace(tzinfo=ZoneInfo("UTC")).astimezone(BOGOTA_TZ)
    return reference_time.astimezone(BOGOTA_TZ)


def _parse_shift_time(value: str | None) -> time | None:
    clean = (value or "").strip().lower().replace(" ", "")
    if not clean:
        return None
    for pattern in ("%H:%M", "%H:%M:%S", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(clean, pattern).time()
        except ValueError:
            continue
    return None


def _shift_is_active(start_value: str | None, end_value: str | None, current: time) -> bool:
    start = _parse_shift_time(start_value)
    end = _parse_shift_time(end_value)
    if start is None or end is None:
        return False
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def read_operational_context(
    db: Session,
    *,
    tenant_id: int,
    reference_time: datetime | None = None,
) -> OperationalContext:
    now = _utc_naive(reference_time)
    local_now = _local_now(reference_time)
    week_start = local_now.date() - timedelta(days=local_now.weekday())
    schedule_week = (
        db.query(models.ScheduleWeek)
        .filter(
            models.ScheduleWeek.tenant_id == tenant_id,
            models.ScheduleWeek.week_start == week_start,
            models.ScheduleWeek.status.in_(["published", "draft"]),
        )
        .first()
    )
    shifts = [] if schedule_week is None else (
        db.query(models.ScheduleShift)
        .join(models.HREmployee, models.HREmployee.id == models.ScheduleShift.employee_id)
        .filter(models.ScheduleShift.tenant_id == tenant_id)
        .filter(models.ScheduleShift.week_id == schedule_week.id)
        .filter(models.ScheduleShift.shift_date == local_now.date())
        .filter(models.ScheduleShift.is_time_off.is_(False))
        .filter(models.HREmployee.status == "Activo")
        .all()
    )
    active_shifts = [
        row
        for row in shifts
        if _shift_is_active(row.start_time, row.end_time, local_now.time())
    ]
    scheduled_names = tuple(
        sorted(
            {
                str(row.employee.name)
                for row in active_shifts
                if row.employee and row.employee.name
            }
        )
    )
    scheduled_people = len(scheduled_names) if schedule_week is not None else None
    schedule_status = schedule_week.status if schedule_week is not None else None

    open_lots = (
        db.query(models.ReceivingLot)
        .filter(
            models.ReceivingLot.tenant_id == tenant_id,
            models.ReceivingLot.status == "open",
        )
        .order_by(models.ReceivingLot.created_at.asc())
        .all()
    )
    reserved_for_receiving = 2 if open_lots else 0
    reserved_for_sales = 1
    available_people = (
        max(0, scheduled_people - reserved_for_sales - reserved_for_receiving)
        if scheduled_people is not None
        else None
    )

    sales_row = (
        db.query(
            func.count(models.Sale.id).label("sale_count"),
            func.coalesce(func.sum(models.Sale.total), 0).label("sale_total"),
        )
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.created_at >= now - timedelta(minutes=30))
        .filter(or_(models.Sale.status.is_(None), ~models.Sale.status.in_(["voided", "cancelled"])))
        .one()
    )
    sales_count = int(sales_row.sale_count or 0)
    sales_total = float(sales_row.sale_total or 0)
    workload_state: Literal["quiet", "normal", "busy", "unknown"]
    if scheduled_people is None:
        workload_state = "unknown"
    elif sales_count <= 1:
        workload_state = "quiet"
    elif sales_count <= 3:
        workload_state = "normal"
    else:
        workload_state = "busy"

    if scheduled_people is None:
        automatic_allowed = False
        automatic_reason = "No hay un horario configurado que permita estimar la capacidad del turno."
    elif available_people <= 0:
        automatic_allowed = False
        automatic_reason = "El turno no tiene capacidad libre después de reservar ventas y recepción."
    elif workload_state != "quiet":
        automatic_allowed = False
        automatic_reason = "La actividad comercial reciente no está en nivel tranquilo."
    else:
        automatic_allowed = True
        automatic_reason = "El turno tiene capacidad estimada y las ventas recientes están tranquilas."

    return OperationalContext(
        generated_at=now,
        local_date=local_now.date(),
        scheduled_people=scheduled_people,
        scheduled_names=scheduled_names,
        schedule_status=schedule_status,
        reserved_for_sales=reserved_for_sales,
        reserved_for_receiving=reserved_for_receiving,
        available_people=available_people,
        open_receiving_count=len(open_lots),
        open_receiving_codes=tuple(
            str(row.lot_number or f"Recepción #{row.id}") for row in open_lots
        ),
        sales_count_30m=sales_count,
        sales_total_30m=round(sales_total, 2),
        workload_state=workload_state,
        automatic_plan_allowed=automatic_allowed,
        automatic_reason=automatic_reason,
    )


def _refresh_plan_status(db: Session, plan: models.KoraStockPlan, now: datetime) -> None:
    if plan.status == "ready" and plan.expires_at and plan.expires_at <= now:
        plan.status = "expired"
    if plan.status == "converted" and plan.converted_recount is not None:
        recount = plan.converted_recount
        if recount.status == "applied":
            plan.status = "completed"
            plan.completed_at = recount.applied_at or now
        elif recount.status == "cancelled":
            plan.status = "cancelled"
            plan.cancelled_at = recount.cancelled_at or now


def get_current_plan(
    db: Session,
    *,
    tenant_id: int,
    reference_time: datetime | None = None,
) -> models.KoraStockPlan | None:
    now = _utc_naive(reference_time)
    plans = (
        db.query(models.KoraStockPlan)
        .options(
            selectinload(models.KoraStockPlan.items),
            selectinload(models.KoraStockPlan.converted_recount),
        )
        .filter(models.KoraStockPlan.tenant_id == tenant_id)
        .filter(models.KoraStockPlan.status.in_(["ready", "converted"]))
        .order_by(models.KoraStockPlan.created_at.desc(), models.KoraStockPlan.id.desc())
        .all()
    )
    changed = False
    for plan in plans:
        before = plan.status
        _refresh_plan_status(db, plan, now)
        changed = changed or before != plan.status
        if plan.status in {"ready", "converted"}:
            if changed:
                db.commit()
            return plan
    if changed:
        db.commit()
    return None


def _recently_planned_product_ids(
    db: Session,
    *,
    tenant_id: int,
    now: datetime,
) -> set[int]:
    rows = (
        db.query(models.KoraStockPlanItem.product_id)
        .join(models.KoraStockPlan, models.KoraStockPlan.id == models.KoraStockPlanItem.plan_id)
        .filter(models.KoraStockPlanItem.tenant_id == tenant_id)
        .filter(
            or_(
                models.KoraStockPlan.status.in_(["ready", "converted"]),
                models.KoraStockPlan.created_at >= now - timedelta(days=RECENT_PLAN_COOLDOWN_DAYS),
            )
        )
        .all()
    )
    return {int(row.product_id) for row in rows}


def _candidate_rows(
    db: Session,
    *,
    tenant_id: int,
    lookback_days: int,
    group_name: str | None,
    now: datetime,
) -> tuple[list[PlanCandidate], int]:
    stock_query = (
        db.query(
            models.Product,
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
            func.max(models.InventoryMovement.created_at).label("last_movement_at"),
        )
        .join(models.InventoryMovement, models.InventoryMovement.product_id == models.Product.id)
        .filter(models.Product.tenant_id == tenant_id)
        .filter(models.InventoryMovement.tenant_id == tenant_id)
        .filter(models.Product.active.is_(True))
        .filter(models.Product.service.is_(False))
        .group_by(models.Product.id)
        .having(func.sum(models.InventoryMovement.qty_delta) < 0)
    )
    rows = stock_query.all()
    negative_count = len(rows)
    clean_group = " ".join((group_name or "").strip().split())
    if clean_group:
        normalized = clean_group.lower()
        rows = [
            row
            for row in rows
            if (row.Product.group_name or "").strip().lower() == normalized
            or (row.Product.group_name or "").strip().lower().startswith(f"{normalized}/")
        ]
    if not rows:
        return [], negative_count

    product_ids = [int(row.Product.id) for row in rows]
    lookback_start = now - timedelta(days=lookback_days)
    sale_rows = (
        db.query(
            models.SaleItem.product_id,
            func.coalesce(func.sum(models.SaleItem.quantity), 0).label("units_sold"),
            func.max(models.Sale.created_at).label("last_sale_at"),
        )
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.SaleItem.product_id.in_(product_ids))
        .filter(models.Sale.created_at >= lookback_start)
        .filter(or_(models.Sale.status.is_(None), ~models.Sale.status.in_(["voided", "cancelled"])))
        .group_by(models.SaleItem.product_id)
        .all()
    )
    sales = {
        int(row.product_id): (float(row.units_sold or 0), row.last_sale_at)
        for row in sale_rows
    }
    recount_rows = (
        db.query(
            models.InventoryRecountLine.product_id,
            func.max(models.InventoryRecount.applied_at).label("last_recount_at"),
        )
        .join(models.InventoryRecount, models.InventoryRecount.id == models.InventoryRecountLine.recount_id)
        .filter(models.InventoryRecount.tenant_id == tenant_id)
        .filter(models.InventoryRecountLine.product_id.in_(product_ids))
        .filter(models.InventoryRecount.status == "applied")
        .group_by(models.InventoryRecountLine.product_id)
        .all()
    )
    recounts = {int(row.product_id): row.last_recount_at for row in recount_rows}
    excluded = _recently_planned_product_ids(db, tenant_id=tenant_id, now=now)

    candidates: list[PlanCandidate] = []
    for row in rows:
        product = row.Product
        product_id = int(product.id)
        if product_id in excluded:
            continue
        qty = float(row.qty_on_hand or 0)
        negative_units = abs(qty)
        units_sold, last_sale_at = sales.get(product_id, (0.0, None))
        last_movement_at = row.last_movement_at
        last_recount_at = recounts.get(product_id)
        cost = max(float(product.cost or 0), 0)
        price = max(float(product.price or 0), 0)
        cost_impact = negative_units * cost
        sale_impact = negative_units * price
        score = min(negative_units * 4.0, 40.0)
        reasons: list[str] = [f"stock {qty:g}"]
        if units_sold > 0:
            score += min(units_sold * 3.0, 30.0)
            reasons.append(f"{units_sold:g} uds. vendidas en {lookback_days} días")
        if last_movement_at:
            movement_age = max(0, (now - last_movement_at).days)
            if movement_age <= 7:
                score += 20.0
                reasons.append("movimiento en los últimos 7 días")
            elif movement_age <= 30:
                score += 10.0
                reasons.append("movimiento reciente")
        if bool(product.web_published):
            score += 8.0
            reasons.append("publicado en Comercio Web")
        score += min(cost_impact / 50_000.0, 20.0)
        if cost_impact >= 100_000:
            reasons.append("impacto económico relevante")
        if last_recount_at and last_recount_at >= now - timedelta(days=14):
            score -= 25.0
            reasons.append("recuento reciente; verificar recurrencia")
        candidates.append(
            PlanCandidate(
                product=product,
                qty_on_hand=qty,
                units_sold=units_sold,
                last_sale_at=last_sale_at,
                last_movement_at=last_movement_at,
                last_recount_at=last_recount_at,
                cost_impact=cost_impact,
                sale_impact=sale_impact,
                score=round(score, 2),
                reasons=tuple(reasons),
            )
        )
    return candidates, negative_count


def _group_family(value: str | None) -> str:
    clean = " ".join((value or "Sin categoría").strip().split()) or "Sin categoría"
    return clean.split("/", 1)[0]


def _select_candidates(
    candidates: list[PlanCandidate],
    *,
    requested_count: int,
    requested_group: str | None,
) -> tuple[list[PlanCandidate], str | None]:
    if not candidates:
        return [], None
    if requested_group:
        selected_group = requested_group
        pool = candidates
    else:
        grouped: dict[str, list[PlanCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(_group_family(candidate.product.group_name), []).append(candidate)
        eligible_groups = [
            entry for entry in grouped.items() if len(entry[1]) >= min(MIN_PLAN_ITEMS, requested_count)
        ] or list(grouped.items())
        selected_group, pool = max(
            eligible_groups,
            key=lambda entry: (
                sum(
                    item.score
                    for item in sorted(entry[1], key=lambda row: row.score, reverse=True)[:requested_count]
                )
                + min(len(entry[1]), requested_count) * 5,
                min(len(entry[1]), requested_count),
            ),
        )
        if len(pool) < MIN_PLAN_ITEMS and len(candidates) >= MIN_PLAN_ITEMS:
            selected_group = None
            pool = candidates
    pool.sort(
        key=lambda item: (
            item.score,
            item.units_sold,
            abs(item.qty_on_hand),
            item.cost_impact,
        ),
        reverse=True,
    )
    return pool[:requested_count], selected_group


def _context_snapshot(context: OperationalContext) -> dict[str, object]:
    return {
        "generated_at": context.generated_at.isoformat(),
        "local_date": context.local_date.isoformat(),
        "scheduled_people": context.scheduled_people,
        "scheduled_names": list(context.scheduled_names),
        "schedule_status": context.schedule_status,
        "reserved_for_sales": context.reserved_for_sales,
        "reserved_for_receiving": context.reserved_for_receiving,
        "available_people": context.available_people,
        "open_receiving_count": context.open_receiving_count,
        "open_receiving_codes": list(context.open_receiving_codes),
        "sales_count_30m": context.sales_count_30m,
        "sales_total_30m": context.sales_total_30m,
        "workload_state": context.workload_state,
        "automatic_plan_allowed": context.automatic_plan_allowed,
        "automatic_reason": context.automatic_reason,
        "presence_basis": "configured_schedule",
    }


def retrieve_or_create_plan(
    db: Session,
    *,
    tenant_id: int,
    requested_count: int = DEFAULT_REQUESTED_COUNT,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    group_name: str | None = None,
    trigger: Literal["manual", "automatic"] = "manual",
    created_by_user_id: int | None = None,
    reference_time: datetime | None = None,
) -> PlanBuildResult:
    requested_count = max(MIN_PLAN_ITEMS, min(int(requested_count or DEFAULT_REQUESTED_COUNT), MAX_PLAN_ITEMS))
    lookback_days = max(7, min(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 90))
    now = _utc_naive(reference_time)
    context = read_operational_context(db, tenant_id=tenant_id, reference_time=reference_time)
    existing = get_current_plan(db, tenant_id=tenant_id, reference_time=reference_time)
    if existing is not None:
        return PlanBuildResult(
            state="existing",
            plan=existing,
            context=context,
            message="Ya existe un plan de saneamiento disponible; recuperé la misma lista para evitar duplicados.",
        )
    if trigger == "automatic" and not context.automatic_plan_allowed:
        return PlanBuildResult(
            state="not_eligible",
            plan=None,
            context=context,
            message=context.automatic_reason,
        )

    candidates, negative_count = _candidate_rows(
        db,
        tenant_id=tenant_id,
        lookback_days=lookback_days,
        group_name=group_name,
        now=now,
    )
    selected, selected_group = _select_candidates(
        candidates,
        requested_count=requested_count,
        requested_group=group_name,
    )
    if not selected:
        return PlanBuildResult(
            state="no_candidates",
            plan=None,
            context=context,
            message="No encontré productos negativos elegibles que no hayan sido incluidos recientemente.",
        )

    display_group = selected_group or "varias categorías"
    title = f"Saneamiento de stock · {display_group}"
    plan = models.KoraStockPlan(
        tenant_id=tenant_id,
        status="ready",
        trigger=trigger,
        title=title,
        group_name=selected_group,
        requested_count=requested_count,
        lookback_days=lookback_days,
        negative_sku_count=negative_count,
        selected_count=len(selected),
        total_negative_units=round(sum(abs(item.qty_on_hand) for item in selected), 2),
        total_cost_impact=round(sum(item.cost_impact for item in selected), 2),
        total_sale_impact=round(sum(item.sale_impact for item in selected), 2),
        scheduled_people=context.scheduled_people,
        reserved_for_sales=context.reserved_for_sales,
        reserved_for_receiving=context.reserved_for_receiving,
        available_people=context.available_people,
        open_receiving_count=context.open_receiving_count,
        sales_count_30m=context.sales_count_30m,
        sales_total_30m=context.sales_total_30m,
        workload_state=context.workload_state,
        context_snapshot=_context_snapshot(context),
        created_by_user_id=created_by_user_id,
        created_at=now,
        expires_at=now + timedelta(hours=PLAN_TTL_HOURS),
    )
    db.add(plan)
    db.flush()
    plan.code = f"KSP-{plan.id:06d}"
    for rank, candidate in enumerate(selected, start=1):
        product = candidate.product
        db.add(
            models.KoraStockPlanItem(
                tenant_id=tenant_id,
                plan_id=plan.id,
                product_id=product.id,
                product_name_snapshot=product.name,
                sku_snapshot=product.sku,
                barcode_snapshot=product.barcode,
                group_name_snapshot=product.group_name,
                system_qty_snapshot=candidate.qty_on_hand,
                unit_cost_snapshot=max(float(product.cost or 0), 0),
                unit_price_snapshot=max(float(product.price or 0), 0),
                cost_impact_snapshot=round(candidate.cost_impact, 2),
                sale_impact_snapshot=round(candidate.sale_impact, 2),
                units_sold_lookback=round(candidate.units_sold, 2),
                web_published_snapshot=bool(product.web_published),
                priority_rank=rank,
                priority_score=candidate.score,
                reasons=list(candidate.reasons),
                last_sale_at=candidate.last_sale_at,
                last_movement_at=candidate.last_movement_at,
                last_recount_at=candidate.last_recount_at,
            )
        )
    db.commit()
    return PlanBuildResult(
        state="ready",
        plan=get_current_plan(db, tenant_id=tenant_id, reference_time=reference_time),
        context=context,
        message=f"Preparé {len(selected)} productos para sanear stock en {display_group}.",
    )


def list_plans(
    db: Session,
    *,
    tenant_id: int,
    limit: int = 20,
) -> list[models.KoraStockPlan]:
    return (
        db.query(models.KoraStockPlan)
        .options(selectinload(models.KoraStockPlan.items))
        .filter(models.KoraStockPlan.tenant_id == tenant_id)
        .order_by(models.KoraStockPlan.created_at.desc(), models.KoraStockPlan.id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


def serialize_plan(plan: models.KoraStockPlan) -> dict[str, object]:
    snapshot = dict(plan.context_snapshot or {})
    context = {
        "scheduled_people": plan.scheduled_people,
        "scheduled_names": list(snapshot.get("scheduled_names") or []),
        "schedule_status": snapshot.get("schedule_status"),
        "reserved_for_sales": int(plan.reserved_for_sales or 0),
        "reserved_for_receiving": int(plan.reserved_for_receiving or 0),
        "available_people": plan.available_people,
        "open_receiving_count": int(plan.open_receiving_count or 0),
        "open_receiving_codes": list(snapshot.get("open_receiving_codes") or []),
        "sales_count_30m": int(plan.sales_count_30m or 0),
        "sales_total_30m": float(plan.sales_total_30m or 0),
        "workload_state": plan.workload_state or "unknown",
        "automatic_plan_allowed": bool(snapshot.get("automatic_plan_allowed", False)),
        "automatic_reason": str(snapshot.get("automatic_reason") or ""),
        "presence_basis": str(snapshot.get("presence_basis") or "configured_schedule"),
    }
    return {
        "id": int(plan.id),
        "code": str(plan.code or f"KSP-{plan.id:06d}"),
        "status": plan.status,
        "trigger": plan.trigger,
        "title": plan.title,
        "group_name": plan.group_name,
        "requested_count": int(plan.requested_count or 0),
        "lookback_days": int(plan.lookback_days or DEFAULT_LOOKBACK_DAYS),
        "negative_sku_count": int(plan.negative_sku_count or 0),
        "selected_count": int(plan.selected_count or 0),
        "total_negative_units": float(plan.total_negative_units or 0),
        "total_cost_impact": float(plan.total_cost_impact or 0),
        "total_sale_impact": float(plan.total_sale_impact or 0),
        "workload_state": plan.workload_state or "unknown",
        "converted_recount_id": plan.converted_recount_id,
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
        "converted_at": plan.converted_at,
        "completed_at": plan.completed_at,
        "context": context,
        "items": [
            {
                "id": int(item.id),
                "product_id": int(item.product_id),
                "product_name": item.product_name_snapshot,
                "sku": item.sku_snapshot,
                "barcode": item.barcode_snapshot,
                "group_name": item.group_name_snapshot,
                "system_qty": float(item.system_qty_snapshot or 0),
                "unit_cost": float(item.unit_cost_snapshot or 0),
                "unit_price": float(item.unit_price_snapshot or 0),
                "cost_impact": float(item.cost_impact_snapshot or 0),
                "sale_impact": float(item.sale_impact_snapshot or 0),
                "units_sold_lookback": float(item.units_sold_lookback or 0),
                "web_published": bool(item.web_published_snapshot),
                "priority_rank": int(item.priority_rank),
                "priority_score": float(item.priority_score or 0),
                "reasons": list(item.reasons or []),
                "last_sale_at": item.last_sale_at,
                "last_movement_at": item.last_movement_at,
                "last_recount_at": item.last_recount_at,
            }
            for item in sorted(plan.items, key=lambda row: row.priority_rank)
        ],
    }
