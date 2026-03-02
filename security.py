import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Dict

PASSWORD_ITERATIONS = 120_000
SECRET_KEY = os.getenv("POS_SECRET_KEY", "kensar-pos-secret-change-me")
POS_TOKEN_TTL_SECONDS = int(os.getenv("POS_TOKEN_TTL", 60 * 60 * 12))
WEB_TOKEN_TTL_SECONDS = int(os.getenv("WEB_TOKEN_TTL", 60 * 60 * 12))
WEB_INACTIVITY_TIMEOUT_SECONDS = int(os.getenv("WEB_INACTIVITY_TIMEOUT", 3 * 60 * 60))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"{_b64encode(salt)}.{_b64encode(digest)}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        salt_b64, digest_b64 = hashed.split(".")
        salt = _b64decode(salt_b64)
        expected_digest = _b64decode(digest_b64)
    except Exception:
        # Hash corrupto o de un formato anterior: tratamos como credencial inválida.
        return False

    new_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return hmac.compare_digest(new_digest, expected_digest)


def create_access_token(
    user_id: int, role: str, ttl: int = POS_TOKEN_TTL_SECONDS
) -> str:
    payload = {
        "sub": user_id,
        "role": role,
        "exp": int(time.time()) + ttl,
    }
    serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(serialized)
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    token = f"{payload_b64}.{_b64encode(signature)}"
    return token


def verify_access_token(token: str) -> Dict:
    try:
        payload_b64, signature_b64 = token.split(".")
    except ValueError:
        raise ValueError("Token inválido")

    expected_signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    provided_signature = _b64decode(signature_b64)

    if not hmac.compare_digest(expected_signature, provided_signature):
        raise ValueError("Token inválido")

    payload_bytes = _b64decode(payload_b64)
    payload = json.loads(payload_bytes.decode("utf-8"))

    if payload.get("exp", 0) < int(time.time()):
        raise ValueError("Token expirado")

    return payload
