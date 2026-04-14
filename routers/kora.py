from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import models
from dependencies import (
    get_current_active_user,
    get_current_tenant_id,
    require_permission,
)


class KoraAskContext(BaseModel):
    topic: str | None = None
    path: str | None = None


class KoraAskRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    context: KoraAskContext | None = None


class KoraActionOut(BaseModel):
    label: str
    href: str | None = None


class KoraAskResponse(BaseModel):
    handled: bool
    answer: str
    source: Literal["rules-v2", "openai-v2"]
    confidence: float = Field(ge=0, le=1)
    actions: list[KoraActionOut] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    generated_at: datetime


router = APIRouter(
    prefix="/kora",
    tags=["kora"],
    dependencies=[Depends(require_permission("dashboard.view"))],
)


ALLOWED_HREFS = {
    "/dashboard",
    "/dashboard/reports",
    "/dashboard/reports/detailed",
    "/dashboard/sales",
    "/dashboard/products",
    "/dashboard/movements",
    "/dashboard/comercio-web",
    "/dashboard/settings",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no"}


def _normalize(value: str) -> str:
    return " ".join(
        (value or "")
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .split()
    )


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return 0.0
    if parsed > 1:
        return 1.0
    return parsed


def _sanitize_actions(raw: object) -> list[KoraActionOut]:
    if not isinstance(raw, list):
        return []
    actions: list[KoraActionOut] = []
    for item in raw[:4]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        href = str(item.get("href") or "").strip() or None
        if not label:
            continue
        if href and not any(href == allowed or href.startswith(f"{allowed}?") for allowed in ALLOWED_HREFS):
            href = None
        actions.append(KoraActionOut(label=label[:80], href=href))
    return actions


def _sanitize_suggestions(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    suggestions: list[str] = []
    for item in raw[:4]:
        text = str(item or "").strip()
        if not text:
            continue
        suggestions.append(text[:180])
    return suggestions


def _rules_response(query: str, normalized: str) -> KoraAskResponse:
    if len(query) < 3:
        return KoraAskResponse(
            handled=False,
            answer="Necesito un poco más de detalle para ayudarte mejor.",
            source="rules-v2",
            confidence=0.22,
            suggestions=[
                "¿Cómo crear un producto?",
                "¿Cuánto vendimos hoy?",
                "¿Cuál fue la última vez que vendimos cable?",
            ],
            generated_at=datetime.utcnow(),
        )

    if "devolucion" in normalized:
        return KoraAskResponse(
            handled=True,
            answer=(
                "Te puedo guiar con devoluciones en el historial de ventas: "
                "abre la venta, valida ítems y usa la opción de devolución confirmada."
            ),
            source="rules-v2",
            confidence=0.72,
            actions=[
                KoraActionOut(
                    label="Abrir historial de ventas",
                    href="/dashboard/sales",
                )
            ],
            suggestions=[
                "¿Qué ventas hubo hoy?",
                "¿Cuáles métodos de pago se usaron el 21 de febrero?",
            ],
            generated_at=datetime.utcnow(),
        )

    if "reporte" in normalized or "informe" in normalized:
        return KoraAskResponse(
            handled=True,
            answer=(
                "Puedo ayudarte con reportes rápidos o detallados. "
                "Si me dices periodo y métrica (ventas, método de pago, producto), te lo estructuro."
            ),
            source="rules-v2",
            confidence=0.66,
            actions=[
                KoraActionOut(label="Abrir Reportes", href="/dashboard/reports"),
                KoraActionOut(
                    label="Abrir Reporte detallado",
                    href="/dashboard/reports/detailed",
                ),
            ],
            suggestions=[
                "¿Cuánto más vendimos que el mes anterior hasta hoy?",
                "¿Cuál es el producto más vendido de este mes?",
            ],
            generated_at=datetime.utcnow(),
        )

    if (
        "producto" in normalized
        or "sku" in normalized
        or "codigo" in normalized
    ):
        return KoraAskResponse(
            handled=True,
            answer=(
                "Para consultas de producto, indícame código/SKU o el nombre. "
                "Ejemplo: 'producto código ABC123' o 'a qué grupo pertenece SKU 100045'."
            ),
            source="rules-v2",
            confidence=0.64,
            actions=[KoraActionOut(label="Abrir Productos", href="/dashboard/products")],
            suggestions=[
                "Producto código ABC123",
                "¿A qué grupo pertenece SKU 100045?",
            ],
            generated_at=datetime.utcnow(),
        )

    return KoraAskResponse(
        handled=False,
        answer=(
            "No encontré una respuesta precisa todavía. "
            "Si reformulas con periodo, métrica y entidad, te respondo mejor."
        ),
        source="rules-v2",
        confidence=0.31,
        suggestions=[
            "¿Cuánto más vendimos que el mes anterior hasta ahora?",
            "¿Qué métodos de pago se usaron el 21 de febrero?",
            "¿Cuál fue la última vez que vendimos cable?",
        ],
        generated_at=datetime.utcnow(),
    )


def _ask_openai(query: str, context: KoraAskContext | None, user: models.PosUser) -> KoraAskResponse | None:
    api_key = (os.getenv("KORA_OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    if not _env_bool("KORA_AI_ENABLED", True):
        return None

    model = (os.getenv("KORA_OPENAI_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = int(os.getenv("KORA_OPENAI_TIMEOUT_SECONDS", "12"))
    endpoint = os.getenv("KORA_OPENAI_ENDPOINT", "https://api.openai.com/v1/chat/completions").strip()

    system_prompt = (
        "Eres KORA, asistente operativo de Metrik POS.\n"
        "Responde SIEMPRE en JSON válido sin texto adicional, con esta forma:\n"
        "{"
        "\"handled\": boolean, "
        "\"answer\": string, "
        "\"confidence\": number, "
        "\"actions\": [{\"label\": string, \"href\": string | null}], "
        "\"suggestions\": [string]"
        "}\n"
        "Reglas:\n"
        "- Español claro, breve y profesional.\n"
        "- No inventes datos numéricos.\n"
        "- Si falta contexto, handled=false y sugiere reformulaciones.\n"
        "- Solo usa href dentro de rutas dashboard internas.\n"
        "- Máximo 4 actions y 4 suggestions."
    )
    user_prompt = (
        f"Usuario: {user.name or 'Operador'}\n"
        f"Contexto: topic={context.topic if context else ''}, path={context.path if context else ''}\n"
        f"Consulta: {query}\n"
        "Devuelve solo JSON."
    )

    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None

    handled = bool(parsed.get("handled"))
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        return None

    return KoraAskResponse(
        handled=handled,
        answer=answer[:1200],
        source="openai-v2",
        confidence=_safe_float(parsed.get("confidence"), 0.4),
        actions=_sanitize_actions(parsed.get("actions")),
        suggestions=_sanitize_suggestions(parsed.get("suggestions")),
        generated_at=datetime.utcnow(),
    )


@router.post("/ask", response_model=KoraAskResponse)
def ask_kora(
    payload: KoraAskRequest,
    _tenant_id: int = Depends(get_current_tenant_id),
    user: models.PosUser = Depends(get_current_active_user),
):
    query = payload.query.strip()
    normalized = _normalize(query)
    rules = _rules_response(query, normalized)
    ai_min_conf = _safe_float(os.getenv("KORA_AI_MIN_CONFIDENCE", "0.58"), 0.58)

    ai = _ask_openai(query, payload.context, user)
    if not ai:
        return rules

    if ai.handled and ai.confidence >= ai_min_conf:
        return ai

    if rules.handled:
        return rules

    if ai.handled and ai.confidence < ai_min_conf:
        return KoraAskResponse(
            handled=False,
            answer="Puedo ayudarte, pero necesito una reformulación un poco más específica para darte una respuesta confiable.",
            source="openai-v2",
            confidence=ai.confidence,
            actions=ai.actions,
            suggestions=ai.suggestions
            or rules.suggestions,
            generated_at=datetime.utcnow(),
        )

    return ai
