import os
import secrets
from datetime import datetime, timedelta


PASSWORD_RESET_TOKEN_TTL_SECONDS = int(os.getenv("PASSWORD_RESET_TOKEN_TTL", "3600"))

_reset_url = os.getenv("PASSWORD_RESET_URL")
if not _reset_url:
    base_url = os.getenv("APP_BASE_URL") or os.getenv("PUBLIC_APP_URL") or "https://app.tudominio.com"
    base_url = base_url.rstrip("/")
    if base_url.endswith("/reset"):
        _reset_url = base_url
    else:
        _reset_url = f"{base_url}/reset"

PASSWORD_RESET_URL = _reset_url


def generate_token_and_expiry() -> tuple[str, datetime]:
    token = secrets.token_urlsafe(48)
    expires_at = datetime.utcnow() + timedelta(seconds=PASSWORD_RESET_TOKEN_TTL_SECONDS)
    return token, expires_at


def build_reset_link(token: str) -> str:
    base = PASSWORD_RESET_URL.rstrip("/")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={token}"
