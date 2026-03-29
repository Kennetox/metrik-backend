from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from routers.web_customers import require_web_customer_auth


router = APIRouter(
    prefix="/web/orders",
    tags=["web-orders"],
)


@router.get("", response_model=list[schemas.WebOrderRead])
def list_my_web_orders(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    orders = crud.list_web_orders(db, account, limit=limit)
    return [crud._serialize_web_order(order) for order in orders]


@router.post("", response_model=schemas.WebOrderRead, status_code=201)
def create_web_order(
    payload: schemas.WebOrderCreateFromCartRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    try:
        return crud.create_web_order_from_cart(db, account, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{order_id}", response_model=schemas.WebOrderRead)
def get_my_web_order(
    order_id: int,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    return crud._serialize_web_order(order)


@router.post("/{order_id}/payments/manual", response_model=schemas.WebOrderRead)
def submit_manual_payment_for_order(
    order_id: int,
    payload: schemas.WebOrderCustomerPaymentSubmissionRequest,
    db: Session = Depends(get_db),
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    order = crud.get_web_order(db, order_id, account.id, tenant_id=account.tenant_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden web no encontrada")
    try:
        return crud.submit_customer_web_order_payment(db, order, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
