from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

import models
from dependencies import get_current_active_user
import schemas
from services import storage

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
)


@router.post(
    "/product-images",
    response_model=schemas.UploadProductImageResponse,
)
async def upload_product_image(
    file: UploadFile = File(...),
    current_user: models.PosUser = Depends(get_current_active_user),
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
    current_user: models.PosUser = Depends(get_current_active_user),
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
        duration_seconds=None,
        size_bytes=result.size_bytes,
    )
