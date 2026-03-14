from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/manual-movements",
    tags=["manual-movements"],
)


def _require_tenant_id(db: Session, user: models.PosUser) -> int:
    tenant_id = crud.resolve_user_tenant_id(db, user)
    if tenant_id is None:
        raise HTTPException(
            status_code=403,
            detail="Usuario sin empresa asignada para operar movimientos manuales",
        )
    return tenant_id


def _to_manual_doc_read(
    doc: models.ManualMovementDocument,
    *,
    lines: List[models.ManualMovementDocumentLine] | None = None,
) -> schemas.ManualMovementDocumentRead:
    doc_lines = lines if lines is not None else []
    return schemas.ManualMovementDocumentRead(
        id=doc.id,
        document_number=doc.document_number or f"MM-{doc.id:06d}",
        kind=doc.kind,  # type: ignore[arg-type]
        status=doc.status,  # type: ignore[arg-type]
        origin_name=doc.origin_name,
        header=crud.parse_manual_movement_header(doc),
        notes=doc.notes,
        external_reference_type=doc.external_reference_type,
        external_reference_id=doc.external_reference_id,
        created_by_user_id=doc.created_by_user_id,
        created_by_user_name=doc.created_by_user_name,
        closed_by_user_id=doc.closed_by_user_id,
        closed_by_user_name=doc.closed_by_user_name,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        closed_at=doc.closed_at,
        lines_count=len(doc_lines),
        units_total=float(sum(abs(float(line.qty or 0.0)) for line in doc_lines)),
    )


@router.post("/documents", response_model=schemas.ManualMovementDocumentRead, status_code=201)
def create_manual_movement_document(
    payload: schemas.ManualMovementDocumentCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    crud.acquire_manual_movement_document_creation_lock(db)
    doc = crud.create_manual_movement_document(
        db,
        payload,
        created_by_user_id=current_user.id,
        tenant_id=tenant_id,
    )
    return _to_manual_doc_read(doc, lines=[])


@router.get("/documents", response_model=schemas.ManualMovementDocumentPage)
def list_manual_movement_documents(
    status: schemas.ManualMovementStatus | None = Query(default=None),
    kind: schemas.ManualMovementKind | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.view")),
):
    tenant_id = _require_tenant_id(db, current_user)
    docs = crud.list_manual_movement_documents(
        db,
        status=status,
        kind=kind,
        skip=skip,
        limit=limit,
        tenant_id=tenant_id,
    )
    total = crud.count_manual_movement_documents(
        db,
        status=status,
        kind=kind,
        tenant_id=tenant_id,
    )
    items: List[schemas.ManualMovementDocumentRead] = []
    for doc in docs:
        lines = crud.list_manual_movement_document_lines(db, doc.id, tenant_id=tenant_id)
        items.append(_to_manual_doc_read(doc, lines=lines))
    return schemas.ManualMovementDocumentPage(items=items, total=total, skip=skip, limit=limit)


@router.get("/documents/{document_id}", response_model=schemas.ManualMovementDocumentDetail)
def get_manual_movement_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.view")),
):
    tenant_id = _require_tenant_id(db, current_user)
    doc = crud.get_manual_movement_document(db, document_id, tenant_id=tenant_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    lines = crud.list_manual_movement_document_lines(db, document_id, tenant_id=tenant_id)
    return schemas.ManualMovementDocumentDetail(
        document=_to_manual_doc_read(doc, lines=lines),
        lines=[schemas.ManualMovementDocumentLineRead.model_validate(line) for line in lines],
    )


@router.patch("/documents/{document_id}/header", response_model=schemas.ManualMovementDocumentRead)
def update_manual_movement_document_header(
    document_id: int,
    payload: schemas.ManualMovementDocumentHeaderUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    doc = crud.get_manual_movement_document(db, document_id, tenant_id=tenant_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if doc.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes editar documentos abiertos")
    updated = crud.update_manual_movement_document_header(
        db,
        doc,
        header=payload.header,
        notes=payload.notes,
    )
    lines = crud.list_manual_movement_document_lines(db, document_id, tenant_id=tenant_id)
    return _to_manual_doc_read(updated, lines=lines)


@router.put("/documents/{document_id}/lines", response_model=schemas.ManualMovementDocumentDetail)
def replace_manual_movement_document_lines(
    document_id: int,
    payload: schemas.ManualMovementDocumentLinesUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    doc = crud.get_manual_movement_document(db, document_id, tenant_id=tenant_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if doc.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes editar documentos abiertos")
    lines = crud.replace_manual_movement_document_lines(
        db,
        doc,
        payload.lines,
        tenant_id=tenant_id,
    )
    refreshed = crud.get_manual_movement_document(db, document_id, tenant_id=tenant_id)
    return schemas.ManualMovementDocumentDetail(
        document=_to_manual_doc_read(refreshed or doc, lines=lines),
        lines=[schemas.ManualMovementDocumentLineRead.model_validate(line) for line in lines],
    )


@router.post("/documents/{document_id}/close", response_model=schemas.ManualMovementDocumentRead)
def close_manual_movement_document(
    document_id: int,
    payload: schemas.ManualMovementDocumentClose,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    doc = crud.get_manual_movement_document(db, document_id, tenant_id=tenant_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if doc.status != "open":
        raise HTTPException(status_code=409, detail="El documento ya está cerrado o cancelado")
    lines = crud.list_manual_movement_document_lines(db, document_id, tenant_id=tenant_id)
    if doc.kind in {"salida_manual", "ajuste", "perdida_dano"} and len(lines) == 0:
        raise HTTPException(status_code=409, detail="No puedes cerrar un documento sin líneas")
    closed = crud.close_manual_movement_document(
        db,
        doc,
        closed_by_user_id=current_user.id,
        external_reference_type=payload.external_reference_type,
        external_reference_id=payload.external_reference_id,
        tenant_id=tenant_id,
    )
    refreshed_lines = crud.list_manual_movement_document_lines(db, document_id, tenant_id=tenant_id)
    return _to_manual_doc_read(closed, lines=refreshed_lines)


@router.post("/documents/{document_id}/cancel", response_model=schemas.ManualMovementDocumentRead)
def cancel_manual_movement_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("movements.manage")),
):
    tenant_id = _require_tenant_id(db, current_user)
    doc = crud.get_manual_movement_document(db, document_id, tenant_id=tenant_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if doc.status != "open":
        raise HTTPException(status_code=409, detail="Solo puedes cancelar documentos abiertos")
    cancelled = crud.cancel_manual_movement_document(db, doc)
    lines = crud.list_manual_movement_document_lines(db, document_id, tenant_id=tenant_id)
    return _to_manual_doc_read(cancelled, lines=lines)

