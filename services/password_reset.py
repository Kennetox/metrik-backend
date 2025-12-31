import os
import secrets
from datetime import datetime, timedelta


PASSWORD_RESET_TOKEN_TTL_SECONDS = int(os.getenv("PASSWORD_RESET_TOKEN_TTL", "3600"))
PASSWORD_RESET_URL = os.getenv("PASSWORD_RESET_URL", "https://app.tudominio.com/reset")


def generate_token_and_expiry() -> tuple[str, datetime]:
    token = secrets.token_urlsafe(48)
    expires_at = datetime.utcnow() + timedelta(seconds=PASSWORD_RESET_TOKEN_TTL_SECONDS)
    return token, expires_at


def build_reset_link(token: str) -> str:
    base = PASSWORD_RESET_URL.rstrip("/")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={token}"

