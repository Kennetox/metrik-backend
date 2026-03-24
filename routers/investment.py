from io import BytesIO
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
import pandas as pd
from sqlalchemy import func, or_, true
from sqlalchemy.orm import Session

import crud, models, schemas
from database import get_db
from dependencies import require_module_access
from services import pdf_utils


router = APIRouter(
    prefix="/investment",
    tags=["investment"],
)


def _resolve_tenant_id(db: Session, user: models.PosUser) -> int:
    tenant_id = crud.resolve_user_tenant_id(db, user)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="Usuario sin empresa asignada")
    return tenant_id


def _compute_cut_metrics(
    db: Session,
    *,
    tenant_id: int,
    period_start: datetime,
    period_end: datetime,
) -> Tuple[float, float, float, float]:
    if period_end <= period_start:
        raise HTTPException(status_code=400, detail="Rango de fechas inválido para el corte")

    sales_row = (
        db.query(
            func.coalesce(func.sum(models.SaleItem.total), 0).label("gross_sales"),
            func.coalesce(
                func.sum(models.SaleItem.quantity * func.coalesce(models.Product.cost, 0)),
                0,
            ).label("cogs"),
        )
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.status == "active")
        .filter(models.Sale.created_at >= period_start)
        .filter(models.Sale.created_at < period_end)
        .filter(models.Product.is_investment.is_(True))
        .first()
    )
    returns_row = (
        db.query(
            func.coalesce(func.sum(models.SaleReturnItem.total_refund), 0).label("refund_sales"),
            func.coalesce(
                func.sum(models.SaleReturnItem.quantity * func.coalesce(models.Product.cost, 0)),
                0,
            ).label("refund_cogs"),
        )
        .join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.return_id)
        .join(models.Product, models.Product.id == models.SaleReturnItem.product_id)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.SaleReturn.created_at >= period_start)
        .filter(models.SaleReturn.created_at < period_end)
        .filter(models.Product.is_investment.is_(True))
        .first()
    )
    sold_gross = float(getattr(sales_row, "gross_sales", 0.0) or 0.0)
    sold_cogs = float(getattr(sales_row, "cogs", 0.0) or 0.0)
    refund_sales = float(getattr(returns_row, "refund_sales", 0.0) or 0.0)
    refund_cogs = float(getattr(returns_row, "refund_cogs", 0.0) or 0.0)
    gross_sales = sold_gross - refund_sales
    cogs = sold_cogs - refund_cogs
    collected_sales = gross_sales
    profit_base = gross_sales - cogs
    return gross_sales, collected_sales, cogs, profit_base


def _participant_read(model: models.InvestmentParticipant) -> schemas.InvestmentParticipantRead:
    return schemas.InvestmentParticipantRead.model_validate(model)


def _period_start_for(reference: datetime) -> datetime:
    day = reference.day
    if day <= 15:
        return reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return reference.replace(day=16, hour=0, minute=0, second=0, microsecond=0)


def _period_end_for(period_start: datetime) -> datetime:
    if period_start.day == 1:
        return period_start.replace(day=16, hour=0, minute=0, second=0, microsecond=0)
    if period_start.month == 12:
        return period_start.replace(
            year=period_start.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return period_start.replace(
        month=period_start.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _active_participants(db: Session, *, tenant_id: int) -> List[models.InvestmentParticipant]:
    return (
        db.query(models.InvestmentParticipant)
        .filter(models.InvestmentParticipant.tenant_id == tenant_id)
        .filter(models.InvestmentParticipant.is_active.is_(True))
        .all()
    )


def _build_cut_allocations(
    *,
    participants: List[models.InvestmentParticipant],
    cogs: float,
    profit_base: float,
) -> List[schemas.InvestmentCutAllocationRead]:
    total_profit_share = sum(
        max(float(row.profit_share_percent or row.share_percent or 0.0), 0.0)
        for row in participants
    )
    total_capital_share = sum(max(float(row.capital_share_percent or 0.0), 0.0) for row in participants)
    allocations: List[schemas.InvestmentCutAllocationRead] = []
    for row in participants:
        profit_share = max(float(row.profit_share_percent or row.share_percent or 0.0), 0.0)
        capital_share = max(float(row.capital_share_percent or 0.0), 0.0)
        profit_amount = (
            float(profit_base * profit_share / total_profit_share)
            if total_profit_share > 0
            else 0.0
        )
        capital_amount = (
            float(cogs * capital_share / total_capital_share)
            if total_capital_share > 0
            else 0.0
        )
        allocations.append(
            schemas.InvestmentCutAllocationRead(
                participant_id=row.id,
                participant_name=row.display_name,
                share_percent=profit_share,
                profit_share_percent=profit_share,
                capital_share_percent=capital_share,
                profit_amount=profit_amount,
                capital_amount=capital_amount,
                amount_due=float(capital_amount + profit_amount),
            )
        )
    return allocations


def _ensure_automatic_quincenal_cuts(db: Session, *, tenant_id: int, as_of: datetime) -> None:
    first_sale_at = (
        db.query(func.min(models.Sale.created_at))
        .join(models.SaleItem, models.SaleItem.sale_id == models.Sale.id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.status == "active")
        .filter(models.Product.is_investment.is_(True))
        .scalar()
    )
    first_return_at = (
        db.query(func.min(models.SaleReturn.created_at))
        .join(models.SaleReturnItem, models.SaleReturnItem.return_id == models.SaleReturn.id)
        .join(models.Product, models.Product.id == models.SaleReturnItem.product_id)
        .filter(models.SaleReturn.tenant_id == tenant_id)
        .filter(models.SaleReturn.status == "confirmed")
        .filter(models.Product.is_investment.is_(True))
        .scalar()
    )
    candidates = [dt for dt in [first_sale_at, first_return_at] if isinstance(dt, datetime)]
    if not candidates:
        return

    participants = _active_participants(db, tenant_id=tenant_id)
    if not participants:
        return

    first_activity_at = min(candidates)
    period_start = _period_start_for(first_activity_at)
    existing_periods = {
        (row.period_start, row.period_end)
        for row in db.query(models.InvestmentCut.period_start, models.InvestmentCut.period_end)
        .filter(models.InvestmentCut.tenant_id == tenant_id)
        .all()
    }

    created_any = False
    while True:
        period_end = _period_end_for(period_start)
        if period_end > as_of:
            break
        key = (period_start, period_end)
        if key not in existing_periods:
            gross_sales, collected_sales, cogs, profit_base = _compute_cut_metrics(
                db,
                tenant_id=tenant_id,
                period_start=period_start,
                period_end=period_end,
            )
            if abs(gross_sales) > 0.0001 or abs(cogs) > 0.0001 or abs(profit_base) > 0.0001:
                allocations = _build_cut_allocations(
                    participants=participants,
                    cogs=float(cogs),
                    profit_base=float(profit_base),
                )
                if allocations:
                    cut = models.InvestmentCut(
                        tenant_id=tenant_id,
                        period_start=period_start,
                        period_end=period_end,
                        gross_sales=float(gross_sales),
                        collected_sales=float(collected_sales),
                        cogs=float(cogs),
                        profit_base=float(profit_base),
                        notes="Corte quincenal automático",
                        created_by_user_id=None,
                    )
                    db.add(cut)
                    db.flush()
                    for allocation in allocations:
                        db.add(
                            models.InvestmentCutAllocation(
                                tenant_id=tenant_id,
                                cut_id=cut.id,
                                participant_id=allocation.participant_id,
                                share_percent=allocation.share_percent,
                                profit_share_percent=allocation.profit_share_percent,
                                capital_share_percent=allocation.capital_share_percent,
                                profit_amount=allocation.profit_amount,
                                capital_amount=allocation.capital_amount,
                                amount_due=allocation.amount_due,
                            )
                        )
                    created_any = True
            existing_periods.add(key)
        period_start = period_end

    if created_any:
        db.commit()


@router.get("/summary", response_model=schemas.InvestmentSummaryRead)
def get_investment_summary(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    stock_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .filter(models.InventoryMovement.tenant_id == tenant_id if tenant_id is not None else true())
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )
    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)
    products_query = (
        db.query(models.Product)
        .filter(models.Product.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(models.Product.is_investment.is_(True))
    )
    total_products = int(products_query.count())
    active_products = int(products_query.filter(models.Product.active.is_(True)).count())
    totals = (
        db.query(
            func.coalesce(func.sum(qty_col), 0).label("stock_units"),
            func.coalesce(func.sum(qty_col * models.Product.cost), 0).label("stock_cost_value"),
            func.coalesce(func.sum(qty_col * models.Product.price), 0).label("stock_sale_value"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(models.Product.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(models.Product.is_investment.is_(True))
        .first()
    )
    return schemas.InvestmentSummaryRead(
        total_products=total_products,
        active_products=active_products,
        stock_units=float(getattr(totals, "stock_units", 0.0) or 0.0),
        stock_cost_value=float(getattr(totals, "stock_cost_value", 0.0) or 0.0),
        stock_sale_value=float(getattr(totals, "stock_sale_value", 0.0) or 0.0),
    )


@router.get("/products", response_model=List[schemas.InvestmentProductRow])
def list_investment_products(
    search: Optional[str] = Query(default=None, min_length=1),
    skip: int = 0,
    limit: int = Query(default=300, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    stock_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .filter(models.InventoryMovement.tenant_id == tenant_id if tenant_id is not None else true())
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )
    latest_movement_subquery = (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.max(models.InventoryMovement.created_at).label("last_movement_at"),
        )
        .filter(models.InventoryMovement.tenant_id == tenant_id if tenant_id is not None else true())
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )
    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)

    rows = (
        db.query(
            models.Product,
            qty_col.label("qty_on_hand"),
            latest_movement_subquery.c.last_movement_at.label("last_movement_at"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .outerjoin(
            latest_movement_subquery,
            latest_movement_subquery.c.product_id == models.Product.id,
        )
        .filter(models.Product.tenant_id == tenant_id if tenant_id is not None else true())
        .filter(models.Product.is_investment.is_(True))
    )
    if search:
        term = f"%{search.strip()}%"
        rows = rows.filter(
            models.Product.name.ilike(term)
            | models.Product.sku.ilike(term)
            | models.Product.group_name.ilike(term)
        )
    rows = (
        rows.order_by(models.Product.active.desc(), models.Product.name.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    result: List[schemas.InvestmentProductRow] = []
    for product, qty_on_hand, last_movement_at in rows:
        qty = float(qty_on_hand or 0.0)
        if qty <= 0:
            status = "critical"
        elif product.low_stock_alert and product.stock_min > 0 and qty <= product.stock_min:
            status = "low"
        else:
            status = "ok"
        result.append(
            schemas.InvestmentProductRow(
                product_id=product.id,
                product_name=product.name,
                sku=product.sku,
                group_name=product.group_name,
                qty_on_hand=qty,
                status=status,
                cost=float(product.cost or 0.0),
                price=float(product.price or 0.0),
                last_movement_at=last_movement_at,
            )
        )
    return result


@router.get("/recent-activity", response_model=schemas.InvestmentRecentActivityRead)
def get_investment_recent_activity(
    limit_sales: int = Query(default=12, ge=1, le=100),
    limit_movements: int = Query(default=12, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    sale_rows = (
        db.query(models.SaleItem, models.Sale, models.Product)
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.status == "active")
        .filter(models.Product.is_investment.is_(True))
        .order_by(models.Sale.created_at.desc(), models.SaleItem.id.desc())
        .limit(limit_sales)
        .all()
    )
    recent_sales: List[schemas.InvestmentRecentSaleRow] = []
    for sale_item, sale, product in sale_rows:
        quantity = float(sale_item.quantity or 0.0)
        unit_price = float(sale_item.unit_price or 0.0)
        unit_price_original = float(sale_item.unit_price_original or 0.0)
        base_unit = unit_price_original if unit_price_original > 0 else unit_price
        gross_line_total = float(base_unit * quantity)
        net_total = float(sale_item.total or 0.0)
        line_discount_value = float(sale_item.line_discount_value or max(gross_line_total - net_total, 0.0))
        line_cost_total = float(quantity * float(product.cost or 0.0))
        discount_percent = (
            float((line_discount_value / gross_line_total) * 100.0)
            if gross_line_total > 0
            else 0.0
        )
        recent_sales.append(
            schemas.InvestmentRecentSaleRow(
                sale_id=sale.id,
                sale_document_number=sale.document_number,
                sold_at=sale.created_at,
                product_id=sale_item.product_id,
                product_name=sale_item.product_name,
                quantity=quantity,
                unit_price=unit_price,
                gross_line_total=gross_line_total,
                line_discount_value=line_discount_value,
                discount_percent=discount_percent,
                line_cost_total=line_cost_total,
                net_total=net_total,
                pos_name=sale.pos_name,
                seller_name=sale.vendor_name,
            )
        )

    movement_rows = (
        db.query(models.InventoryMovement, models.Product.name)
        .join(models.Product, models.Product.id == models.InventoryMovement.product_id)
        .filter(models.InventoryMovement.tenant_id == tenant_id)
        .filter(models.Product.is_investment.is_(True))
        .order_by(models.InventoryMovement.created_at.desc(), models.InventoryMovement.id.desc())
        .limit(limit_movements)
        .all()
    )
    recent_movements = [
        schemas.InvestmentRecentMovementRow(
            movement_id=movement.id,
            product_id=movement.product_id,
            product_name=product_name,
            qty_delta=float(movement.qty_delta or 0.0),
            reason=movement.reason,
            notes=movement.notes,
            created_at=movement.created_at,
        )
        for movement, product_name in movement_rows
    ]

    return schemas.InvestmentRecentActivityRead(
        recent_sales=recent_sales,
        recent_movements=recent_movements,
    )


@router.get("/sales-lines", response_model=schemas.InvestmentSaleLinePage)
def list_investment_sales_lines(
    period_start: Optional[datetime] = Query(default=None),
    period_end: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None, min_length=1),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=300),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    if period_start and period_end and period_end <= period_start:
        raise HTTPException(status_code=400, detail="Rango de fechas inválido")

    base_query = (
        db.query(models.SaleItem, models.Sale, models.Product)
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.status == "active")
        .filter(models.Product.is_investment.is_(True))
    )
    if period_start is not None:
        base_query = base_query.filter(models.Sale.created_at >= period_start)
    if period_end is not None:
        base_query = base_query.filter(models.Sale.created_at < period_end)
    if search:
        term = f"%{search.strip()}%"
        base_query = base_query.filter(
            or_(
                models.SaleItem.product_name.ilike(term),
                models.SaleItem.product_sku.ilike(term),
                models.Sale.document_number.ilike(term),
                models.Sale.vendor_name.ilike(term),
                models.Sale.pos_name.ilike(term),
            )
        )

    total = int(base_query.count())
    totals_row = base_query.with_entities(
        func.coalesce(func.sum(models.SaleItem.quantity), 0).label("total_quantity"),
        func.coalesce(func.sum(models.SaleItem.line_discount_value), 0).label("total_discount"),
        func.coalesce(func.sum(models.SaleItem.total), 0).label("total_net"),
    ).first()

    rows = (
        base_query.order_by(models.Sale.created_at.desc(), models.SaleItem.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    items: List[schemas.InvestmentSaleLineRow] = []
    for sale_item, sale, product in rows:
        quantity = float(sale_item.quantity or 0.0)
        unit_price = float(sale_item.unit_price or 0.0)
        unit_price_original = float(sale_item.unit_price_original or 0.0)
        base_unit = unit_price_original if unit_price_original > 0 else unit_price
        gross_line_total = float(base_unit * quantity)
        net_total = float(sale_item.total or 0.0)
        line_discount_value = float(sale_item.line_discount_value or max(gross_line_total - net_total, 0.0))
        line_cost_total = float(quantity * float(product.cost or 0.0))
        discount_percent = (
            float((line_discount_value / gross_line_total) * 100.0)
            if gross_line_total > 0
            else 0.0
        )
        items.append(
            schemas.InvestmentSaleLineRow(
                sale_id=sale.id,
                sale_document_number=sale.document_number,
                sold_at=sale.created_at,
                product_id=sale_item.product_id,
                product_name=sale_item.product_name,
                quantity=quantity,
                unit_price=unit_price,
                gross_line_total=gross_line_total,
                line_discount_value=line_discount_value,
                discount_percent=discount_percent,
                line_cost_total=line_cost_total,
                net_total=net_total,
                pos_name=sale.pos_name,
                seller_name=sale.vendor_name,
            )
        )

    return schemas.InvestmentSaleLinePage(
        items=items,
        total=total,
        skip=skip,
        limit=limit,
        total_quantity=float(getattr(totals_row, "total_quantity", 0.0) or 0.0),
        total_discount=float(getattr(totals_row, "total_discount", 0.0) or 0.0),
        total_net=float(getattr(totals_row, "total_net", 0.0) or 0.0),
    )


@router.get("/sales-lines/export/xlsx")
def export_investment_sales_lines_xlsx(
    period_start: Optional[datetime] = Query(default=None),
    period_end: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None, min_length=1),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    if period_start and period_end and period_end <= period_start:
        raise HTTPException(status_code=400, detail="Rango de fechas inválido")

    query = (
        db.query(models.SaleItem, models.Sale, models.Product)
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.status == "active")
        .filter(models.Product.is_investment.is_(True))
    )
    if period_start is not None:
        query = query.filter(models.Sale.created_at >= period_start)
    if period_end is not None:
        query = query.filter(models.Sale.created_at < period_end)
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                models.SaleItem.product_name.ilike(term),
                models.SaleItem.product_sku.ilike(term),
                models.Sale.document_number.ilike(term),
                models.Sale.vendor_name.ilike(term),
                models.Sale.pos_name.ilike(term),
            )
        )
    rows = query.order_by(models.Sale.created_at.desc(), models.SaleItem.id.desc()).all()

    export_rows: List[Dict[str, object]] = []
    for sale_item, sale, product in rows:
        quantity = float(sale_item.quantity or 0.0)
        unit_price = float(sale_item.unit_price or 0.0)
        unit_price_original = float(sale_item.unit_price_original or 0.0)
        base_unit = unit_price_original if unit_price_original > 0 else unit_price
        gross_line_total = float(base_unit * quantity)
        net_total = float(sale_item.total or 0.0)
        line_discount_value = float(
            sale_item.line_discount_value or max(gross_line_total - net_total, 0.0)
        )
        line_cost_total = float(quantity * float(product.cost or 0.0))
        discount_percent = (
            float((line_discount_value / gross_line_total) * 100.0)
            if gross_line_total > 0
            else 0.0
        )
        export_rows.append(
            {
                "Fecha": sale.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "Documento": sale.document_number or f"#{sale.id}",
                "Producto": sale_item.product_name,
                "Cantidad": quantity,
                "Precio unitario": unit_price,
                "Bruto": gross_line_total,
                "Descuento": line_discount_value,
                "Descuento %": discount_percent,
                "Costo linea": line_cost_total,
                "Neto": net_total,
                "POS": sale.pos_name or "",
                "Vendedor": sale.vendor_name or "",
            }
        )
    if not export_rows:
        export_rows.append(
            {
                "Fecha": "",
                "Documento": "",
                "Producto": "",
                "Cantidad": 0,
                "Precio unitario": 0,
                "Bruto": 0,
                "Descuento": 0,
                "Descuento %": 0,
                "Costo linea": 0,
                "Neto": 0,
                "POS": "",
                "Vendedor": "",
            }
        )

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(export_rows).to_excel(writer, index=False, sheet_name="Registros")
    output.seek(0)
    filename = f"investment_registros_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sales-lines/export/pdf")
def export_investment_sales_lines_pdf(
    period_start: Optional[datetime] = Query(default=None),
    period_end: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None, min_length=1),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    if period_start and period_end and period_end <= period_start:
        raise HTTPException(status_code=400, detail="Rango de fechas inválido")

    query = (
        db.query(models.SaleItem, models.Sale, models.Product)
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.tenant_id == tenant_id)
        .filter(models.Sale.status == "active")
        .filter(models.Product.is_investment.is_(True))
    )
    if period_start is not None:
        query = query.filter(models.Sale.created_at >= period_start)
    if period_end is not None:
        query = query.filter(models.Sale.created_at < period_end)
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                models.SaleItem.product_name.ilike(term),
                models.SaleItem.product_sku.ilike(term),
                models.Sale.document_number.ilike(term),
                models.Sale.vendor_name.ilike(term),
                models.Sale.pos_name.ilike(term),
            )
        )
    rows = query.order_by(models.Sale.created_at.desc(), models.SaleItem.id.desc()).all()

    lines = [
        "Exportacion de registros de ventas (inversion)",
        f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Filas: {len(rows)}",
        "",
    ]
    for sale_item, sale, product in rows[:500]:
        quantity = float(sale_item.quantity or 0.0)
        unit_price = float(sale_item.unit_price or 0.0)
        unit_price_original = float(sale_item.unit_price_original or 0.0)
        base_unit = unit_price_original if unit_price_original > 0 else unit_price
        gross_line_total = float(base_unit * quantity)
        net_total = float(sale_item.total or 0.0)
        line_discount_value = float(
            sale_item.line_discount_value or max(gross_line_total - net_total, 0.0)
        )
        lines.append(
            f"{sale.created_at.strftime('%Y-%m-%d %H:%M')} | "
            f"{sale.document_number or f'#{sale.id}'} | {sale_item.product_name} | "
            f"cant={quantity:.2f} | desc={line_discount_value:.2f} | neto={net_total:.2f}"
        )
    if len(rows) > 500:
        lines.append(f"... ({len(rows) - 500} filas adicionales no incluidas en PDF simple)")
    if not rows:
        lines.append("Sin datos para exportar.")

    pdf_bytes = pdf_utils.build_simple_pdf("Registros Inversion", lines)
    filename = f"investment_registros_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/participants", response_model=List[schemas.InvestmentParticipantRead])
def list_investment_participants(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    rows = (
        db.query(models.InvestmentParticipant)
        .filter(models.InvestmentParticipant.tenant_id == tenant_id)
        .order_by(
            models.InvestmentParticipant.is_active.desc(),
            models.InvestmentParticipant.created_at.asc(),
        )
        .all()
    )
    return [_participant_read(row) for row in rows]


@router.put("/participants", response_model=List[schemas.InvestmentParticipantRead])
def replace_investment_participants(
    payload: schemas.InvestmentParticipantsReplaceRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    existing = (
        db.query(models.InvestmentParticipant)
        .filter(models.InvestmentParticipant.tenant_id == tenant_id)
        .all()
    )
    existing_map: Dict[Tuple[Optional[int], str], models.InvestmentParticipant] = {}
    for row in existing:
        key = (
            int(row.user_id) if row.user_id is not None else None,
            (row.display_name or "").strip().lower(),
        )
        existing_map[key] = row

    touched_keys: set[Tuple[Optional[int], str]] = set()
    for item in payload.items:
        normalized_name = item.display_name.strip()
        if not normalized_name:
            raise HTTPException(status_code=400, detail="Cada participante debe tener nombre")
        key = (
            int(item.user_id) if item.user_id is not None else None,
            normalized_name.lower(),
        )
        touched_keys.add(key)
        model = existing_map.get(key)
        if model:
            model.display_name = normalized_name
            profit_share = float(item.profit_share_percent or item.share_percent or 0.0)
            capital_share = float(item.capital_share_percent or 0.0)
            model.share_percent = profit_share
            model.profit_share_percent = profit_share
            model.capital_share_percent = capital_share
            model.is_active = bool(item.is_active)
            model.user_id = item.user_id
        else:
            profit_share = float(item.profit_share_percent or item.share_percent or 0.0)
            capital_share = float(item.capital_share_percent or 0.0)
            db.add(
                models.InvestmentParticipant(
                    tenant_id=tenant_id,
                    user_id=item.user_id,
                    display_name=normalized_name,
                    share_percent=profit_share,
                    profit_share_percent=profit_share,
                    capital_share_percent=capital_share,
                    is_active=bool(item.is_active),
                )
            )

    for row in existing:
        key = (
            int(row.user_id) if row.user_id is not None else None,
            (row.display_name or "").strip().lower(),
        )
        if key not in touched_keys:
            row.is_active = False

    db.commit()
    rows = (
        db.query(models.InvestmentParticipant)
        .filter(models.InvestmentParticipant.tenant_id == tenant_id)
        .order_by(
            models.InvestmentParticipant.is_active.desc(),
            models.InvestmentParticipant.created_at.asc(),
        )
        .all()
    )
    return [_participant_read(row) for row in rows]


@router.post("/cuts/preview", response_model=schemas.InvestmentCutRead)
def preview_investment_cut(
    payload: schemas.InvestmentCutPreviewRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    gross_sales, collected_sales, cogs, profit_base = _compute_cut_metrics(
        db,
        tenant_id=tenant_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
    )
    participants = _active_participants(db, tenant_id=tenant_id)
    allocations = _build_cut_allocations(
        participants=participants,
        cogs=float(cogs),
        profit_base=float(profit_base),
    )
    return schemas.InvestmentCutRead(
        id=0,
        period_start=payload.period_start,
        period_end=payload.period_end,
        gross_sales=float(gross_sales),
        collected_sales=float(collected_sales),
        cogs=float(cogs),
        profit_base=float(profit_base),
        notes=None,
        reconciled=False,
        reconciled_at=None,
        reconciled_by_user_id=None,
        created_at=datetime.utcnow(),
        allocations=allocations,
    )


@router.post("/cuts", response_model=schemas.InvestmentCutRead, status_code=201)
def create_investment_cut(
    payload: schemas.InvestmentCutCreateRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    gross_sales, collected_sales, cogs, profit_base = _compute_cut_metrics(
        db,
        tenant_id=tenant_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
    )
    existing_cut = (
        db.query(models.InvestmentCut.id)
        .filter(models.InvestmentCut.tenant_id == tenant_id)
        .filter(models.InvestmentCut.period_start == payload.period_start)
        .filter(models.InvestmentCut.period_end == payload.period_end)
        .first()
    )
    if existing_cut:
        raise HTTPException(status_code=400, detail="Ya existe un corte para ese período.")

    participants = _active_participants(db, tenant_id=tenant_id)
    allocations = _build_cut_allocations(
        participants=participants,
        cogs=float(cogs),
        profit_base=float(profit_base),
    )
    if not allocations:
        raise HTTPException(
            status_code=400,
            detail="No hay porcentajes válidos de utilidad/capital para generar el corte.",
        )

    cut = models.InvestmentCut(
        tenant_id=tenant_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        gross_sales=float(gross_sales),
        collected_sales=float(collected_sales),
        cogs=float(cogs),
        profit_base=float(profit_base),
        notes=(payload.notes or None),
        created_by_user_id=current_user.id,
    )
    db.add(cut)
    db.flush()

    for allocation in allocations:
        db.add(
            models.InvestmentCutAllocation(
                tenant_id=tenant_id,
                cut_id=cut.id,
                participant_id=allocation.participant_id,
                share_percent=allocation.share_percent,
                profit_share_percent=allocation.profit_share_percent,
                capital_share_percent=allocation.capital_share_percent,
                profit_amount=allocation.profit_amount,
                capital_amount=allocation.capital_amount,
                amount_due=allocation.amount_due,
            )
        )
    db.commit()
    db.refresh(cut)
    return schemas.InvestmentCutRead(
        id=cut.id,
        period_start=cut.period_start,
        period_end=cut.period_end,
        gross_sales=float(cut.gross_sales or 0.0),
        collected_sales=float(cut.collected_sales or 0.0),
        cogs=float(cut.cogs or 0.0),
        profit_base=float(cut.profit_base or 0.0),
        notes=cut.notes,
        reconciled=bool(cut.reconciled),
        reconciled_at=cut.reconciled_at,
        reconciled_by_user_id=cut.reconciled_by_user_id,
        created_at=cut.created_at,
        allocations=allocations,
    )


@router.get("/cuts", response_model=List[schemas.InvestmentCutRead])
def list_investment_cuts(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    _ensure_automatic_quincenal_cuts(db, tenant_id=tenant_id, as_of=datetime.utcnow())
    cuts = (
        db.query(models.InvestmentCut)
        .filter(models.InvestmentCut.tenant_id == tenant_id)
        .order_by(models.InvestmentCut.period_start.desc(), models.InvestmentCut.id.desc())
        .limit(limit)
        .all()
    )
    result: List[schemas.InvestmentCutRead] = []
    for cut in cuts:
        allocs = (
            db.query(models.InvestmentCutAllocation, models.InvestmentParticipant.display_name)
            .join(
                models.InvestmentParticipant,
                models.InvestmentParticipant.id == models.InvestmentCutAllocation.participant_id,
            )
            .filter(models.InvestmentCutAllocation.cut_id == cut.id)
            .order_by(models.InvestmentCutAllocation.id.asc())
            .all()
        )
        allocations = [
            schemas.InvestmentCutAllocationRead(
                participant_id=allocation.participant_id,
                participant_name=participant_name,
                share_percent=float(allocation.share_percent or 0.0),
                profit_share_percent=float(
                    allocation.profit_share_percent or allocation.share_percent or 0.0
                ),
                capital_share_percent=float(allocation.capital_share_percent or 0.0),
                profit_amount=float(allocation.profit_amount or 0.0),
                capital_amount=float(allocation.capital_amount or 0.0),
                amount_due=float(allocation.amount_due or 0.0),
            )
            for allocation, participant_name in allocs
        ]
        result.append(
            schemas.InvestmentCutRead(
                id=cut.id,
                period_start=cut.period_start,
                period_end=cut.period_end,
                gross_sales=float(cut.gross_sales or 0.0),
                collected_sales=float(cut.collected_sales or 0.0),
                cogs=float(cut.cogs or 0.0),
                profit_base=float(cut.profit_base or 0.0),
                notes=cut.notes,
                reconciled=bool(cut.reconciled),
                reconciled_at=cut.reconciled_at,
                reconciled_by_user_id=cut.reconciled_by_user_id,
                created_at=cut.created_at,
                allocations=allocations,
            )
        )
    return result


@router.post("/cuts/{cut_id}/reconcile", response_model=schemas.InvestmentCutRead)
def reconcile_investment_cut(
    cut_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    cut = (
        db.query(models.InvestmentCut)
        .filter(models.InvestmentCut.tenant_id == tenant_id)
        .filter(models.InvestmentCut.id == cut_id)
        .first()
    )
    if not cut:
        raise HTTPException(status_code=404, detail="Corte no encontrado")
    if cut.reconciled:
        raise HTTPException(status_code=400, detail="Este corte ya está conciliado.")

    due_total = (
        db.query(func.coalesce(func.sum(models.InvestmentCutAllocation.amount_due), 0))
        .filter(models.InvestmentCutAllocation.cut_id == cut.id)
        .scalar()
    ) or 0
    paid_total = (
        db.query(func.coalesce(func.sum(models.InvestmentPayout.amount), 0))
        .filter(models.InvestmentPayout.tenant_id == tenant_id)
        .filter(models.InvestmentPayout.cut_id == cut.id)
        .scalar()
    ) or 0
    pending_total = float(due_total or 0) - float(paid_total or 0)
    if pending_total > 0.009:
        raise HTTPException(
            status_code=400,
            detail="No se puede conciliar: el corte aún tiene saldo pendiente.",
        )

    cut.reconciled = True
    cut.reconciled_at = datetime.utcnow()
    cut.reconciled_by_user_id = current_user.id
    db.commit()
    db.refresh(cut)

    allocs = (
        db.query(models.InvestmentCutAllocation, models.InvestmentParticipant.display_name)
        .join(
            models.InvestmentParticipant,
            models.InvestmentParticipant.id == models.InvestmentCutAllocation.participant_id,
        )
        .filter(models.InvestmentCutAllocation.cut_id == cut.id)
        .order_by(models.InvestmentCutAllocation.id.asc())
        .all()
    )
    allocations = [
        schemas.InvestmentCutAllocationRead(
            participant_id=allocation.participant_id,
            participant_name=participant_name,
            share_percent=float(allocation.share_percent or 0.0),
            profit_share_percent=float(allocation.profit_share_percent or allocation.share_percent or 0.0),
            capital_share_percent=float(allocation.capital_share_percent or 0.0),
            profit_amount=float(allocation.profit_amount or 0.0),
            capital_amount=float(allocation.capital_amount or 0.0),
            amount_due=float(allocation.amount_due or 0.0),
        )
        for allocation, participant_name in allocs
    ]
    return schemas.InvestmentCutRead(
        id=cut.id,
        period_start=cut.period_start,
        period_end=cut.period_end,
        gross_sales=float(cut.gross_sales or 0.0),
        collected_sales=float(cut.collected_sales or 0.0),
        cogs=float(cut.cogs or 0.0),
        profit_base=float(cut.profit_base or 0.0),
        notes=cut.notes,
        reconciled=bool(cut.reconciled),
        reconciled_at=cut.reconciled_at,
        reconciled_by_user_id=cut.reconciled_by_user_id,
        created_at=cut.created_at,
        allocations=allocations,
    )


@router.post("/payouts", response_model=schemas.InvestmentPayoutRead, status_code=201)
def create_investment_payout(
    payload: schemas.InvestmentPayoutCreateRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    participant = (
        db.query(models.InvestmentParticipant)
        .filter(models.InvestmentParticipant.tenant_id == tenant_id)
        .filter(models.InvestmentParticipant.id == payload.participant_id)
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Participante no encontrado")
    if payload.cut_id is not None:
        cut = (
            db.query(models.InvestmentCut)
            .filter(models.InvestmentCut.tenant_id == tenant_id)
            .filter(models.InvestmentCut.id == payload.cut_id)
            .first()
        )
        if not cut:
            raise HTTPException(status_code=404, detail="Corte no encontrado")
        if cut.reconciled:
            raise HTTPException(status_code=400, detail="No se pueden registrar pagos en un corte conciliado.")

    payout = models.InvestmentPayout(
        tenant_id=tenant_id,
        participant_id=participant.id,
        cut_id=payload.cut_id,
        amount=float(payload.amount or 0.0),
        paid_at=payload.paid_at or datetime.utcnow(),
        method=(payload.method or None),
        reference=(payload.reference or None),
        notes=(payload.notes or None),
        created_by_user_id=current_user.id,
    )
    db.add(payout)
    db.commit()
    db.refresh(payout)
    return schemas.InvestmentPayoutRead(
        id=payout.id,
        participant_id=participant.id,
        participant_name=participant.display_name,
        cut_id=payout.cut_id,
        amount=float(payout.amount or 0.0),
        paid_at=payout.paid_at,
        method=payout.method,
        reference=payout.reference,
        notes=payout.notes,
        created_at=payout.created_at,
    )


@router.get("/payouts", response_model=List[schemas.InvestmentPayoutRead])
def list_investment_payouts(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    rows = (
        db.query(models.InvestmentPayout, models.InvestmentParticipant.display_name)
        .join(
            models.InvestmentParticipant,
            models.InvestmentParticipant.id == models.InvestmentPayout.participant_id,
        )
        .filter(models.InvestmentPayout.tenant_id == tenant_id)
        .order_by(models.InvestmentPayout.paid_at.desc(), models.InvestmentPayout.id.desc())
        .limit(limit)
        .all()
    )
    return [
        schemas.InvestmentPayoutRead(
            id=payout.id,
            participant_id=payout.participant_id,
            participant_name=participant_name,
            cut_id=payout.cut_id,
            amount=float(payout.amount or 0.0),
            paid_at=payout.paid_at,
            method=payout.method,
            reference=payout.reference,
            notes=payout.notes,
            created_at=payout.created_at,
        )
        for payout, participant_name in rows
    ]


@router.get("/payouts/export/xlsx")
def export_investment_payouts_xlsx(
    period_start: Optional[datetime] = Query(default=None),
    period_end: Optional[datetime] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    query = (
        db.query(models.InvestmentPayout, models.InvestmentParticipant.display_name)
        .join(
            models.InvestmentParticipant,
            models.InvestmentParticipant.id == models.InvestmentPayout.participant_id,
        )
        .filter(models.InvestmentPayout.tenant_id == tenant_id)
    )
    if period_start is not None:
        query = query.filter(models.InvestmentPayout.paid_at >= period_start)
    if period_end is not None:
        query = query.filter(models.InvestmentPayout.paid_at < period_end)
    rows = query.order_by(models.InvestmentPayout.paid_at.desc(), models.InvestmentPayout.id.desc()).all()

    export_rows: List[Dict[str, object]] = []
    for payout, participant_name in rows:
        participant_lower = (participant_name or "").strip().lower()
        recipient = "Papá" if ("papa" in participant_lower or "papá" in participant_lower) else "Ken+Sar"
        export_rows.append(
            {
                "Fecha": payout.paid_at.strftime("%Y-%m-%d %H:%M:%S"),
                "Destinatario": recipient,
                "Participante": participant_name,
                "Corte": f"#{payout.cut_id}" if payout.cut_id else "—",
                "Monto": float(payout.amount or 0.0),
                "Metodo": payout.method or "",
                "Referencia": payout.reference or "",
                "Notas": payout.notes or "",
            }
        )
    if not export_rows:
        export_rows.append(
            {
                "Fecha": "",
                "Destinatario": "",
                "Participante": "",
                "Corte": "",
                "Monto": 0,
                "Metodo": "",
                "Referencia": "",
                "Notas": "",
            }
        )

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(export_rows).to_excel(writer, index=False, sheet_name="Transferencias")
    output.seek(0)
    filename = f"investment_transferencias_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/payouts/export/pdf")
def export_investment_payouts_pdf(
    period_start: Optional[datetime] = Query(default=None),
    period_end: Optional[datetime] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    query = (
        db.query(models.InvestmentPayout, models.InvestmentParticipant.display_name)
        .join(
            models.InvestmentParticipant,
            models.InvestmentParticipant.id == models.InvestmentPayout.participant_id,
        )
        .filter(models.InvestmentPayout.tenant_id == tenant_id)
    )
    if period_start is not None:
        query = query.filter(models.InvestmentPayout.paid_at >= period_start)
    if period_end is not None:
        query = query.filter(models.InvestmentPayout.paid_at < period_end)
    rows = query.order_by(models.InvestmentPayout.paid_at.desc(), models.InvestmentPayout.id.desc()).all()

    lines = [
        "Exportacion de transferencias registradas (inversion)",
        f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Filas: {len(rows)}",
        "",
    ]
    for payout, participant_name in rows[:500]:
        participant_lower = (participant_name or "").strip().lower()
        recipient = "Papá" if ("papa" in participant_lower or "papá" in participant_lower) else "Ken+Sar"
        lines.append(
            f"{payout.paid_at.strftime('%Y-%m-%d %H:%M')} | {recipient} | {participant_name} | "
            f"corte={f'#{payout.cut_id}' if payout.cut_id else '—'} | monto={float(payout.amount or 0.0):.2f} | "
            f"metodo={payout.method or '—'} | ref={payout.reference or '—'}"
        )
    if len(rows) > 500:
        lines.append(f"... ({len(rows) - 500} filas adicionales no incluidas en PDF simple)")
    if not rows:
        lines.append("Sin datos para exportar.")

    pdf_bytes = pdf_utils.build_simple_pdf("Transferencias Inversion", lines)
    filename = f"investment_transferencias_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ledger", response_model=schemas.InvestmentLedgerRead)
def get_investment_ledger(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_module_access("investment")),
):
    tenant_id = _resolve_tenant_id(db, current_user)
    _ensure_automatic_quincenal_cuts(db, tenant_id=tenant_id, as_of=datetime.utcnow())
    participants = (
        db.query(models.InvestmentParticipant)
        .filter(models.InvestmentParticipant.tenant_id == tenant_id)
        .all()
    )
    due_rows = (
        db.query(
            models.InvestmentCutAllocation.participant_id.label("participant_id"),
            func.coalesce(func.sum(models.InvestmentCutAllocation.amount_due), 0).label("due_total"),
        )
        .filter(models.InvestmentCutAllocation.tenant_id == tenant_id)
        .group_by(models.InvestmentCutAllocation.participant_id)
        .all()
    )
    paid_rows = (
        db.query(
            models.InvestmentPayout.participant_id.label("participant_id"),
            func.coalesce(func.sum(models.InvestmentPayout.amount), 0).label("paid_total"),
        )
        .filter(models.InvestmentPayout.tenant_id == tenant_id)
        .group_by(models.InvestmentPayout.participant_id)
        .all()
    )
    due_map = {int(row.participant_id): float(row.due_total or 0.0) for row in due_rows}
    paid_map = {int(row.participant_id): float(row.paid_total or 0.0) for row in paid_rows}

    ledger_rows: List[schemas.InvestmentLedgerRow] = []
    for participant in participants:
        due_total = due_map.get(participant.id, 0.0)
        paid_total = paid_map.get(participant.id, 0.0)
        ledger_rows.append(
            schemas.InvestmentLedgerRow(
                participant_id=participant.id,
                participant_name=participant.display_name,
                due_total=due_total,
                paid_total=paid_total,
                balance=due_total - paid_total,
            )
        )
    due_total = float(sum(row.due_total for row in ledger_rows))
    paid_total = float(sum(row.paid_total for row in ledger_rows))
    return schemas.InvestmentLedgerRead(
        rows=ledger_rows,
        due_total=due_total,
        paid_total=paid_total,
        balance_total=due_total - paid_total,
    )
