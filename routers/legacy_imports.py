from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission
from services import legacy_imports

router = APIRouter(prefix="/legacy-imports", tags=["legacy-imports"])


@router.post("/batches", response_model=schemas.LegacyImportBatchRead, status_code=201)
def create_batch(
    payload: schemas.LegacyImportBatchCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    base_key = payload.batch_key or payload.title
    slug = re.sub(r"[^a-z0-9]+", "-", (base_key or "batch").strip().lower()).strip("-")
    if not slug:
        slug = datetime.utcnow().strftime("batch-%Y%m%d-%H%M%S")

    existing = (
        db.query(models.LegacyImportBatch)
        .filter(models.LegacyImportBatch.tenant_id == tenant_id)
        .filter(models.LegacyImportBatch.batch_key == slug)
        .first()
    )
    if existing:
        slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

    row = models.LegacyImportBatch(
        tenant_id=tenant_id,
        source_system=(payload.source_system or "aronium").strip().lower() or "aronium",
        batch_key=slug,
        title=payload.title.strip() or "Lote históricos",
        note=(payload.note or "").strip() or None,
        status="draft",
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/batches/{batch_id}/files", response_model=schemas.LegacyImportBatchRead)
async def upload_batch_file(
    batch_id: int,
    file_kind: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    batch = (
        db.query(models.LegacyImportBatch)
        .filter(models.LegacyImportBatch.id == batch_id)
        .filter(models.LegacyImportBatch.tenant_id == tenant_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Lote no encontrado")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío")

    path = legacy_imports.save_uploaded_file(
        tenant_id=tenant_id,
        batch_id=batch.id,
        file_kind=file_kind,
        filename=file.filename or "archivo.csv",
        content=content,
    )

    normalized_kind = file_kind.strip().lower()
    if normalized_kind in {"sales", "ventas"}:
        batch.uploaded_sales_path = path
    elif normalized_kind in {"items", "detalle"}:
        batch.uploaded_items_path = path
    elif normalized_kind in {"payments", "pagos"}:
        batch.uploaded_payments_path = path
    elif normalized_kind in {"refunds", "devoluciones"}:
        batch.uploaded_refunds_path = path
    else:
        raise HTTPException(status_code=400, detail="file_kind no soportado")

    batch.status = "files_uploaded"
    batch.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/batches/{batch_id}/process", response_model=schemas.LegacyImportProcessResponse)
def process_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    batch = (
        db.query(models.LegacyImportBatch)
        .filter(models.LegacyImportBatch.id == batch_id)
        .filter(models.LegacyImportBatch.tenant_id == tenant_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Lote no encontrado")

    try:
        result = legacy_imports.process_batch(db, batch)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error procesando lote: {exc}") from exc

    db.refresh(batch)
    return schemas.LegacyImportProcessResponse(
        batch=batch,
        sales_loaded=result.sales_loaded,
        items_loaded=result.items_loaded,
        payments_loaded=result.payments_loaded,
        warnings=result.warnings,
    )


@router.get("/batches/{batch_id}", response_model=schemas.LegacyImportBatchRead)
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    batch = (
        db.query(models.LegacyImportBatch)
        .filter(models.LegacyImportBatch.id == batch_id)
        .filter(models.LegacyImportBatch.tenant_id == tenant_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    return batch


@router.get("/batches", response_model=schemas.LegacyImportBatchListResponse)
def list_batches(
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("settings.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    items = legacy_imports.list_batches(db, tenant_id=tenant_id, limit=limit)
    return schemas.LegacyImportBatchListResponse(items=items, total=len(items))
