from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import case, func, or_
from fastapi.responses import StreamingResponse
import csv
import io
import pandas as pd

import models, schemas
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
)


@router.get("/overview", response_model=schemas.InventoryOverview)
def get_inventory_overview(
    status_limit: int = Query(default=6, ge=1, le=20),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    stock_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )

    product_rows = (
        db.query(
            models.Product,
            func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
        .all()
    )

    total_qty = 0.0
    low_stock_count = 0
    critical_count = 0
    reorder_count = 0
    status_rows: List[schemas.InventoryStatusRow] = []

    for product, qty_on_hand in product_rows:
        qty = float(qty_on_hand or 0.0)
        total_qty += qty
        if qty <= 0:
            critical_count += 1
            status = "critical"
        elif product.low_stock_alert and product.stock_min > 0 and qty <= product.stock_min:
            low_stock_count += 1
            status = "low"
        else:
            status = "ok"

        if product.low_stock_alert and product.reorder_point > 0 and qty <= product.reorder_point:
            reorder_count += 1

        status_rows.append(
            schemas.InventoryStatusRow(
                product_id=product.id,
                product_name=product.name,
                qty_on_hand=qty,
                status=status,
            )
        )

    def status_rank(item: schemas.InventoryStatusRow) -> int:
        if item.status == "critical":
            return 0
        if item.status == "low":
            return 1
        return 2

    status_rows_sorted = sorted(status_rows, key=status_rank)[:status_limit]

    movement_rows = (
        db.query(models.InventoryMovement, models.Product.name)
        .join(models.Product, models.Product.id == models.InventoryMovement.product_id)
        .order_by(models.InventoryMovement.created_at.desc())
        .limit(8)
        .all()
    )

    recent_movements: List[schemas.InventoryMovementRead] = []
    for movement, product_name in movement_rows:
        recent_movements.append(
            schemas.InventoryMovementRead(
                id=movement.id,
                product_id=movement.product_id,
                product_name=product_name,
                qty_delta=float(movement.qty_delta or 0.0),
                reason=movement.reason,
                notes=movement.notes,
                reference_type=movement.reference_type,
                reference_id=movement.reference_id,
                created_at=movement.created_at,
                created_by_user_id=movement.created_by_user_id,
            )
        )

    summary = schemas.InventorySummary(
        total_qty=total_qty,
        low_stock_count=low_stock_count,
        critical_count=critical_count,
        anomaly_count=0,
        reorder_count=reorder_count,
    )

    return schemas.InventoryOverview(
        summary=summary,
        recent_movements=recent_movements,
        status_rows=status_rows_sorted,
    )


@router.get("/movements", response_model=List[schemas.InventoryMovementRead])
def list_inventory_movements(
    skip: int = 0,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    movement_rows = (
        db.query(models.InventoryMovement, models.Product.name)
        .join(models.Product, models.Product.id == models.InventoryMovement.product_id)
        .order_by(models.InventoryMovement.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    results: List[schemas.InventoryMovementRead] = []
    for movement, product_name in movement_rows:
        results.append(
            schemas.InventoryMovementRead(
                id=movement.id,
                product_id=movement.product_id,
                product_name=product_name,
                qty_delta=float(movement.qty_delta or 0.0),
                reason=movement.reason,
                notes=movement.notes,
                reference_type=movement.reference_type,
                reference_id=movement.reference_id,
                created_at=movement.created_at,
                created_by_user_id=movement.created_by_user_id,
            )
        )

    return results


def _apply_product_filters(query, qty_col, search: str | None, stock: str | None):
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(
            or_(
                models.Product.name.ilike(pattern),
                models.Product.sku.ilike(pattern),
                models.Product.barcode.ilike(pattern),
            )
        )

    if stock == "positive":
        query = query.filter(qty_col > 0)
    elif stock == "zero":
        query = query.filter(qty_col == 0)
    elif stock == "negative":
        query = query.filter(qty_col < 0)

    return query


def _apply_product_sort(query, qty_col, sort: str | None):
    if sort == "stock_asc":
        return query.order_by(qty_col.asc(), models.Product.name.asc())
    if sort == "stock_desc":
        return query.order_by(qty_col.desc(), models.Product.name.asc())
    return query.order_by(models.Product.name.asc())


@router.get("/products", response_model=schemas.InventoryProductPage)
def list_inventory_products(
    skip: int = 0,
    limit: int = Query(default=200, ge=1, le=1000),
    search: str | None = Query(default=None, min_length=1),
    stock: str | None = Query(default=None, pattern="^(all|positive|zero|negative)$"),
    sort: str | None = Query(
        default="name_asc", pattern="^(name_asc|stock_asc|stock_desc)$"
    ),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    stock_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label(
                "qty_on_hand"
            ),
        )
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )

    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)

    product_rows = (
        db.query(
            models.Product,
            qty_col.label("qty_on_hand"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
    )
    product_rows = _apply_product_filters(product_rows, qty_col, search, stock)
    product_rows = _apply_product_sort(product_rows, qty_col, sort)
    product_rows = product_rows.offset(skip).limit(limit).all()

    count_query = (
        db.query(func.count(models.Product.id))
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
    )
    count_query = _apply_product_filters(count_query, qty_col, search, stock)
    total = int(count_query.scalar() or 0)

    totals_query = (
        db.query(
            func.coalesce(func.sum(qty_col * models.Product.cost), 0).label(
                "total_cost"
            ),
            func.coalesce(func.sum(qty_col * models.Product.price), 0).label(
                "total_price"
            ),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
    )
    totals_query = _apply_product_filters(totals_query, qty_col, search, stock)
    totals_row = totals_query.first()
    total_cost_value = float(getattr(totals_row, "total_cost", 0.0) or 0.0)
    total_price_value = float(getattr(totals_row, "total_price", 0.0) or 0.0)

    results: List[schemas.InventoryProductRow] = []
    for product, qty_on_hand in product_rows:
        qty = float(qty_on_hand or 0.0)
        if qty <= 0:
            status = "critical"
        elif product.low_stock_alert and product.stock_min > 0 and qty <= product.stock_min:
            status = "low"
        else:
            status = "ok"

        results.append(
            schemas.InventoryProductRow(
                product_id=product.id,
                product_name=product.name,
                sku=product.sku,
                barcode=product.barcode,
                qty_on_hand=qty,
                status=status,
                cost=float(product.cost or 0.0),
                price=float(product.price or 0.0),
            )
        )

    return schemas.InventoryProductPage(
        items=results,
        total=int(total),
        skip=skip,
        limit=limit,
        total_cost_value=total_cost_value,
        total_price_value=total_price_value,
    )


@router.get("/products/export")
def export_inventory_products(
    search: str | None = Query(default=None, min_length=1),
    stock: str | None = Query(default=None, pattern="^(all|positive|zero|negative)$"),
    sort: str | None = Query(
        default="name_asc", pattern="^(name_asc|stock_asc|stock_desc)$"
    ),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    stock_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label(
                "qty_on_hand"
            ),
        )
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )
    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)
    product_rows = (
        db.query(
            models.Product,
            qty_col.label("qty_on_hand"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
    )
    product_rows = _apply_product_filters(product_rows, qty_col, search, stock)
    product_rows = _apply_product_sort(product_rows, qty_col, sort).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "producto_id",
            "sku",
            "codigo_barras",
            "nombre",
            "stock",
            "estado",
            "costo",
            "precio",
        ]
    )

    for product, qty_on_hand in product_rows:
        qty = float(qty_on_hand or 0.0)
        if qty <= 0:
            status = "critico"
        elif product.low_stock_alert and product.stock_min > 0 and qty <= product.stock_min:
            status = "bajo"
        else:
            status = "ok"
        writer.writerow(
            [
                product.id,
                product.sku or "",
                product.barcode or "",
                product.name,
                qty,
                status,
                float(product.cost or 0.0),
                float(product.price or 0.0),
            ]
        )

    output.seek(0)
    response = StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = "attachment; filename=inventario.csv"
    return response


@router.get("/products/export/xlsx")
def export_inventory_products_xlsx(
    search: str | None = Query(default=None, min_length=1),
    stock: str | None = Query(default=None, pattern="^(all|positive|zero|negative)$"),
    sort: str | None = Query(
        default="name_asc", pattern="^(name_asc|stock_asc|stock_desc)$"
    ),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    stock_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label(
                "qty_on_hand"
            ),
        )
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )
    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)
    product_rows = (
        db.query(
            models.Product,
            qty_col.label("qty_on_hand"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.service.is_(False))
        .filter(models.Product.active.is_(True))
    )
    product_rows = _apply_product_filters(product_rows, qty_col, search, stock)
    product_rows = _apply_product_sort(product_rows, qty_col, sort).all()

    rows = []
    for product, qty_on_hand in product_rows:
        qty = float(qty_on_hand or 0.0)
        if qty <= 0:
            status = "critico"
        elif product.low_stock_alert and product.stock_min > 0 and qty <= product.stock_min:
            status = "bajo"
        else:
            status = "ok"
        rows.append(
            {
                "producto_id": product.id,
                "sku": product.sku or "",
                "codigo_barras": product.barcode or "",
                "nombre": product.name,
                "stock": qty,
                "estado": status,
                "costo": float(product.cost or 0.0),
                "precio": float(product.price or 0.0),
            }
        )

    output = io.BytesIO()
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Inventario")
    output.seek(0)
    headers = {
        "Content-Disposition": 'attachment; filename="inventario.xlsx"'
    }
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/products/{product_id}/history", response_model=schemas.InventoryProductHistory)
def get_product_history(
    product_id: int,
    skip: int = 0,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    totals = (
        db.query(
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("net"),
            func.coalesce(
                func.sum(
                    case(
                        (models.InventoryMovement.qty_delta > 0, models.InventoryMovement.qty_delta),
                        else_=0,
                    )
                ),
                0,
            ).label("total_in"),
            func.coalesce(
                func.sum(
                    case(
                        (models.InventoryMovement.qty_delta < 0, -models.InventoryMovement.qty_delta),
                        else_=0,
                    )
                ),
                0,
            ).label("total_out"),
        )
        .filter(models.InventoryMovement.product_id == product_id)
        .first()
    )
    net = float(getattr(totals, "net", 0.0) or 0.0)
    total_in = float(getattr(totals, "total_in", 0.0) or 0.0)
    total_out = float(getattr(totals, "total_out", 0.0) or 0.0)

    movement_query = (
        db.query(models.InventoryMovement)
        .filter(models.InventoryMovement.product_id == product_id)
    )
    total_movements = int(
        movement_query.with_entities(func.count(models.InventoryMovement.id)).scalar()
        or 0
    )
    movement_rows = (
        movement_query.order_by(models.InventoryMovement.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    movements: List[schemas.InventoryProductMovement] = []
    for movement in movement_rows:
        movements.append(
            schemas.InventoryProductMovement(
                id=movement.id,
                reason=movement.reason,
                qty_delta=float(movement.qty_delta or 0.0),
                notes=movement.notes,
                reference_type=movement.reference_type,
                reference_id=movement.reference_id,
                created_at=movement.created_at,
            )
        )

    return schemas.InventoryProductHistory(
        product_id=product.id,
        product_name=product.name,
        qty_on_hand=net,
        total_in=total_in,
        total_out=total_out,
        net=net,
        movements=movements,
        total_movements=total_movements,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/movements",
    response_model=schemas.InventoryMovementRead,
    status_code=status.HTTP_201_CREATED,
)
def create_inventory_movement(
    payload: schemas.InventoryMovementCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.manage")),
):
    if payload.qty_delta == 0:
        raise HTTPException(status_code=400, detail="La cantidad no puede ser 0")

    product = db.query(models.Product).filter(models.Product.id == payload.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    if product.service:
        raise HTTPException(
            status_code=400,
            detail="No se pueden mover inventarios de productos tipo servicio",
        )

    movement = models.InventoryMovement(
        product_id=payload.product_id,
        qty_delta=payload.qty_delta,
        reason=payload.reason,
        notes=payload.notes,
        reference_type=payload.reference_type,
        reference_id=payload.reference_id,
        created_by_user_id=current_user.id,
    )
    db.add(movement)
    db.commit()
    db.refresh(movement)

    return schemas.InventoryMovementRead(
        id=movement.id,
        product_id=movement.product_id,
        product_name=product.name,
        qty_delta=float(movement.qty_delta or 0.0),
        reason=movement.reason,
        notes=movement.notes,
        reference_type=movement.reference_type,
        reference_id=movement.reference_id,
        created_at=movement.created_at,
        created_by_user_id=movement.created_by_user_id,
    )
