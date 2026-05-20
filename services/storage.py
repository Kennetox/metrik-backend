import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import UploadFile

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_VIDEO_EXTENSIONS = {".mp4"}
MAX_VIDEO_SIZE = 20 * 1024 * 1024  # 20MB
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
class StoredProductVideo:
    filename: str
    url: str
    size_bytes: int


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


@dataclass
class StoredReceivingSupportFile:
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


def _get_product_video_dir(tenant_id: Optional[int] = None) -> Path:
    configured = os.getenv("PRODUCT_VIDEO_UPLOAD_DIR")
    if configured:
        base_dir = Path(configured)
    else:
        base_dir = _get_base_dir().parent / "product-videos"
    if tenant_id is not None:
        base_dir = base_dir / str(tenant_id)
    return base_dir


def _build_product_video_public_url(filename: str, tenant_id: Optional[int] = None) -> str:
    relative_parts = [filename]
    if tenant_id is not None:
        relative_parts.insert(0, str(tenant_id))
    relative_path = "/".join(relative_parts)
    base_url = os.getenv("PRODUCT_VIDEO_UPLOAD_BASE_URL")
    if base_url:
        return f"{base_url.rstrip('/')}/{relative_path}"
    storage_path = os.getenv("PRODUCT_VIDEO_UPLOAD_PUBLIC_PATH", "/uploads/product-videos")
    return f"{storage_path.rstrip('/')}/{relative_path}"


def _get_logo_dir() -> Path:
    return Path(os.getenv("POS_LOGO_UPLOAD_DIR", "uploads/pos-logos"))


def _build_logo_url(filename: str) -> str:
    base_url = os.getenv("POS_LOGO_BASE_URL")
    if base_url:
        return f"{base_url.rstrip('/')}/{filename}"
    storage_path = os.getenv("POS_LOGO_PUBLIC_PATH", "/uploads/pos-logos")
    return f"{storage_path.rstrip('/')}/{filename}"


def _get_avatar_dir(tenant_id: Optional[int] = None) -> Path:
    base = Path(os.getenv("USER_AVATAR_UPLOAD_DIR", "uploads/user-avatars"))
    if tenant_id is not None:
        return base / str(tenant_id)
    return base


def _build_avatar_url(filename: str, tenant_id: Optional[int] = None) -> str:
    base_url = os.getenv("USER_AVATAR_BASE_URL")
    if base_url:
        if tenant_id is not None:
            return f"{base_url.rstrip('/')}/{tenant_id}/{filename}"
        return f"{base_url.rstrip('/')}/{filename}"
    storage_path = os.getenv("USER_AVATAR_PUBLIC_PATH", "/uploads/user-avatars")
    if tenant_id is not None:
        return f"{storage_path.rstrip('/')}/{tenant_id}/{filename}"
    return f"{storage_path.rstrip('/')}/{filename}"


def _get_user_documents_dir(user_id: int, tenant_id: Optional[int] = None) -> Path:
    base = Path(os.getenv("USER_DOC_UPLOAD_DIR", "uploads/user-documents"))
    if tenant_id is not None:
        base = base / str(tenant_id)
    return base / str(user_id)


def _build_user_document_url(
    filename: str,
    user_id: int,
    tenant_id: Optional[int] = None,
) -> str:
    base_url = os.getenv("USER_DOC_BASE_URL")
    if base_url:
        if tenant_id is not None:
            return f"{base_url.rstrip('/')}/{tenant_id}/{user_id}/{filename}"
        return f"{base_url.rstrip('/')}/{user_id}/{filename}"
    storage_path = os.getenv("USER_DOC_PUBLIC_PATH", "/uploads/user-documents")
    if tenant_id is not None:
        return f"{storage_path.rstrip('/')}/{tenant_id}/{user_id}/{filename}"
    return f"{storage_path.rstrip('/')}/{user_id}/{filename}"


def _get_receiving_support_dir(
    lot_id: int,
    tenant_id: Optional[int] = None,
) -> Path:
    base = Path(os.getenv("RECEIVING_SUPPORT_UPLOAD_DIR", "uploads/receiving-support"))
    if tenant_id is not None:
        base = base / str(tenant_id)
    return base / str(lot_id)

def get_receiving_support_dir(
    lot_id: int,
    tenant_id: Optional[int] = None,
) -> Path:
    return _get_receiving_support_dir(lot_id, tenant_id=tenant_id)


def _build_receiving_support_url(
    filename: str,
    lot_id: int,
    tenant_id: Optional[int] = None,
) -> str:
    base_url = os.getenv("RECEIVING_SUPPORT_BASE_URL")
    if base_url:
        if tenant_id is not None:
            return f"{base_url.rstrip('/')}/{tenant_id}/{lot_id}/{filename}"
        return f"{base_url.rstrip('/')}/{lot_id}/{filename}"
    storage_path = os.getenv("RECEIVING_SUPPORT_PUBLIC_PATH", "/uploads/receiving-support")
    if tenant_id is not None:
        return f"{storage_path.rstrip('/')}/{tenant_id}/{lot_id}/{filename}"
    return f"{storage_path.rstrip('/')}/{lot_id}/{filename}"


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
        raise ValueError("La imagen supera los 10MB permitidos")

    filename = f"{uuid4().hex}{extension}"

    base_dir = _get_base_dir(tenant_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_public_url(filename, tenant_id)
    return StoredProductImage(filename=filename, url=url, thumb_url=url)


async def save_product_video(
    file: UploadFile,
    tenant_id: Optional[int] = None,
) -> StoredProductVideo:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa MP4.")

    contents = await file.read()
    if len(contents) > MAX_VIDEO_SIZE:
        raise ValueError("El video supera los 20MB permitidos")

    filename = f"{uuid4().hex}{extension}"
    base_dir = _get_product_video_dir(tenant_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_product_video_public_url(filename, tenant_id)
    return StoredProductVideo(filename=filename, url=url, size_bytes=len(contents))


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


async def save_user_avatar(
    file: UploadFile,
    tenant_id: Optional[int] = None,
) -> StoredAvatar:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in AVATAR_ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa JPG, PNG o WEBP.")

    contents = await file.read()
    if len(contents) > MAX_AVATAR_SIZE:
        raise ValueError("La imagen supera los 2MB permitidos")

    filename = f"user-avatar-{uuid4().hex}{extension}"
    base_dir = _get_avatar_dir(tenant_id=tenant_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_avatar_url(filename, tenant_id=tenant_id)
    return StoredAvatar(filename=filename, url=url)


async def save_user_document(
    file: UploadFile,
    user_id: int,
    tenant_id: Optional[int] = None,
) -> StoredUserDocument:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in DOC_ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa PDF, JPG, PNG, WEBP o DOC/DOCX.")

    contents = await file.read()
    if len(contents) > MAX_DOC_SIZE:
        raise ValueError("El archivo supera los 5MB permitidos")

    filename = f"user-doc-{uuid4().hex}{extension}"
    base_dir = _get_user_documents_dir(user_id, tenant_id=tenant_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_user_document_url(filename, user_id, tenant_id=tenant_id)
    return StoredUserDocument(filename=original_name, url=url, size=len(contents))


async def save_receiving_support_file(
    file: UploadFile,
    lot_id: int,
    tenant_id: Optional[int] = None,
) -> StoredReceivingSupportFile:
    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in DOC_ALLOWED_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa PDF, JPG, PNG, WEBP o DOC/DOCX.")

    contents = await file.read()
    if len(contents) > MAX_DOC_SIZE:
        raise ValueError("El archivo supera los 5MB permitidos")

    filename = f"receiving-support-{uuid4().hex}{extension}"
    base_dir = _get_receiving_support_dir(lot_id, tenant_id=tenant_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    file_path = base_dir / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    url = _build_receiving_support_url(filename, lot_id, tenant_id=tenant_id)
    return StoredReceivingSupportFile(filename=original_name, url=url, size=len(contents))
