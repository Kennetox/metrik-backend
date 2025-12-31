from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File
from sqlalchemy.orm import Session
from typing import List
from io import BytesIO
from fastapi.responses import StreamingResponse, PlainTextResponse
import io
import csv
import pandas as pd

import schemas, crud, models
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/products",
    tags=["products"]
)


@router.get("/", response_model=List[schemas.ProductRead])
def list_products(skip: int = 0, limit: int = 10000, db: Session = Depends(get_db)):
    products = crud.get_products(db, skip=skip, limit=limit)
    return products


@router.post("/", response_model=schemas.ProductRead)
def create_product(
    product_in: schemas.ProductCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.manage")),
):
    # Si quieres evitar SKUs duplicados:
    if product_in.sku:
        existing = crud.get_product_by_sku(db, product_in.sku)
        if existing:
            raise HTTPException(status_code=400, detail="SKU already registered")

    product = crud.create_product(db, product_in)
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
    _: models.PosUser = Depends(require_permission("products.manage")),
):
    db_product = crud.get_product(db, product_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Si cambia el SKU, comprobamos duplicado
    if product_in.sku and product_in.sku != db_product.sku:
        existing = crud.get_product_by_sku(db, product_in.sku)
        if existing:
            raise HTTPException(status_code=400, detail="SKU already registered")

    updated = crud.update_product(db, db_product, product_in)
    return updated


# 🔹 Eliminar producto
@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("products.manage")),
):
    db_product = crud.get_product(db, product_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    crud.delete_product(db, db_product)
    return Response(status_code=204)

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

@router.post("/import/xlsx")
async def import_products_xlsx(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
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
