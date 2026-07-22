import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

import crud
import models
from dependencies import get_current_active_user
from database import get_db
import schemas
from services import permissions
from services import storage

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
)


def require_product_media_upload(
    current_user: models.PosUser = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> models.PosUser:
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
    if permissions.role_has_permission(matrix, "products.manage", current_user.role):
        return current_user
    tenant = crud.get_tenant(db, tenant_id) if tenant_id is not None else None
    if permissions.role_has_permission(
        matrix, "commerce_web.manage", current_user.role
    ) and crud.can_user_access_tenant_module(
        tenant, "commerce_web", user=current_user
    ):
        return current_user
    raise HTTPException(status_code=403, detail="No autorizado para subir archivos")


def require_home_video_upload(
    current_user: models.PosUser = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> models.PosUser:
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
    tenant = crud.get_tenant(db, tenant_id) if tenant_id is not None else None
    if permissions.role_has_permission(
        matrix, "commerce_web.manage", current_user.role
    ) and crud.can_user_access_tenant_module(
        tenant, "commerce_web", user=current_user
    ):
        return current_user
    raise HTTPException(status_code=403, detail="No autorizado para subir archivos")


@router.post(
    "/product-images",
    response_model=schemas.UploadProductImageResponse,
)
async def upload_product_image(
    file: UploadFile = File(...),
    current_user: models.PosUser = Depends(require_product_media_upload),
):
    try:
        result = await storage.save_product_image(
            file,
            tenant_id=getattr(current_user, "tenant_id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar la imagen: {exc}") from exc

    return schemas.UploadProductImageResponse(url=result.url, thumb_url=result.thumb_url)


@router.post(
    "/product-videos",
    response_model=schemas.UploadProductVideoResponse,
)
async def upload_product_video(
    file: UploadFile = File(...),
    current_user: models.PosUser = Depends(require_product_media_upload),
):
    try:
        result = await storage.save_product_video(
            file,
            tenant_id=getattr(current_user, "tenant_id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar el video: {exc}") from exc

    return schemas.UploadProductVideoResponse(
        url=result.url,
        duration_seconds=result.duration_seconds,
        size_bytes=result.size_bytes,
    )


@router.post(
    "/home-videos",
    response_model=schemas.UploadProductVideoResponse,
)
async def upload_home_video(
    file: UploadFile = File(...),
    current_user: models.PosUser = Depends(require_home_video_upload),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    logger.info(
        "home_video_upload_received tenant_id=%s filename=%s declared_size=%s",
        tenant_id,
        file.filename,
        getattr(file, "size", None),
    )
    try:
        result = await storage.save_home_video(
            file,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        logger.warning(
            "home_video_upload_rejected tenant_id=%s filename=%s reason=%s",
            tenant_id,
            file.filename,
            exc,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        logger.exception(
            "home_video_upload_failed tenant_id=%s filename=%s",
            tenant_id,
            file.filename,
        )
        raise HTTPException(500, detail=f"No se pudo guardar el video: {exc}") from exc

    logger.info(
        "home_video_upload_completed tenant_id=%s filename=%s output_bytes=%s duration_seconds=%s",
        tenant_id,
        result.filename,
        result.size_bytes,
        result.duration_seconds,
    )
    return schemas.UploadProductVideoResponse(
        url=result.url,
        duration_seconds=result.duration_seconds,
        size_bytes=result.size_bytes,
    )
