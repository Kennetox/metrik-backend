from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_module_access, require_permission
from routers import web_payments_mercadopago


router = APIRouter(
    prefix="/comercio-web",
    tags=["comercio-web"],
)


def _tenant_id_for_user(db: Session, user: models.PosUser) -> int:
    tenant_id = crud.resolve_user_tenant_id(db, user)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="Usuario sin empresa asignada")
    return tenant_id


@router.get("/orders", response_model=list[schemas.WebOrderRead])
def list_comercio_web_orders(
    limit: int = Query(default=100, ge=1, le=200),
    status: Optional[schemas.WebOrderStatus] = Query(default=None),
    payment_status: Optional[schemas.WebOrderPaymentStatus] = Query(default=None),
    search: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.view")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    orders = crud.list_backoffice_web_orders(
        db,
        tenant_id=tenant_id,
        limit=limit,
        status=status,
        payment_status=payment_status,
        search=search,
    )
    orders = web_payments_mercadopago.refresh_backoffice_order_payment_statuses(db, orders)
    return [crud._serialize_web_order(order) for order in orders]


@router.get("/catalog/products", response_model=list[schemas.ProductRead])
def list_comercio_web_catalog_products(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    q: Optional[str] = Query(default=None),
    published_only: Optional[bool] = Query(default=None),
    configured_only: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.view")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    if (
        q is not None
        or published_only is not None
        or configured_only is not None
        or skip != 0
        or limit != 1000
    ):
        return crud.search_comercio_web_catalog_products(
            db,
            tenant_id=tenant_id,
            q=q,
            published_only=published_only,
            configured_only=configured_only,
            skip=skip,
            limit=limit,
        )
    return crud.get_products(db, skip=skip, limit=limit, tenant_id=tenant_id)


@router.get(
    "/catalog/publications",
    response_model=schemas.ComercioWebCatalogPublicationPage,
)
def list_comercio_web_publications(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    q: Optional[str] = Query(default=None),
    field: str = Query(default="all"),
    status_filter: str = Query(default="all"),
    featured_filter: str = Query(default="all"),
    badge_filter: str = Query(default="all"),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.view")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    return crud.list_comercio_web_publications_page(
        db,
        tenant_id=tenant_id,
        q=q,
        field=field,
        status_filter=status_filter,
        featured_filter=featured_filter,
        badge_filter=badge_filter,
        skip=skip,
        limit=limit,
    )


@router.put("/catalog/products/{product_id}", response_model=schemas.ProductRead)
def update_comercio_web_catalog_product(
    product_id: int,
    payload: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    product = crud.get_product(db, product_id, tenant_id=tenant_id)
    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    try:
        updated = crud.update_product(db, product, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    crud.create_product_audit_log(
        db,
        product_id=updated.id,
        action="update",
        actor_user=current_user,
        changes={"commerce_web_catalog": True},
    )
    return updated


@router.get(
    "/catalog/categories",
    response_model=list[schemas.ComercioWebCatalogCategoryRead],
)
def list_comercio_web_catalog_categories(
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.view")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    return crud.list_comercio_web_catalog_categories(
        db,
        tenant_id=tenant_id,
        include_inactive=include_inactive,
    )


@router.post(
    "/catalog/categories",
    response_model=schemas.ComercioWebCatalogCategoryRead,
)
def create_comercio_web_catalog_category(
    payload: schemas.ComercioWebCatalogCategoryCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    try:
        return crud.create_comercio_web_catalog_category(
            db,
            tenant_id=tenant_id,
            payload=payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/catalog/categories/{category_id}",
    response_model=schemas.ComercioWebCatalogCategoryRead,
)
def update_comercio_web_catalog_category(
    category_id: int,
    payload: schemas.ComercioWebCatalogCategoryUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    try:
        return crud.update_comercio_web_catalog_category(
            db,
            tenant_id=tenant_id,
            category_id=category_id,
            payload=payload,
        )
    except ValueError as exc:
        detail = str(exc)
        if "no encontrada" in detail.lower():
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc


@router.delete("/catalog/categories/{category_id}")
def delete_comercio_web_catalog_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    try:
        crud.delete_comercio_web_catalog_category(
            db,
            tenant_id=tenant_id,
            category_id=category_id,
        )
    except ValueError as exc:
        detail = str(exc)
        if "no encontrada" in detail.lower():
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return {"ok": True}


@router.get(
    "/catalog/discount-codes",
    response_model=schemas.ComercioWebDiscountCodePage,
)
def list_comercio_web_discount_codes(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    q: Optional[str] = Query(default=None),
    active_only: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.view")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    return crud.list_comercio_web_discount_codes_page(
        db,
        tenant_id=tenant_id,
        q=q,
        active_only=active_only,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/catalog/discount-codes",
    response_model=schemas.ComercioWebDiscountCodeRead,
)
def create_comercio_web_discount_code(
    payload: schemas.ComercioWebDiscountCodeCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    try:
        return crud.create_comercio_web_discount_code(
            db,
            tenant_id=tenant_id,
            payload=payload,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/catalog/discount-codes/{discount_code_id}",
    response_model=schemas.ComercioWebDiscountCodeRead,
)
def update_comercio_web_discount_code(
    discount_code_id: int,
    payload: schemas.ComercioWebDiscountCodeUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    try:
        return crud.update_comercio_web_discount_code(
            db,
            tenant_id=tenant_id,
            discount_code_id=discount_code_id,
            payload=payload,
        )
    except ValueError as exc:
        detail = str(exc)
        if "no encontrado" in detail.lower():
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc


@router.get("/orders/{order_id}", response_model=schemas.WebOrderRead)
def get_comercio_web_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.view")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    order = web_payments_mercadopago.refresh_backoffice_order_payment_statuses(db, [order])[0]
    return crud._serialize_web_order(order)


@router.post("/orders/{order_id}/payments", response_model=schemas.WebOrderRead)
def record_comercio_web_payment(
    order_id: int,
    payload: schemas.WebOrderPaymentRecordRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    try:
        return crud.record_web_order_payment(
            db,
            order,
            payload,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orders/{order_id}/status", response_model=schemas.WebOrderRead)
def update_comercio_web_order_status(
    order_id: int,
    payload: schemas.WebOrderStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    try:
        return crud.update_backoffice_web_order_status(
            db,
            order,
            payload,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orders/{order_id}/convert-to-sale", response_model=schemas.WebOrderRead)
def convert_comercio_web_order_to_sale(
    order_id: int,
    payload: schemas.WebOrderConvertToSaleRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("commerce_web.manage")),
    _: models.PosUser = Depends(require_module_access("commerce_web")),
):
    tenant_id = _tenant_id_for_user(db, current_user)
    order = crud.get_backoffice_web_order(db, order_id, tenant_id=tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    try:
        return crud.convert_web_order_to_sale(
            db,
            order,
            payload,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
