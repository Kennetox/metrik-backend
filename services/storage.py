import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import UploadFile

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
MAX_VIDEO_SIZE = 500 * 1024 * 1024  # 500MB input, streamed to disk before compression
MAX_HOME_VIDEO_SIZE = 90 * 1024 * 1024  # Practical limit below the production proxy ceiling
MAX_VIDEO_DURATION_SECONDS = 45
MAX_HOME_VIDEO_DURATION_SECONDS = 3 * 60
VIDEO_UPLOAD_CHUNK_SIZE = 1024 * 1024
VIDEO_PROCESS_TIMEOUT_SECONDS = 10 * 60
LOGO_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".svg"}
MAX_LOGO_SIZE = 1 * 1024 * 1024  # 1MB
AVATAR_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2MB
DOC_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx"}
MAX_DOC_SIZE = 5 * 1024 * 1024  # 5MB

logger = logging.getLogger(__name__)


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
    duration_seconds: int


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: int
    video_codec: str
    audio_codec: Optional[str]
    width: int
    height: int
    fps: float
    bitrate: int
    pixel_format: str


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


def _parse_frame_rate(value: object) -> float:
    raw = str(value or "0").strip()
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
        return float(raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _probe_video_metadata(ffprobe_bin: str, file_path: Path) -> VideoMetadata:
    process = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    if process.returncode != 0:
        raise ValueError("No se pudo leer la información técnica del video.")
    try:
        payload = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("No se pudo leer la información técnica del video.") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else []
    streams = streams if isinstance(streams, list) else []
    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    if not isinstance(video_stream, dict):
        raise ValueError("El archivo no contiene una pista de video válida.")

    format_data = payload.get("format") if isinstance(payload, dict) else {}
    format_data = format_data if isinstance(format_data, dict) else {}
    try:
        duration_seconds = int(round(float(format_data.get("duration") or 0)))
    except (TypeError, ValueError):
        duration_seconds = 0
    try:
        bitrate = int(video_stream.get("bit_rate") or format_data.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0

    return VideoMetadata(
        duration_seconds=duration_seconds,
        video_codec=str(video_stream.get("codec_name") or "").lower(),
        audio_codec=(
            str(audio_stream.get("codec_name") or "").lower()
            if isinstance(audio_stream, dict)
            else None
        ),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=_parse_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        bitrate=bitrate,
        pixel_format=str(video_stream.get("pix_fmt") or "").lower(),
    )


def _is_web_ready_home_video(metadata: VideoMetadata) -> bool:
    return bool(
        metadata.video_codec == "h264"
        and metadata.audio_codec in {None, "aac"}
        and 0 < metadata.width <= 720
        and 0 < metadata.height <= 1280
        and 0 < metadata.fps <= 30.5
        and 0 < metadata.bitrate <= 1_500_000
        and metadata.pixel_format in {"yuv420p", "yuvj420p"}
    )


def _run_video_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=VIDEO_PROCESS_TIMEOUT_SECONDS,
    )


async def save_product_video(
    file: UploadFile,
    tenant_id: Optional[int] = None,
    compression_profile: str = "product",
) -> StoredProductVideo:
    is_home_video = compression_profile == "home"
    profile_name = "home" if is_home_video else "product"
    original_name = Path(file.filename or "").name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError("Formato no soportado. Usa MP4, MOV, M4V, WebM, AVI o MKV.")

    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        raise ValueError("No hay soporte de compresión de video disponible en el servidor.")

    max_input_size = MAX_HOME_VIDEO_SIZE if is_home_video else MAX_VIDEO_SIZE
    max_input_mb = max_input_size // (1024 * 1024)
    logger.info(
        "video_processing_started profile=%s tenant_id=%s filename=%s",
        profile_name,
        tenant_id,
        original_name,
    )

    with tempfile.TemporaryDirectory(prefix="metrik-video-") as temp_dir:
        temp_root = Path(temp_dir)
        input_path = temp_root / f"input{extension}"
        output_path = temp_root / "output.mp4"
        uploaded_size = 0
        with input_path.open("wb") as input_file:
            while chunk := await file.read(VIDEO_UPLOAD_CHUNK_SIZE):
                uploaded_size += len(chunk)
                if uploaded_size > max_input_size:
                    logger.warning(
                        "video_input_rejected profile=%s tenant_id=%s size_bytes=%s limit_bytes=%s",
                        profile_name,
                        tenant_id,
                        uploaded_size,
                        max_input_size,
                    )
                    raise ValueError(
                        f"El video supera los {max_input_mb}MB permitidos para procesar."
                    )
                input_file.write(chunk)

        if uploaded_size <= 0:
            raise ValueError("El archivo de video está vacío.")

        metadata = await asyncio.to_thread(_probe_video_metadata, ffprobe_bin, input_path)
        duration_seconds = metadata.duration_seconds
        if duration_seconds <= 0:
            raise ValueError("No se pudo leer la duración del video.")
        max_duration_seconds = (
            MAX_HOME_VIDEO_DURATION_SECONDS if is_home_video else MAX_VIDEO_DURATION_SECONDS
        )
        if duration_seconds > max_duration_seconds:
            if is_home_video:
                raise ValueError("El video de inicio no puede superar los 3 minutos.")
            raise ValueError(f"El video no puede superar los {MAX_VIDEO_DURATION_SECONDS} segundos.")

        use_fast_path = is_home_video and _is_web_ready_home_video(metadata)
        scale_filter = (
            "scale=w='min(720,iw)':h='min(1280,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
            if is_home_video
            else "scale=w='min(1280,iw)':h='min(1280,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        transcode_command = [
            ffmpeg_bin,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-vf",
            scale_filter,
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30" if is_home_video else "28",
            "-maxrate",
            "1200k" if is_home_video else "2500k",
            "-bufsize",
            "2400k" if is_home_video else "5000k",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "64k" if is_home_video else "96k",
            "-ac",
            "2",
            "-ar",
            "44100",
            str(output_path),
        ]
        remux_command = [
            ffmpeg_bin,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        processing_mode = "fast_remux" if use_fast_path else "transcode"
        logger.info(
            "video_encoding_started profile=%s tenant_id=%s mode=%s input_bytes=%s duration_seconds=%s",
            profile_name,
            tenant_id,
            processing_mode,
            uploaded_size,
            duration_seconds,
        )
        try:
            process = await asyncio.to_thread(
                _run_video_command,
                remux_command if use_fast_path else transcode_command,
            )
            if use_fast_path and (process.returncode != 0 or not output_path.exists()):
                logger.warning(
                    "video_fast_remux_failed profile=%s tenant_id=%s; falling_back_to_transcode",
                    profile_name,
                    tenant_id,
                )
                output_path.unlink(missing_ok=True)
                processing_mode = "transcode_fallback"
                process = await asyncio.to_thread(_run_video_command, transcode_command)
        except subprocess.TimeoutExpired as exc:
            logger.exception(
                "video_encoding_timeout profile=%s tenant_id=%s filename=%s",
                profile_name,
                tenant_id,
                original_name,
            )
            raise ValueError(
                "El procesamiento tardó demasiado. Optimiza el video antes de volver a subirlo."
            ) from exc

        if (process.returncode != 0 or not output_path.exists()) and not is_home_video:
            output_path.unlink(missing_ok=True)
            remux_process = await asyncio.to_thread(_run_video_command, remux_command)
            if remux_process.returncode != 0 or not output_path.exists():
                transcode_error = (process.stderr or "").strip().splitlines()[-4:]
                remux_error = (remux_process.stderr or "").strip().splitlines()[-4:]
                error_tail = "\n".join([*transcode_error, *remux_error]).strip()
                raise ValueError(
                    "No se pudo comprimir el video. Intenta con otro archivo MP4."
                    + (f" ({error_tail})" if error_tail else "")
                )

        if is_home_video and (process.returncode != 0 or not output_path.exists()):
            error_tail = (process.stderr or "").strip().splitlines()[-6:]
            logger.error(
                "video_encoding_failed profile=home tenant_id=%s filename=%s error=%s",
                tenant_id,
                original_name,
                " | ".join(error_tail),
            )
            raise ValueError(
                "No se pudo comprimir el video para web. Intenta con otro archivo."
                + (f" ({' '.join(error_tail)})" if error_tail else "")
            )

        output_size = output_path.stat().st_size
        if output_size <= 0:
            raise ValueError("No se pudo generar un video válido.")

        filename = f"{uuid4().hex}.mp4"
        base_dir = _get_product_video_dir(tenant_id)
        base_dir.mkdir(parents=True, exist_ok=True)
        file_path = base_dir / filename
        shutil.copyfile(output_path, file_path)

    logger.info(
        "video_processing_completed profile=%s tenant_id=%s mode=%s output_bytes=%s duration_seconds=%s",
        profile_name,
        tenant_id,
        processing_mode,
        output_size,
        duration_seconds,
    )
    url = _build_product_video_public_url(filename, tenant_id)
    return StoredProductVideo(
        filename=filename,
        url=url,
        size_bytes=output_size,
        duration_seconds=duration_seconds,
    )


async def save_home_video(
    file: UploadFile,
    tenant_id: Optional[int] = None,
) -> StoredProductVideo:
    return await save_product_video(
        file,
        tenant_id=tenant_id,
        compression_profile="home",
    )


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
