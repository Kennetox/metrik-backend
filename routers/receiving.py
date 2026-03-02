from datetime import datetime, timezone
from typing import List
import math

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission
from services import storage


router = APIRouter(
    prefix="/receiving",
    tags=["receiving"],
)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_iso_datetime(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} inválido. Usa formato ISO 8601.",
        ) from exc


def _validate_invoice_requirements(
    purchase_type: schemas.PurchaseType,
    supplier_name: str | None,
    reference: str | None,
) -> None:
    if purchase_type != "invoice":
        return
    if not supplier_name:
        raise HTTPException(
            status_code=422,
            detail="Para compras con factura, el proveedor es obligatorio.",
        )
    if not reference:
        raise HTTPException(
            status_code=422,
            detail="Para compras con factura, la referencia/número de factura es obligatoria.",
        )


@router.post("/lots", response_model=schemas.ReceivingLotRead, status_code=201)
def create_receiving_lot(
    payload: schemas.ReceivingLotCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    supplier_name = _normalize_optional_text(payload.supplier_name)
    invoice_reference = _normalize_optional_text(payload.invoice_reference)
    _validate_invoice_requirements(
        payload.purchase_type,
        supplier_name,
        invoice_reference or _normalize_optional_text(payload.source_reference),
    )
    payload.supplier_name = supplier_name
    payload.invoice_reference = invoice_reference
    payload.source_reference = _normalize_optional_text(payload.source_reference) or invoice_reference

    lot = crud.create_receiving_lot(
        db,
        payload,
        created_by_user_id=current_user.id,
    )
    return lot


@router.get("/lots", response_model=schemas.ReceivingLotPage)
def list_receiving_lots(
    status: schemas.ReceivingLotStatus | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.view")),
):
    items = crud.list_receiving_lots(
        db,
        status=status,
        skip=skip,
        limit=limit,
    )
    total = crud.count_receiving_lots(db, status=status)
    return schemas.ReceivingLotPage(
        items=items,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/products/search", response_model=List[schemas.ReceivingProductLookup])
def search_receiving_products(
    q: str = Query(default="", min_length=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.view")),
):
    products = crud.search_receiving_products(db, q=q, limit=limit)
    return [schemas.ReceivingProductLookup.model_validate(product) for product in products]


@router.get("/product-groups", response_model=List[schemas.ReceivingProductGroupOption])
def list_receiving_product_groups(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.view")),
):
    configured_groups = crud.list_product_groups(db, skip=0, limit=5000)
    options_by_path: dict[str, schemas.ReceivingProductGroupOption] = {}

    for group in configured_groups:
        path = (group.path or "").strip()
        if not path:
            continue
        options_by_path[path] = schemas.ReceivingProductGroupOption(
            path=path,
            display_name=(group.display_name or path),
            parent_path=group.parent_path,
        )

    product_group_rows = (
        db.query(models.Product.group_name)
        .filter(models.Product.group_name.isnot(None))
        .all()
    )
    for (raw_group_name,) in product_group_rows:
        raw_path = (raw_group_name or "").strip()
        if not raw_path:
            continue
        parts = [segment.strip() for segment in raw_path.split("/") if segment.strip()]
        if not parts:
            continue
        for idx in range(len(parts)):
            path = "/".join(parts[: idx + 1])
            if path in options_by_path:
                continue
            parent_path = "/".join(parts[:idx]) if idx > 0 else None
            options_by_path[path] = schemas.ReceivingProductGroupOption(
                path=path,
                display_name=parts[idx],
                parent_path=parent_path or None,
            )

    ordered = sorted(options_by_path.values(), key=lambda row: row.path.lower())
    return ordered[skip : skip + limit]


@router.get("/products/next-codes", response_model=schemas.ReceivingProductCodePreview)
def get_receiving_product_next_codes(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    sku, barcode = crud.get_next_product_codes(db)
    return schemas.ReceivingProductCodePreview(sku=sku, barcode=barcode)


@router.post("/products/quick-create", response_model=schemas.ReceivingProductLookup, status_code=201)
def quick_create_receiving_product(
    payload: schemas.ReceivingProductQuickCreate,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("movements.manage")),
):
    try:
        product = crud.create_receiving_product_quick(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    crud.create_product_audit_log(
        db,
        product_id=product.id,
        action="create",
        actor_user=actor,
        changes={
            "source": "metrik_stock_app",
            "after": {
                "id": product.id,
                "sku": product.sku,
                "barcode": product.barcode,
                "name": product.name,
                "price": product.price,
                "cost": product.cost,
            },
        },
    )
    return schemas.ReceivingProductLookup.model_validate(product)


@router.post("/lots/{lot_id}/items", response_model=schemas.ReceivingLotItemRead, status_code=201)
def add_receiving_lot_item(
    lot_id: int,
    payload: schemas.ReceivingLotItemCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes editar lotes abiertos")

    product = crud.get_product(db, payload.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    item = crud.add_receiving_lot_item(
        db,
        lot=lot,
        product=product,
        qty_received=payload.qty_received,
        unit_cost=payload.unit_cost,
        notes=payload.notes,
    )
    return schemas.ReceivingLotItemRead.model_validate(item)


@router.patch("/lots/{lot_id}/items/{item_id}", response_model=schemas.ReceivingLotItemRead)
def update_receiving_lot_item(
    lot_id: int,
    item_id: int,
    payload: schemas.ReceivingLotItemUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes editar lotes abiertos")

    item = crud.get_receiving_lot_item(db, lot_id=lot_id, item_id=item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Ítem no encontrado")

    updated = crud.update_receiving_lot_item(
        db,
        item=item,
        qty_received=payload.qty_received,
        unit_cost=payload.unit_cost,
        notes=payload.notes,
    )
    return schemas.ReceivingLotItemRead.model_validate(updated)


@router.delete("/lots/{lot_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_receiving_lot_item(
    lot_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes editar lotes abiertos")

    item = crud.get_receiving_lot_item(db, lot_id=lot_id, item_id=item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Ítem no encontrado")

    crud.delete_receiving_lot_item(db, item)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/lots/{lot_id}/close", response_model=schemas.ReceivingLotRead)
def close_receiving_lot(
    lot_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="El lote ya está cerrado o cancelado")

    items = crud.list_receiving_lot_items(db, lot_id=lot_id)
    if len(items) == 0:
        raise HTTPException(status_code=409, detail="No puedes cerrar un lote sin ítems")
    _validate_invoice_requirements(
        lot.purchase_type,
        _normalize_optional_text(lot.supplier_name),
        _normalize_optional_text(lot.invoice_reference or lot.source_reference),
    )

    closed = crud.close_receiving_lot(db, lot=lot, closed_by_user_id=current_user.id)
    return schemas.ReceivingLotRead.model_validate(closed)


@router.patch("/lots/{lot_id}", response_model=schemas.ReceivingLotRead)
def update_receiving_lot(
    lot_id: int,
    payload: schemas.ReceivingLotUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes editar lotes abiertos")

    supplier_name = _normalize_optional_text(payload.supplier_name)
    invoice_reference = _normalize_optional_text(payload.invoice_reference)
    source_reference = _normalize_optional_text(payload.source_reference) or invoice_reference
    _validate_invoice_requirements(payload.purchase_type, supplier_name, invoice_reference or source_reference)

    updated = crud.update_receiving_lot(
        db,
        lot=lot,
        purchase_type=payload.purchase_type,
        supplier_name=supplier_name,
        invoice_reference=invoice_reference,
        source_reference=source_reference,
        notes=payload.notes,
    )
    return schemas.ReceivingLotRead.model_validate(updated)


@router.post("/lots/{lot_id}/cancel", response_model=schemas.ReceivingLotRead)
def cancel_receiving_lot(
    lot_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes cancelar lotes abiertos")

    cancelled = crud.cancel_receiving_lot(db, lot)
    return schemas.ReceivingLotRead.model_validate(cancelled)


@router.post("/lots/{lot_id}/support-file", response_model=schemas.ReceivingLotRead)
async def upload_receiving_lot_support_file(
    lot_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.manage")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lot.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes adjuntar soporte en lotes abiertos")

    try:
        stored = await storage.save_receiving_support_file(file, lot_id=lot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = crud.update_receiving_lot_support_file(
        db,
        lot,
        support_file_name=stored.filename,
        support_file_url=stored.url,
        support_file_size=stored.size,
    )
    return schemas.ReceivingLotRead.model_validate(updated)


@router.get("/lots/{lot_id}", response_model=schemas.ReceivingLotDetail)
def get_receiving_lot(
    lot_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.view")),
):
    lot = crud.get_receiving_lot(db, lot_id)
    if not lot:
        raise HTTPException(status_code=404, detail="Lote no encontrado")

    items: List[models.ReceivingLotItem] = crud.list_receiving_lot_items(db, lot_id)
    pending_labels = sum(max(0, int(math.ceil(float(item.qty_received or 0)))) for item in items)
    warnings: List[schemas.ApiWarning] = []

    missing_barcode_count = sum(1 for item in items if not (item.barcode_snapshot or "").strip())
    if missing_barcode_count > 0:
        warnings.append(
            schemas.ApiWarning(
                code="missing_barcode",
                message=f"{missing_barcode_count} ítem(s) no tienen código de barras en snapshot.",
            )
        )

    invalid_price_count = sum(1 for item in items if float(item.unit_price_snapshot or 0) <= 0)
    if invalid_price_count > 0:
        warnings.append(
            schemas.ApiWarning(
                code="invalid_price",
                message=f"{invalid_price_count} ítem(s) tienen precio de venta en 0 o inválido.",
            )
        )

    if pending_labels > 0:
        warnings.append(
            schemas.ApiWarning(
                code="labels_pending",
                message=f"Hay {pending_labels} etiqueta(s) pendientes por procesar.",
            )
        )

    return schemas.ReceivingLotDetail(
        lot=schemas.ReceivingLotRead.model_validate(lot),
        items=[schemas.ReceivingLotItemRead.model_validate(item) for item in items],
        labels_summary=schemas.ReceivingLabelsSummary(pending=pending_labels, printed=0, error=0),
        warnings=warnings,
    )


@router.get("/documents", response_model=schemas.ReceivingDocumentPage)
def list_receiving_documents(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.view")),
):
    parsed_date_from = _parse_iso_datetime(date_from, "date_from")
    parsed_date_to = _parse_iso_datetime(date_to, "date_to")
    query = (
        db.query(
            models.ReceivingLot.id.label("id"),
            models.ReceivingLot.lot_number.label("lot_number"),
            models.ReceivingLot.status.label("status"),
            models.ReceivingLot.purchase_type.label("purchase_type"),
            models.ReceivingLot.origin_name.label("origin_name"),
            models.ReceivingLot.created_at.label("created_at"),
            models.ReceivingLot.closed_at.label("closed_at"),
            models.ReceivingLot.supplier_name.label("supplier_name"),
            models.ReceivingLot.invoice_reference.label("invoice_reference"),
            models.ReceivingLot.support_file_name.label("support_file_name"),
            models.ReceivingLot.support_file_url.label("support_file_url"),
            models.ReceivingLot.support_file_size.label("support_file_size"),
            models.PosUser.name.label("closed_by_user_name"),
            func.count(models.ReceivingLotItem.id).label("lines_count"),
            func.coalesce(func.sum(models.ReceivingLotItem.qty_received), 0.0).label("units_total"),
        )
        .outerjoin(models.ReceivingLotItem, models.ReceivingLotItem.lot_id == models.ReceivingLot.id)
        .outerjoin(models.PosUser, models.PosUser.id == models.ReceivingLot.closed_by_user_id)
        .filter(models.ReceivingLot.status == "closed")
    )

    if parsed_date_from:
        query = query.filter(models.ReceivingLot.closed_at >= parsed_date_from)
    if parsed_date_to:
        query = query.filter(models.ReceivingLot.closed_at <= parsed_date_to)

    grouped = query.group_by(
        models.ReceivingLot.id,
        models.ReceivingLot.lot_number,
        models.ReceivingLot.status,
        models.ReceivingLot.purchase_type,
        models.ReceivingLot.origin_name,
        models.ReceivingLot.created_at,
        models.ReceivingLot.closed_at,
        models.ReceivingLot.supplier_name,
        models.ReceivingLot.invoice_reference,
        models.ReceivingLot.support_file_name,
        models.ReceivingLot.support_file_url,
        models.ReceivingLot.support_file_size,
        models.PosUser.name,
    )

    total = grouped.count()
    rows = (
        grouped
        .order_by(models.ReceivingLot.closed_at.desc(), models.ReceivingLot.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    items = [
        schemas.ReceivingDocumentRead(
            id=row.id,
            lot_number=row.lot_number,
            status=row.status,
            purchase_type=row.purchase_type,
            origin_name=row.origin_name,
            lines_count=int(row.lines_count or 0),
            units_total=float(row.units_total or 0.0),
            created_at=row.created_at,
            closed_at=row.closed_at,
            closed_by_user_name=row.closed_by_user_name,
            supplier_name=row.supplier_name,
            invoice_reference=row.invoice_reference,
            support_file_name=row.support_file_name,
            support_file_url=row.support_file_url,
            support_file_size=row.support_file_size,
        )
        for row in rows
    ]
    return schemas.ReceivingDocumentPage(items=items, total=total, skip=skip, limit=limit)


@router.get("/products/created", response_model=schemas.ReceivingCreatedProductPage)
def list_receiving_created_products(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("movements.view")),
):
    source_pattern_legacy = '%"source": "receiving_quick_create"%'
    source_pattern_app = '%"source": "metrik_stock_app"%'
    query = (
        db.query(
            models.ProductAuditLog.id.label("audit_id"),
            models.Product.id.label("product_id"),
            models.Product.name.label("name"),
            models.Product.sku.label("sku"),
            models.Product.barcode.label("barcode"),
            models.Product.price.label("price"),
            models.Product.cost.label("cost"),
            models.Product.group_name.label("group_name"),
            models.ProductAuditLog.created_at.label("created_at"),
            models.ProductAuditLog.actor_name.label("created_by_user_name"),
        )
        .join(models.Product, models.Product.id == models.ProductAuditLog.product_id)
        .filter(models.ProductAuditLog.action == "create")
        .filter(
            or_(
                cast(models.ProductAuditLog.changes, String).ilike(source_pattern_legacy),
                cast(models.ProductAuditLog.changes, String).ilike(source_pattern_app),
            )
        )
    )

    total = query.count()
    rows = (
        query.order_by(models.ProductAuditLog.created_at.desc(), models.ProductAuditLog.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    items = [
        schemas.ReceivingCreatedProductRead(
            audit_id=row.audit_id,
            product_id=row.product_id,
            name=row.name,
            sku=row.sku,
            barcode=row.barcode,
            price=float(row.price or 0),
            cost=float(row.cost or 0),
            group_name=row.group_name,
            created_at=row.created_at,
            created_by_user_name=row.created_by_user_name,
        )
        for row in rows
    ]
    return schemas.ReceivingCreatedProductPage(items=items, total=total, skip=skip, limit=limit)
