import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import UploadFile

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE = 2 * 1024 * 1024  # 2MB
LOGO_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".svg"}
MAX_LOGO_SIZE = 1 * 1024 * 1024  # 1MB


@dataclass
class StoredProductImage:
    filename: str
    url: str
    thumb_url: str


@dataclass
class StoredLogo:
    filename: str
    url: str


def _get_base_dir(tenant_id: Optional[int] = None) -> Path:
    base_dir = Path(os.getenv("PRODUCT_UPLOAD_DIR", "uploads/product-images"))
    if tenant_id is not None:
        base_dir = base_dir / str(tenant_id)
    return base_dir


def get_product_images_dir() -> Path:
    """Returns the directory where original product images are stored."""
    return _get_base_dir()


def get_uploads_root_dir() -> Path:
    """Directory exposed via FastAPI's static mount (e.g. ./uploads)."""
    base_dir = _get_base_dir()
    parent = base_dir.parent
    # When the directory already represents the root (no named parent), reuse it.
    return base_dir if parent == Path(".") else parent


def _build_public_url(filename: str, tenant_id: Optional[int] = None) -> str:
    relative_parts = [filename]
    if tenant_id is not None:
        relative_parts.insert(0, str(tenant_id))
    relative_path = "/".join(relative_parts)

    base_url = os.getenv("PRODUCT_UPLOAD_BASE_URL")
    if base_url:
        return f"{base_url.rstrip('/')}/{relative_path}"
    storage_path = os.getenv("PRODUCT_UPLOAD_PUBLIC_PATH", "/uploads/product-images")
    return f"{storage_path.rstrip('/')}/{relative_path}"


def _get_logo_dir() -> Path:
    return Path(os.getenv("POS_LOGO_UPLOAD_DIR", "uploads/pos-logos"))


def _build_logo_url(filename: str) -> str:
    base_url = os.getenv("POS_LOGO_BASE_URL")
    if base_url:
        return f"{base_url.rstrip('/')}/{filename}"
    storage_path = os.getenv("POS_LOGO_PUBLIC_PATH", "/uploads/pos-logos")
    return f"{storage_path.rstrip('/')}/{filename}"


async def save_product_image(
    file: UploadFile,
    tenant_id: Optional[int] = None,
) -> StoredProductImage:
    """Saves an uploaded product image and returns its public URLs."""

    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa JPG, PNG o WEBP.")

    contents = await file.read()
    if len(contents) > MAX_IMAGE_SIZE:
        raise ValueError("La imagen supera los 2MB permitidos")

    filename = f"{uuid4().hex}{extension}"

    base_dir = _get_base_dir(tenant_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_public_url(filename, tenant_id)
    return StoredProductImage(filename=filename, url=url, thumb_url=url)


async def save_pos_logo(file: UploadFile) -> StoredLogo:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in LOGO_ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa PNG, JPG o SVG.")

    contents = await file.read()
    if len(contents) > MAX_LOGO_SIZE:
        raise ValueError("El logo supera 1MB")

    filename = f"pos-logo-{uuid4().hex}{extension}"
    base_dir = _get_logo_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_logo_url(filename)
    return StoredLogo(filename=filename, url=url)
