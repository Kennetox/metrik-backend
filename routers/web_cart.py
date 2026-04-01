from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from routers.web_customers import require_web_customer_auth


router = APIRouter(
    prefix="/web/cart",
    tags=["web-cart"],
)


@router.get("", response_model=schemas.WebCartRead)
def get_my_cart(
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    return crud.get_web_cart(db, account)


@router.post("/items", response_model=schemas.WebCartRead, status_code=201)
def add_cart_item(
    payload: schemas.WebCartItemMutationRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    try:
        return crud.add_item_to_web_cart(db, account, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/items/{product_id}", response_model=schemas.WebCartRead)
def update_cart_item(
    product_id: int,
    payload: schemas.WebCartItemUpdateRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    try:
        return crud.update_web_cart_item_quantity(db, account, product_id, payload.quantity)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/items/{product_id}", response_model=schemas.WebCartRead)
def remove_cart_item(
    product_id: int,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    try:
        return crud.update_web_cart_item_quantity(db, account, product_id, 0)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("", status_code=204)
def clear_cart(
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    crud.clear_web_cart(db, account)
    return Response(status_code=204)


@router.put("/coupon", response_model=schemas.WebCartRead)
def apply_cart_coupon(
    payload: schemas.WebCartCouponApplyRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    try:
        return crud.apply_coupon_to_web_cart(db, account, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/coupon", response_model=schemas.WebCartRead)
def clear_cart_coupon(
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    return crud.clear_coupon_from_web_cart(db, account)
