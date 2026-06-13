from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import crud
import schemas
from database import get_db


router = APIRouter(
    prefix="/web/catalog",
    tags=["web-catalog"],
)


@router.get("/version", response_model=schemas.WebCatalogVersion)
def get_web_catalog_version(
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    return crud.get_web_catalog_version(db, tenant_id=tenant_id)


@router.get("/categories", response_model=schemas.WebCatalogCategoryList)
def list_web_catalog_categories(
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    items = crud.get_web_catalog_categories(db, tenant_id=tenant_id)
    return schemas.WebCatalogCategoryList(items=items)


@router.get("/home-sliders", response_model=schemas.WebCatalogHomeSliderList)
def list_web_catalog_home_sliders(
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    items = crud.list_public_web_home_sliders(db, tenant_id=tenant_id)
    return schemas.WebCatalogHomeSliderList(items=items)


@router.get("/products", response_model=schemas.WebCatalogProductList)
def list_web_catalog_products(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    featured: bool | None = Query(default=None),
    sort: str = Query(default="recommended", pattern="^(recommended|name_asc|name_desc|price_asc|price_desc)$"),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=60),
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    return crud.get_web_catalog_products(
        db,
        tenant_id=tenant_id,
        q=q,
        category=category,
        brands=brand,
        featured=featured,
        sort=sort,
        min_price=min_price,
        max_price=max_price,
        page=page,
        page_size=page_size,
    )


@router.get("/best-sellers", response_model=schemas.WebCatalogBestSellerList)
def list_web_catalog_best_sellers(
    limit: int = Query(default=10, ge=1, le=20),
    days: int = Query(default=90, ge=7, le=365),
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    items, updated_at = crud.get_web_catalog_best_sellers(
        db,
        tenant_id=tenant_id,
        limit=limit,
        days=days,
    )
    return schemas.WebCatalogBestSellerList(items=items, updated_at=updated_at)


@router.get("/combos", response_model=list[schemas.ComercioWebComboRead])
def list_web_catalog_combos(
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    return crud.list_comercio_web_combos(
        db,
        tenant_id=tenant_id,
        q=q,
        published_only=True,
        active_only=True,
    )


@router.get("/products/{slug}", response_model=schemas.WebCatalogProductDetail)
def get_web_catalog_product(
    slug: str,
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    product = crud.get_web_catalog_product_by_slug(db, slug=slug, tenant_id=tenant_id)
    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return product


@router.get(
    "/personalization/bindings",
    response_model=schemas.WebPersonalizationBindings,
)
def get_web_personalization_bindings(
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    return crud.get_public_web_personalization_bindings(db, tenant_id=tenant_id)


@router.get(
    "/personalization/home-images",
    response_model=schemas.WebPersonalizationHomeImages,
)
def get_web_personalization_home_images(
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    return crud.get_public_web_personalization_home_images(db, tenant_id=tenant_id)


@router.get(
    "/brand-collage",
    response_model=schemas.WebBrandCollageImages,
)
def get_web_brand_collage_images(
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    return crud.get_public_web_brand_collage_images(db, tenant_id=tenant_id)


@router.get(
    "/personalization/service-by-sku",
    response_model=schemas.ProductRead,
)
def get_web_personalization_service_by_sku(
    sku: str = Query(min_length=1),
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    normalized_sku = sku.strip()
    bindings = crud.get_public_web_personalization_bindings(db, tenant_id=tenant_id)
    allowed_service_skus = {
        str(row.get("service_sku") or "").strip()
        for row in (bindings or {}).values()
        if isinstance(row, dict)
    }
    if not normalized_sku or normalized_sku not in allowed_service_skus:
        raise HTTPException(status_code=404, detail="Servicio de personalización no disponible")

    product = crud.get_product_by_sku(db, normalized_sku, tenant_id=tenant_id)
    if not product or not bool(product.active):
        raise HTTPException(status_code=404, detail="Servicio de personalización no encontrado")
    if not bool(product.service):
        raise HTTPException(status_code=400, detail="El SKU vinculado no corresponde a un servicio")

    return crud.get_product(db, product.id, tenant_id=tenant_id)


@router.post("/coupon/preview", response_model=schemas.WebGuestCouponPreviewResponse)
def preview_web_coupon(
    payload: schemas.WebGuestCouponPreviewRequest,
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    normalized_code = (payload.code or "").strip().upper()
    if not normalized_code:
        raise HTTPException(status_code=400, detail="Ingresa un código válido")

    valid_coupon = crud._resolve_valid_discount_code(
        db,
        tenant_id=tenant_id,
        code=normalized_code,
    )
    if not valid_coupon:
        raise HTTPException(status_code=400, detail="El código no está disponible o ya venció")

    item_inputs = [item for item in (payload.items or []) if float(item.quantity or 0) > 0]
    if not item_inputs:
        raise HTTPException(status_code=400, detail="El checkout no tiene items válidos.")

    product_ids = list({int(item.product_id) for item in item_inputs})
    qty_by_product = crud._get_web_cart_stock_snapshot(db, tenant_id, product_ids)

    subtotal_base = 0.0
    for item_input in item_inputs:
        product = crud.get_product(db, int(item_input.product_id), tenant_id=tenant_id)
        if not product or not product.active or not product.web_published:
            raise HTTPException(
                status_code=400,
                detail=f"Producto {item_input.product_id} no disponible para checkout web.",
            )
        stock_status = crud.resolve_web_product_stock_status(
            product,
            qty_by_product.get(product.id, 0.0),
        )
        if stock_status == "out_of_stock" and product.web_visible_when_out_of_stock is False:
            raise HTTPException(
                status_code=400,
                detail=f"Producto {product.name} no disponible por stock.",
            )
        quantity = float(item_input.quantity or 0.0)
        unit_price = float(crud.resolve_web_product_sale_price(product) or 0.0)
        subtotal_base += unit_price * quantity

    if subtotal_base <= 0:
        raise HTTPException(status_code=400, detail="No se pudo calcular el subtotal del checkout.")

    discount_type, discount_value, discount_percent = crud._resolve_discount_code_snapshot_values(
        discount_type=getattr(valid_coupon, "discount_type", None),
        discount_value=getattr(valid_coupon, "discount_value", None),
        discount_percent=getattr(valid_coupon, "discount_percent", None),
    )
    discount_amount = crud._compute_coupon_discount_amount(
        subtotal_base,
        discount_type=discount_type,
        discount_value=discount_value,
        discount_percent=discount_percent,
    )
    total = max(0.0, subtotal_base - discount_amount)

    return schemas.WebGuestCouponPreviewResponse(
        code=normalized_code,
        discount_type=discount_type, 
        discount_value=discount_value,
        discount_percent=discount_percent,
        subtotal_base=round(subtotal_base, 2),
        discount_amount=discount_amount,
        total=total,
    )
