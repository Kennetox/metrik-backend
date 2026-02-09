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
AVATAR_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2MB
DOC_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx"}
MAX_DOC_SIZE = 5 * 1024 * 1024  # 5MB


@dataclass
class StoredProductImage:
    filename: str
    url: str
    thumb_url: str


@dataclass
class StoredLogo:
    filename: str
    url: str


@dataclass
class StoredAvatar:
    filename: str
    url: str


@dataclass
class StoredUserDocument:
    filename: str
    url: str
    size: int


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


def _get_avatar_dir() -> Path:
    return Path(os.getenv("USER_AVATAR_UPLOAD_DIR", "uploads/user-avatars"))


def _build_avatar_url(filename: str) -> str:
    base_url = os.getenv("USER_AVATAR_BASE_URL")
    if base_url:
        return f"{base_url.rstrip('/')}/{filename}"
    storage_path = os.getenv("USER_AVATAR_PUBLIC_PATH", "/uploads/user-avatars")
    return f"{storage_path.rstrip('/')}/{filename}"


def _get_user_documents_dir(user_id: int) -> Path:
    base = Path(os.getenv("USER_DOC_UPLOAD_DIR", "uploads/user-documents"))
    return base / str(user_id)


def _build_user_document_url(filename: str, user_id: int) -> str:
    base_url = os.getenv("USER_DOC_BASE_URL")
    if base_url:
        return f"{base_url.rstrip('/')}/{user_id}/{filename}"
    storage_path = os.getenv("USER_DOC_PUBLIC_PATH", "/uploads/user-documents")
    return f"{storage_path.rstrip('/')}/{user_id}/{filename}"


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


async def save_user_avatar(file: UploadFile) -> StoredAvatar:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in AVATAR_ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa JPG, PNG o WEBP.")

    contents = await file.read()
    if len(contents) > MAX_AVATAR_SIZE:
        raise ValueError("La imagen supera los 2MB permitidos")

    filename = f"user-avatar-{uuid4().hex}{extension}"
    base_dir = _get_avatar_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_avatar_url(filename)
    return StoredAvatar(filename=filename, url=url)


async def save_user_document(file: UploadFile, user_id: int) -> StoredUserDocument:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in DOC_ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa PDF, JPG, PNG, WEBP o DOC/DOCX.")

    contents = await file.read()
    if len(contents) > MAX_DOC_SIZE:
        raise ValueError("El archivo supera los 5MB permitidos")

    filename = f"user-doc-{uuid4().hex}{extension}"
    base_dir = _get_user_documents_dir(user_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_user_document_url(filename, user_id)
    return StoredUserDocument(filename=original_name, url=url, size=len(contents))
