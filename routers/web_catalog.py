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


@router.get("/products", response_model=schemas.WebCatalogProductList)
def list_web_catalog_products(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    brand: str | None = Query(default=None),
    featured: bool | None = Query(default=None),
    sort: str = Query(default="recommended", pattern="^(recommended|name_asc|price_asc|price_desc)$"),
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
        brand=brand,
        featured=featured,
        sort=sort,
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
