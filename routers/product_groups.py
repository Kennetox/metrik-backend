from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud
import schemas
from database import get_db
from dependencies import require_permission

router = APIRouter(
    prefix="/product-groups",
    tags=["product-groups"],
)


@router.get("/", response_model=List[schemas.ProductGroupRead])
def list_groups(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: object = Depends(require_permission("products.view")),
):
    return crud.list_product_groups(db, skip=skip, limit=limit)


@router.post("/", response_model=schemas.ProductGroupRead, status_code=201)
def create_group(
    group_in: schemas.ProductGroupCreate,
    db: Session = Depends(get_db),
    _: object = Depends(require_permission("products.manage")),
):
    try:
        return crud.create_product_group(db, group_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{group_id}", response_model=schemas.ProductGroupRead)
def update_group(
    group_id: int,
    group_in: schemas.ProductGroupUpdate,
    db: Session = Depends(get_db),
    _: object = Depends(require_permission("products.manage")),
):
    group = crud.get_product_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado")
    try:
        return crud.update_product_group(db, group, group_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
