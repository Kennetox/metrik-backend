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
