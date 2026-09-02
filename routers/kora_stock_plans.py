from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

import crud
import models
import schemas
from database import get_db
from dependencies import get_current_active_user, get_current_tenant_id, require_permission
from routers.inventory import build_inventory_recount_read
from services import kora_stock_sanitization


router = APIRouter(
    prefix="/kora/stock-sanitization-plans",
    tags=["kora", "inventory"],
    dependencies=[Depends(require_permission("movements.view"))],
)


def _response(
    *,
    state: str,
    message: str,
    plan: models.KoraStockPlan | None,
) -> schemas.KoraStockPlanResponse:
    return schemas.KoraStockPlanResponse(
        generated_at=datetime.utcnow(),
        state=state,
        message=message,
        plan=(
            schemas.KoraStockPlanRead.model_validate(
                kora_stock_sanitization.serialize_plan(plan)
            )
            if plan is not None
            else None
        ),
    )


@router.get("", response_model=schemas.KoraStockPlanListRead)
def get_stock_sanitization_plans(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    tenant_id: int = Depends(get_current_tenant_id),
):
    plans = kora_stock_sanitization.list_plans(db, tenant_id=tenant_id, limit=limit)
    return schemas.KoraStockPlanListRead(
        items=[
            schemas.KoraStockPlanRead.model_validate(
                kora_stock_sanitization.serialize_plan(plan)
            )
            for plan in plans
        ],
        total=len(plans),
    )


@router.get("/current", response_model=schemas.KoraStockPlanResponse)
def get_current_stock_sanitization_plan(
    db: Session = Depends(get_db),
    tenant_id: int = Depends(get_current_tenant_id),
):
    plan = kora_stock_sanitization.get_current_plan(db, tenant_id=tenant_id)
    if plan is None:
        return _response(
            state="none",
            message="No hay un plan de saneamiento vigente.",
            plan=None,
        )
    return _response(
        state="existing",
        message="Recuperé el plan de saneamiento vigente.",
        plan=plan,
    )


@router.post("/retrieve", response_model=schemas.KoraStockPlanResponse)
def retrieve_stock_sanitization_plan(
    payload: schemas.KoraStockPlanRetrieveRequest,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(get_current_tenant_id),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    result = kora_stock_sanitization.retrieve_or_create_plan(
        db,
        tenant_id=tenant_id,
        requested_count=payload.requested_count,
        lookback_days=payload.lookback_days,
        group_name=payload.group_name,
        trigger="manual",
        created_by_user_id=current_user.id,
    )
    return _response(state=result.state, message=result.message, plan=result.plan)


@router.post(
    "/{plan_id}/convert",
    response_model=schemas.KoraStockPlanConversionRead,
)
def convert_stock_sanitization_plan(
    plan_id: int,
    payload: schemas.KoraStockPlanConvertRequest,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(get_current_tenant_id),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    plan = (
        db.query(models.KoraStockPlan)
        .options(
            selectinload(models.KoraStockPlan.items),
            selectinload(models.KoraStockPlan.converted_recount),
        )
        .filter(
            models.KoraStockPlan.id == plan_id,
            models.KoraStockPlan.tenant_id == tenant_id,
        )
        .with_for_update()
        .first()
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan de saneamiento no encontrado.")

    now = datetime.utcnow()
    if plan.status == "converted" and plan.converted_recount is not None:
        return schemas.KoraStockPlanConversionRead(
            plan=schemas.KoraStockPlanRead.model_validate(
                kora_stock_sanitization.serialize_plan(plan)
            ),
            recount=build_inventory_recount_read(db, plan.converted_recount, tenant_id),
        )
    if plan.status != "ready":
        raise HTTPException(status_code=409, detail="Este plan ya no está disponible para iniciar un recuento.")
    if plan.expires_at and plan.expires_at <= now:
        plan.status = "expired"
        db.commit()
        raise HTTPException(status_code=409, detail="Este plan venció. Solicita uno nuevo a Kora.")
    if not plan.items:
        raise HTTPException(status_code=409, detail="El plan no contiene productos para contar.")

    device_id = payload.stock_device_id.strip()
    stock_device = crud.get_stock_device(db, device_id, tenant_id=tenant_id)
    if stock_device is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DEVICE_NOT_ALLOWED",
                "message": "El dispositivo de inventario no existe para esta empresa.",
            },
        )
    if not stock_device.is_active:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DEVICE_BLOCKED",
                "message": "El dispositivo de inventario está inactivo.",
            },
        )

    open_count = (
        db.query(func.count(models.InventoryRecount.id))
        .filter(
            models.InventoryRecount.tenant_id == tenant_id,
            models.InventoryRecount.status.in_(["draft", "counting", "closed"]),
        )
        .scalar()
        or 0
    )
    if open_count >= 2:
        raise HTTPException(
            status_code=409,
            detail="Hay dos recuentos en curso o pendientes. Finaliza uno antes de iniciar el plan de Kora.",
        )

    product_ids = [int(item.product_id) for item in plan.items]
    stock_rows = (
        db.query(
            models.InventoryMovement.product_id,
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .filter(
            models.InventoryMovement.tenant_id == tenant_id,
            models.InventoryMovement.product_id.in_(product_ids),
        )
        .group_by(models.InventoryMovement.product_id)
        .all()
    )
    current_stock = {int(row.product_id): float(row.qty_on_hand or 0) for row in stock_rows}
    recount = models.InventoryRecount(
        tenant_id=tenant_id,
        status="counting",
        source="app",
        stock_device_id=stock_device.id,
        stock_device_name=stock_device.name,
        scope_type="free",
        count_mode=payload.count_mode,
        title=f"Kora · {plan.code}",
        notes=f"Recuento guiado desde el plan de saneamiento {plan.code}.",
        created_by_user_id=current_user.id,
        started_at=now,
    )
    db.add(recount)
    db.flush()
    recount.code = f"RCN-{recount.id:06d}"
    for item in sorted(plan.items, key=lambda row: row.priority_rank):
        db.add(
            models.InventoryRecountLine(
                tenant_id=tenant_id,
                recount_id=recount.id,
                product_id=item.product_id,
                product_name_snapshot=item.product_name_snapshot,
                sku_snapshot=item.sku_snapshot,
                barcode_snapshot=item.barcode_snapshot,
                group_name_snapshot=item.group_name_snapshot,
                system_qty=current_stock.get(int(item.product_id), 0.0),
            )
        )
    plan.status = "converted"
    plan.converted_recount_id = recount.id
    plan.converted_at = now
    stock_device.last_seen_at = now
    db.commit()
    db.refresh(recount)
    db.refresh(plan)

    return schemas.KoraStockPlanConversionRead(
        plan=schemas.KoraStockPlanRead.model_validate(
            kora_stock_sanitization.serialize_plan(plan)
        ),
        recount=build_inventory_recount_read(db, recount, tenant_id),
    )


@router.get("/{plan_id}", response_model=schemas.KoraStockPlanRead)
def get_stock_sanitization_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(get_current_tenant_id),
):
    plan = (
        db.query(models.KoraStockPlan)
        .options(selectinload(models.KoraStockPlan.items))
        .filter(
            models.KoraStockPlan.id == plan_id,
            models.KoraStockPlan.tenant_id == tenant_id,
        )
        .first()
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan de saneamiento no encontrado.")
    return schemas.KoraStockPlanRead.model_validate(
        kora_stock_sanitization.serialize_plan(plan)
    )
