from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import case, func
from typing import Any, Dict, List, Optional
from io import BytesIO
from fastapi.responses import StreamingResponse, PlainTextResponse
import io
import csv
import pandas as pd
from pydantic import BaseModel

import schemas, crud, models
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/products",
    tags=["products"]
)


class ExportProductsRequest(BaseModel):
    scope: str = "all"
    search: Optional[str] = None
    show_only_active: bool = False
    group: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    price_min: Optional[str] = None
    price_max: Optional[str] = None
    columns: List[str] = []
    file_name: Optional[str] = None


def _parse_price(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _filter_products(products: List[models.Product], payload: ExportProductsRequest):
    if payload.scope != "filtered":
        return products
    term = (payload.search or "").strip().lower()
    min_price = _parse_price(payload.price_min)
    max_price = _parse_price(payload.price_max)
    group_filter = (payload.group or "").strip()
    brand_filter = (payload.brand or "").strip()
    supplier_filter = (payload.supplier or "").strip()

    filtered: List[models.Product] = []
    for p in products:
        if term:
            haystack = " ".join(
                [
                    p.name or "",
                    p.sku or "",
                    p.barcode or "",
                    p.group_name or "",
                    p.brand or "",
                    p.supplier or "",
                ]
            ).lower()
            if term not in haystack:
                continue

        if payload.show_only_active and not p.active:
            continue
        if group_filter and (p.group_name or "") != group_filter:
            continue
        if brand_filter and (p.brand or "") != brand_filter:
            continue
        if supplier_filter and (p.supplier or "") != supplier_filter:
            continue
        if min_price is not None and p.price < min_price:
            continue
        if max_price is not None and p.price > max_price:
            continue

        filtered.append(p)

    return filtered


def _build_export_rows(
    db: Session,
    products: List[models.Product],
    columns: List[str],
):
    history_map: Dict[int, Dict[str, Optional[str]]] = {}
    include_history = "history" in columns if columns else False
    if include_history and products:
        product_ids = [p.id for p in products]
        history_rows = (
            db.query(
                models.ProductAuditLog.product_id.label("product_id"),
                func.min(
                    case(
                        (
                            models.ProductAuditLog.action.in_(["create", "snapshot"]),
                            models.ProductAuditLog.created_at,
                        ),
                        else_=None,
                    )
                ).label("created_or_imported_at"),
                func.max(
                    case(
                        (models.ProductAuditLog.action == "update", models.ProductAuditLog.created_at),
                        else_=None,
                    )
                ).label("last_modified_at"),
            )
            .filter(models.ProductAuditLog.product_id.in_(product_ids))
            .group_by(models.ProductAuditLog.product_id)
            .all()
        )
        history_map = {
            int(row.product_id): {
                "created_or_imported_at": (
                    row.created_or_imported_at.isoformat(sep=" ", timespec="seconds")
                    if row.created_or_imported_at
                    else ""
                ),
                "last_modified_at": (
                    row.last_modified_at.isoformat(sep=" ", timespec="seconds")
                    if row.last_modified_at
                    else ""
                ),
            }
            for row in history_rows
        }

    column_map = {
        "id": ("ID", lambda p: p.id),
        "sku": ("SKU", lambda p: p.sku or ""),
        "name": ("Nombre", lambda p: p.name),
        "group_name": ("Grupo", lambda p: p.group_name or ""),
        "brand": ("Marca", lambda p: p.brand or ""),
        "supplier": ("Proveedor", lambda p: p.supplier or ""),
        "price": ("Precio", lambda p: p.price),
        "cost": ("Costo", lambda p: p.cost),
        "barcode": ("Código barras", lambda p: p.barcode or ""),
        "unit": ("Unidad", lambda p: p.unit or ""),
        "preferred_qty": ("Cant. preferida", lambda p: p.preferred_qty),
        "reorder_point": ("Punto pedido", lambda p: p.reorder_point),
        "stock_min": ("Stock mínimo", lambda p: p.stock_min),
        "low_stock_alert": ("Alerta stock", lambda p: 1 if p.low_stock_alert else 0),
        "allow_price_change": ("Cambio $ permitido", lambda p: 1 if p.allow_price_change else 0),
        "active": ("Activo", lambda p: 1 if p.active else 0),
        "service": ("Servicio", lambda p: 1 if p.service else 0),
        "includes_tax": ("IVA incl.", lambda p: 1 if p.includes_tax else 0),
        "history_created_at": (
            "Fecha creación/importación",
            lambda p: history_map.get(p.id, {}).get("created_or_imported_at", ""),
        ),
        "history_last_modified_at": (
            "Fecha última modificación",
            lambda p: history_map.get(p.id, {}).get("last_modified_at", ""),
        ),
    }

    final_columns = list(columns) if columns else []
    expanded_columns: List[str] = []
    for key in final_columns:
        if key == "history":
            expanded_columns.extend(["history_created_at", "history_last_modified_at"])
        else:
            expanded_columns.append(key)
    final_columns = expanded_columns
    if "sku" not in final_columns:
        final_columns.insert(0, "sku")
    if "name" not in final_columns:
        final_columns.insert(1, "name")

    header = [column_map[key][0] for key in final_columns if key in column_map]
    rows = [
        [column_map[key][1](p) for key in final_columns if key in column_map]
        for p in products
    ]
    return header, rows


def _model_dump(payload: Any) -> Dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=True)
    return payload.dict(exclude_unset=True)


def _build_product_changes(
    current_product: models.Product,
    update_payload: schemas.ProductUpdate,
) -> Dict[str, Dict[str, Any]]:
    incoming = _model_dump(update_payload)
    changes: Dict[str, Dict[str, Any]] = {}
    for field, new_value in incoming.items():
        old_value = getattr(current_product, field, None)
        if old_value != new_value:
            changes[field] = {"before": old_value, "after": new_value}
    return changes


@router.get("/", response_model=List[schemas.ProductRead])
def list_products(skip: int = 0, limit: int = 10000, db: Session = Depends(get_db)):
    products = crud.get_products(db, skip=skip, limit=limit)
    return products


@router.get("/catalog-version", response_model=schemas.CatalogVersion)
def get_catalog_version(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    products_ts, groups_ts, updated_at, products_count, groups_count = crud.get_catalog_version(db)
    return {
        "products_updated_at": products_ts,
        "groups_updated_at": groups_ts,
        "updated_at": updated_at,
        "products_count": products_count,
        "groups_count": groups_count,
    }


@router.post("/", response_model=schemas.ProductRead)
def create_product(
    product_in: schemas.ProductCreate,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("products.manage")),
):
    # Si quieres evitar SKUs duplicados:
    if product_in.sku:
        existing = crud.get_product_by_sku(db, product_in.sku)
        if existing:
            raise HTTPException(status_code=400, detail="SKU already registered")

    product = crud.create_product(db, product_in)
    crud.create_product_audit_log(
        db,
        product_id=product.id,
        action="create",
        actor_user=actor,
        changes={"after": _model_dump(product_in)},
    )
    return product

def get_product(product_id: int, db: Session = Depends(get_db)):
    db_product = crud.get_product(db, product_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
    return db_product


# 🔹 Actualizar producto
@router.put("/{product_id}", response_model=schemas.ProductRead)
def update_product(
    product_id: int,
    product_in: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("products.manage")),
):
    db_product = crud.get_product(db, product_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Si cambia el SKU, comprobamos duplicado
    if product_in.sku and product_in.sku != db_product.sku:
        existing = crud.get_product_by_sku(db, product_in.sku)
        if existing:
            raise HTTPException(status_code=400, detail="SKU already registered")

    changes = _build_product_changes(db_product, product_in)
    updated = crud.update_product(db, db_product, product_in)
    crud.create_product_audit_log(
        db,
        product_id=updated.id,
        action="update",
        actor_user=actor,
        changes=changes or None,
    )
    return updated


# 🔹 Eliminar producto
@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("products.manage")),
):
    db_product = crud.get_product(db, product_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    crud.create_product_audit_log(
        db,
        product_id=db_product.id,
        action="delete",
        actor_user=actor,
        changes={
            "before": {
                "id": db_product.id,
                "sku": db_product.sku,
                "name": db_product.name,
                "active": db_product.active,
            }
        },
    )
    crud.delete_product(db, db_product)
    return Response(status_code=204)


@router.get("/{product_id}/audit", response_model=List[schemas.ProductAuditLogRead])
def get_product_audit_log(
    product_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.view")),
):
    db_product = crud.get_product(db, product_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
    return crud.list_product_audit_logs(db, product_id=product_id, limit=limit)

@router.get("/export/csv")
def export_products_csv(db: Session = Depends(get_db)):
    products = crud.get_products(db, skip=0, limit=100000)

    output = io.StringIO()
    writer = csv.writer(output)

    # Cabeceras
    writer.writerow(
        [
            "id",
            "sku",
            "name",
            "price",
            "cost",
            "barcode",
            "unit",
            "image_url",
            "image_thumb_url",
            "stock_min",
            "active",
            "service",
            "includes_tax",
        ]
    )

    # Filas
    for p in products:
        writer.writerow(
            [
                p.id,
                p.sku or "",
                p.name,
                p.price,
                p.cost,
                p.barcode or "",
                p.unit or "",
                p.image_url or "",
                p.image_thumb_url or "",
                p.stock_min,
                int(p.active),
                int(p.service),
                int(p.includes_tax),
            ]
        )

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=products.csv"},
    )

router.get("/export/xlsx")
def export_products_xlsx(db: Session = Depends(get_db)):
    products = crud.get_products(db, skip=0, limit=100000)

    data = []
    for p in products:
        data.append(
            {
                "sku": p.sku,
                "nombre": p.name,
                "precio": p.price,
                "costo": p.cost,
                "grupo": p.group_name or "",
                "imagen": p.image_url or "",
                "imagen_thumb": p.image_thumb_url or "",
                "codigo_barras": p.barcode or "",
                "marca": p.brand or "",
                "proveedor": p.supplier or "",
                "cantidad_stock_bajo": p.stock_min,
                "unidad_medida": p.unit or "",
                "precio_incluye_impuestos": 1 if p.includes_tax else 0,
                "servicio_no_stock": 1 if p.service else 0,
                "producto_activo": 1 if p.active else 0,
            }
        )

    df = pd.DataFrame(data)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=products.xlsx"},
    )


@router.post("/export/xlsx")
def export_products_xlsx_custom(
    payload: ExportProductsRequest,
    db: Session = Depends(get_db),
):
    products = crud.get_products(db, skip=0, limit=100000)
    filtered = _filter_products(products, payload)
    header, rows = _build_export_rows(db, filtered, payload.columns)

    df = pd.DataFrame(rows, columns=header)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")
    output.seek(0)

    file_name = payload.file_name or "productos"
    safe_name = file_name.strip() or "productos"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={safe_name}.xlsx"
        },
    )


@router.post("/export/csv")
def export_products_csv_custom(
    payload: ExportProductsRequest,
    db: Session = Depends(get_db),
):
    products = crud.get_products(db, skip=0, limit=100000)
    filtered = _filter_products(products, payload)
    header, rows = _build_export_rows(db, filtered, payload.columns)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    output.seek(0)

    file_name = payload.file_name or "productos"
    safe_name = file_name.strip() or "productos"
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe_name}.csv"},
    )

@router.post("/import/xlsx")
async def import_products_xlsx(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.import")),
):
    """
    Importa productos desde un Excel con columnas:
    sku, nombre, precio, costo, grupo, codigo_barras, marca, proveedor,
    cantidad_stock_bajo, unidad_medida, precio_incluye_impuestos,
    servicio_no_stock, producto_activo.
    """
    contents = await file.read()
    try:
        df = pd.read_excel(BytesIO(contents))
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo Excel")

    created = 0
    updated = 0

    for _, row in df.iterrows():
        sku_val = row.get("sku")
        if pd.isna(sku_val):
            continue
        sku = str(sku_val).strip()
        if sku == "":
            continue

        nombre_val = row.get("nombre")
        if pd.isna(nombre_val) or str(nombre_val).strip() == "":
            continue
        name = str(nombre_val).strip()

        precio = float(row.get("precio") or 0)
        costo = float(row.get("costo") or 0)

        # mapeos directos
        grupo_val = row.get("grupo")
        group_name = str(grupo_val).strip() if not pd.isna(grupo_val) else None

        marca_val = row.get("marca")
        brand = str(marca_val).strip() if not pd.isna(marca_val) else None

        prov_val = row.get("proveedor")
        supplier = str(prov_val).strip() if not pd.isna(prov_val) else None

        codigo_val = row.get("codigo_barras")
        barcode = str(codigo_val).strip() if not pd.isna(codigo_val) else None

        unidad_val = row.get("unidad_medida")
        unit = str(unidad_val).strip() if not pd.isna(unidad_val) else None

        stock_min_val = row.get("cantidad_stock_bajo")
        stock_min = int(stock_min_val) if not pd.isna(stock_min_val) else 0

        preferred_val = row.get("cantidad_preferida")
        preferred_qty = int(preferred_val) if not pd.isna(preferred_val) else 0

        reorder_val = row.get("punto_pedido")
        reorder_point = int(reorder_val) if not pd.isna(reorder_val) else 0

        low_stock_val = row.get("advertencia_stock_bajo")
        low_stock_alert = False
        if not pd.isna(low_stock_val):
            try:
                low_stock_alert = bool(int(low_stock_val))
            except Exception:
                low_stock_alert = bool(low_stock_val)

        change_val = row.get("cambio_precio_permitido")
        allow_price_change = False
        if not pd.isna(change_val):
            try:
                allow_price_change = bool(int(change_val))
            except Exception:
                allow_price_change = bool(change_val)

        incluye_iva_val = row.get("precio_incluye_impuestos")
        includes_tax = (
            bool(int(incluye_iva_val))
            if not pd.isna(incluye_iva_val)
            else False
        )

        servicio_val = row.get("servicio_no_stock")
        service = (
            bool(int(servicio_val))
            if not pd.isna(servicio_val)
            else False
        )

        activo_val = row.get("producto_activo")
        active = (
            bool(int(activo_val))
            if not pd.isna(activo_val)
            else True
        )

        product_data = schemas.ProductCreate(
            sku=sku,
            name=name,
            price=precio,
            cost=costo,
            barcode=barcode,
            unit=unit,
            stock_min=stock_min,
            preferred_qty=preferred_qty,
            reorder_point=reorder_point,
            low_stock_alert=low_stock_alert,    
            allow_price_change=allow_price_change,
            active=active,
            service=service,
            includes_tax=includes_tax,
            group_name=group_name,
            brand=brand,
            supplier=supplier,
        )

        existing = crud.get_product_by_sku(db, sku)
        if existing:
            crud.update_product(db, existing, product_data)
            updated += 1
        else:
            crud.create_product(db, product_data)
            created += 1

    return {"created": created, "updated": updated}

@router.get("/export/xlsx")
def export_products_xlsx(db: Session = Depends(get_db)):
    """
    Exporta todos los productos a un archivo Excel (.xlsx)
    con las mismas columnas que usamos para importar.
    """
    products = crud.get_products(db, skip=0, limit=1_000_000)

    data = []
    for p in products:
        data.append(
            {
                "sku": p.sku or "",
                "nombre": p.name,
                "precio": p.price,
                "costo": p.cost,
                "grupo": p.group_name or "",
                "codigo_barras": p.barcode or "",
                "marca": p.brand or "",
                "proveedor": p.supplier or "",
                "cantidad_stock_bajo": p.stock_min,
                "unidad_medida": p.unit or "",
                "precio_incluye_impuestos": 1 if p.includes_tax else 0,
                "servicio_no_stock": 1 if p.service else 0,
                "producto_activo": 1 if p.active else 0,
            }
        )

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")
    output.seek(0)

    headers = {
        "Content-Disposition": 'attachment; filename="productos_kensar.xlsx"'
    }

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/export/csv")
def export_products_csv(db: Session = Depends(get_db)):
    """
    Exporta todos los productos a un archivo CSV.
    """
    products = crud.get_products(db, skip=0, limit=1_000_000)

    data = []
    for p in products:
        data.append(
            {
                "sku": p.sku or "",
                "nombre": p.name,
                "precio": p.price,
                "costo": p.cost,
                "grupo": p.group_name or "",
                "codigo_barras": p.barcode or "",
                "marca": p.brand or "",
                "proveedor": p.supplier or "",
                "cantidad_stock_bajo": p.stock_min,
                "cantidad_preferida": p.preferred_qty,
                "punto_pedido": p.reorder_point,
                "advertencia_stock_bajo": 1 if p.low_stock_alert else 0,
                "unidad_medida": p.unit or "",
                "precio_incluye_impuestos": 1 if p.includes_tax else 0,
                "servicio_no_stock": 1 if p.service else 0,
                "cambio_precio_permitido": 1 if p.allow_price_change else 0,
                "producto_activo": 1 if p.active else 0,
            }
        )

    df = pd.DataFrame(data)
    csv_data = df.to_csv(index=False)

    headers = {
        "Content-Disposition": 'attachment; filename="productos_kensar.csv"'
    }

    return PlainTextResponse(csv_data, media_type="text/csv", headers=headers)
