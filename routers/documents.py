from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission
from services.document_search import MANUAL_TYPES, search_documents


router = APIRouter(prefix="/documents", tags=["documents"])

DOCUMENT_TYPES = {
    "all", "venta", "orden_web", "devolucion", "cambio", "anulacion",
    "abono", "recepcion", "movimiento_manual", "recuento", "cierre",
    *MANUAL_TYPES.keys(),
}


@router.get("/search", response_model=schemas.DocumentSearchPage)
def search_document_history(
    document_type: str = Query(default="all", alias="type"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    term: str | None = Query(default=None, max_length=160),
    payment_method: str | None = Query(default=None, max_length=80),
    customer: str | None = Query(default=None, max_length=160),
    pos: str | None = Query(default=None, max_length=160),
    vendor: str | None = Query(default=None, max_length=160),
    skip: int = Query(default=0, ge=0, le=5000),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("documents")),
):
    normalized_type = (document_type or "all").strip().lower()
    if normalized_type not in DOCUMENT_TYPES:
        normalized_type = "all"
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    try:
        items, has_more = search_documents(
            db,
            tenant_id=tenant_id,
            document_type=normalized_type,
            date_from=date_from,
            date_to=date_to,
            term=term,
            payment_method=payment_method,
            customer=customer,
            pos=pos,
            vendor=vendor,
            skip=skip,
            limit=limit,
        )
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail=(
                "La búsqueda excedió el tiempo seguro. Reduce el periodo o "
                "agrega un filtro más específico."
            ),
        ) from exc
    return schemas.DocumentSearchPage(
        items=items,
        skip=skip,
        limit=limit,
        has_more=has_more,
    )
