from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import math
import os
import re
import secrets
import string
import unicodedata
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from sqlalchemy import String, and_, case, cast, false, func, not_, or_, text, true
from sqlalchemy.exc import IntegrityError

from sqlalchemy.orm import Session, selectinload, joinedload

import models, schemas
from services import permissions
from services import tenant_modules
from security import hash_password, verify_password


def _clean_field(value):
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    return value


def _env_int(name: str, default: int, *, min_value: int = 0, max_value: int = 60 * 24 * 365) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except Exception:
        return default
    return max(min_value, min(max_value, parsed))


def _web_order_pending_expiry_minutes() -> int:
    # Regla operativa: no permitir ventanas de espera > 30 min para órdenes web.
    return _env_int("WEB_ORDER_PENDING_EXPIRY_MINUTES", 30, min_value=1, max_value=30)


def _web_order_reuse_window_minutes() -> int:
    return _env_int("WEB_ORDER_REUSE_WINDOW_MINUTES", 24 * 60)


CHECKOUT_CONTEXT_NOTE_MARKER = "CHECKOUT_CONTEXT_JSON:"
_WEB_BEST_SELLERS_CACHE: Dict[str, tuple[datetime, List[schemas.WebCatalogProductCard], datetime]] = {}


def _web_best_sellers_cache_ttl_seconds() -> int:
    return _env_int("WEB_BEST_SELLERS_CACHE_TTL_SECONDS", 12 * 60 * 60, min_value=300, max_value=7 * 24 * 60 * 60)


def _strip_checkout_context_note_segment(notes: Optional[str]) -> Optional[str]:
    raw_notes = (notes or "").strip()
    if not raw_notes:
        return None
    marker_index = raw_notes.find(CHECKOUT_CONTEXT_NOTE_MARKER)
    if marker_index < 0:
        return raw_notes
    clean_notes = raw_notes[:marker_index].strip()
    return clean_notes or None


def _sanitize_checkout_context_in_notes_for_backoffice(notes: Optional[str]) -> Optional[str]:
    raw_notes = (notes or "").strip()
    if not raw_notes:
        return None
    marker_index = raw_notes.find(CHECKOUT_CONTEXT_NOTE_MARKER)
    if marker_index < 0:
        return raw_notes
    note_text = raw_notes[:marker_index].strip()
    context_raw = raw_notes[marker_index + len(CHECKOUT_CONTEXT_NOTE_MARKER) :].strip()
    if not context_raw:
        return raw_notes
    try:
        parsed = json.loads(context_raw)
    except Exception:
        return raw_notes
    if not isinstance(parsed, dict):
        return raw_notes
    personalization = parsed.get("personalization")
    if not isinstance(personalization, dict):
        return raw_notes
    if "preview_images" not in personalization:
        return raw_notes

    sanitized_personalization = dict(personalization)
    sanitized_personalization.pop("preview_images", None)
    sanitized_context = dict(parsed)
    sanitized_context["personalization"] = sanitized_personalization
    context_json = json.dumps(sanitized_context, ensure_ascii=False, separators=(",", ":"))
    if note_text:
        return f"{note_text}\n\n{CHECKOUT_CONTEXT_NOTE_MARKER}{context_json}"
    return f"{CHECKOUT_CONTEXT_NOTE_MARKER}{context_json}"


def _normalize_web_order_item_signature_value(value: Any, *, digits: int) -> float:
    return round(float(value or 0.0), digits)


def build_web_order_item_signature(items: Sequence[Any]) -> tuple[tuple[int, float, float, float], ...]:
    rows: list[tuple[int, float, float, float]] = []
    for item in (items or []):
        if isinstance(item, dict):
            product_id = item.get("product_id")
            quantity = item.get("quantity")
            unit_price = item.get("unit_price_snapshot", item.get("unit_price"))
            line_total = item.get("line_total")
        else:
            product_id = getattr(item, "product_id", None)
            quantity = getattr(item, "quantity", None)
            unit_price = getattr(item, "unit_price_snapshot", getattr(item, "unit_price", None))
            line_total = getattr(item, "line_total", None)
        try:
            pid = int(product_id)
        except Exception:
            continue
        rows.append(
            (
                pid,
                _normalize_web_order_item_signature_value(quantity, digits=4),
                _normalize_web_order_item_signature_value(unit_price, digits=2),
                _normalize_web_order_item_signature_value(line_total, digits=2),
            )
        )
    rows.sort(key=lambda row: (row[0], row[2], row[1], row[3]))
    return tuple(rows)


def _money_eq(left: Any, right: Any) -> bool:
    return abs(round(float(left or 0.0), 2) - round(float(right or 0.0), 2)) <= 0.01


DEFAULT_PRODUCT_LABEL_FORMAT = "Kensar1"
CABLES_PRODUCT_LABEL_FORMAT = "Cables_1"


def _parse_product_gallery_urls(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return []
        try:
            parsed = json.loads(text_value)
        except Exception:
            return []
        value = parsed
    if not isinstance(value, list):
        return []
    clean: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized and normalized not in clean:
            clean.append(normalized)
    return clean[:5]


def _build_product_gallery_urls(product: models.Product) -> list[str]:
    urls = _parse_product_gallery_urls(getattr(product, "web_gallery_urls", None))
    for candidate in [product.image_url, product.image_thumb_url]:
        if isinstance(candidate, str):
            normalized = candidate.strip()
            if normalized and normalized not in urls:
                urls.append(normalized)
    return urls[:5]


def _is_cables_group(group_name: Optional[str]) -> bool:
    if not isinstance(group_name, str):
        return False
    normalized = group_name.strip().lower()
    if not normalized:
        return False
    return "cables" in normalized


def resolve_product_label_format(
    *,
    group_name: Optional[str],
    label_format: Optional[str] = None,
) -> str:
    manual = (label_format or "").strip()
    if manual:
        return manual
    if _is_cables_group(group_name):
        return CABLES_PRODUCT_LABEL_FORMAT
    return DEFAULT_PRODUCT_LABEL_FORMAT


def _normalize_label(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _slugify_text(value: Optional[str]) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def build_product_web_slug(name: Optional[str], sku: Optional[str] = None) -> str:
    base = _slugify_text(name)
    if base:
        return base[:160]
    fallback = _slugify_text(sku)
    return (fallback or "producto")[:160]


DEFAULT_WEB_CATALOG_CATEGORIES = [
    {"key": "audio-profesional", "name": "Audio profesional", "sort_order": 10},
    {"key": "instrumentos", "name": "Instrumentos", "sort_order": 20},
    {"key": "microfonos", "name": "Microfonos", "sort_order": 30},
    {"key": "accesorios", "name": "Accesorios", "sort_order": 40},
    {"key": "camaras", "name": "Camaras", "sort_order": 50},
]
DEFAULT_WEB_DESCRIPTION_TEMPLATE_CLOSING = (
    "En Kensar te asesoramos para elegir el producto adecuado segun tu necesidad. "
    "Contactanos por WhatsApp para mas informacion."
)
DEFAULT_WEB_DESCRIPTION_TEMPLATES = [
    {
        "template_key": "sonido",
        "label": "Sonido",
        "assigned_category_key": None,
        "keywords": [
            "sonido",
            "amplificador",
            "cabina",
            "car audio",
            "consola",
            "megafono",
            "microfono",
            "parlante",
        ],
        "paragraph1": "El [NOMBRE] es una excelente opcion para sistemas de sonido que requieren un uso practico y funcional.",
        "paragraph2": "Ideal para eventos, DJs, instalaciones o uso profesional y domestico, ofrece un rendimiento estable y confiable en diferentes entornos.",
        "paragraph3": "Su diseno permite una integracion practica dentro de configuraciones de audio, facilitando un sonido claro y equilibrado segun la necesidad.",
        "closing": "En Kensar te asesoramos para elegir el equipo adecuado segun tu uso. Contactanos por WhatsApp para mas informacion.",
        "sort_order": 10,
    },
    {
        "template_key": "studio",
        "label": "Studio",
        "assigned_category_key": None,
        "keywords": ["studio", "estudio", "monitoreo", "grabacion", "mezcla"],
        "paragraph1": "El [NOMBRE] es una excelente opcion para entornos de grabacion, monitoreo o produccion de audio.",
        "paragraph2": "Ideal para estudios, creadores de contenido y musicos, permite trabajar con mayor control y precision en el sonido.",
        "paragraph3": "Su diseno esta orientado a ofrecer un rendimiento confiable en procesos de monitoreo, grabacion o mezcla.",
        "closing": "En Kensar te asesoramos para elegir el equipo adecuado segun tu proyecto. Contactanos por WhatsApp para mas informacion.",
        "sort_order": 20,
    },
    {
        "template_key": "cables_accesorios",
        "label": "Cables y Accesorios",
        "assigned_category_key": None,
        "keywords": [
            "cable",
            "accesorio",
            "audio profesional",
            "cables de red",
            "hdmi",
            "rca",
            "tripode",
            "conector",
        ],
        "paragraph1": "El [NOMBRE] es una solucion practica para conexiones y configuraciones de audio, video o red.",
        "paragraph2": "Ideal para instalaciones profesionales o uso domestico, permite una conexion estable y funcional entre dispositivos.",
        "paragraph3": "Su diseno facilita el correcto funcionamiento de tus equipos en diferentes entornos.",
        "closing": "En Kensar te asesoramos para elegir el accesorio adecuado segun tu necesidad. Contactanos por WhatsApp para mas informacion.",
        "sort_order": 30,
    },
    {
        "template_key": "hogar_entretenimiento",
        "label": "Hogar y Entretenimiento",
        "assigned_category_key": None,
        "keywords": [
            "hogar",
            "entretenimiento",
            "televisor",
            "camara de seguridad",
            "seguridad",
            "luz solar",
        ],
        "paragraph1": "El [NOMBRE] es una excelente opcion para mejorar la experiencia en el hogar o espacios personales.",
        "paragraph2": "Ideal para entretenimiento, seguridad o uso diario, ofrece un funcionamiento practico y adaptable a diferentes necesidades.",
        "paragraph3": "Su diseno esta pensado para integrarse facilmente en distintos entornos, brindando comodidad y funcionalidad.",
        "closing": "En Kensar te asesoramos para elegir la mejor opcion segun tu espacio. Contactanos por WhatsApp para mas informacion.",
        "sort_order": 40,
    },
    {
        "template_key": "instrumentos",
        "label": "Instrumentos Musicales",
        "assigned_category_key": None,
        "keywords": ["instrumento", "cuerda", "viento", "salsero", "salsa", "percusion", "teclado"],
        "paragraph1": "El [NOMBRE] es una excelente opcion para quienes buscan un instrumento versatil y funcional.",
        "paragraph2": "Ideal para aprendizaje, practica o presentaciones, ofrece una experiencia comoda y un sonido adecuado segun su uso.",
        "paragraph3": "Su diseno permite un manejo practico, adaptandose a distintos niveles de experiencia.",
        "closing": "En Kensar te asesoramos para elegir el instrumento adecuado segun tu necesidad. Contactanos por WhatsApp para mas informacion.",
        "sort_order": 50,
    },
    {
        "template_key": "instrumentos_latinos",
        "label": "Instrumentos Latinos/Percusion",
        "assigned_category_key": None,
        "keywords": ["salsero", "salsa", "percusion", "conga", "bongo", "timbal"],
        "paragraph1": "El [NOMBRE] es una excelente opcion para ritmos latinos y acompanamientos percusivos.",
        "paragraph2": "Ideal para ensayos, presentaciones y uso musical, ofrece una respuesta sonora clara y facil ejecucion.",
        "paragraph3": "Fabricado para un uso practico, permite integrarse en diferentes estilos y configuraciones musicales.",
        "closing": "En Kensar te asesoramos para elegir el instrumento adecuado segun tu necesidad. Contactanos por WhatsApp para mas informacion.",
        "sort_order": 60,
    },
    {
        "template_key": "default",
        "label": "General",
        "assigned_category_key": None,
        "keywords": [],
        "paragraph1": "El [NOMBRE] es una opcion funcional para quienes buscan un producto confiable segun su necesidad.",
        "paragraph2": "Ideal para uso diario, profesional o tecnico segun su aplicacion, permite una implementacion practica en distintos entornos.",
        "paragraph3": "Su configuracion ofrece una solucion estable para tareas de conexion, operacion o soporte de equipos.",
        "closing": DEFAULT_WEB_DESCRIPTION_TEMPLATE_CLOSING,
        "sort_order": 999,
    },
]
WEB_PRICE_SOURCE_DEFAULT = "base"
WEB_PRICE_SOURCE_FIXED = "fixed"
WEB_PRICE_SOURCE_DISCOUNT_PERCENT = "discount_percent"
WEB_PRICE_SOURCE_OPTIONS = {
    WEB_PRICE_SOURCE_DEFAULT,
    WEB_PRICE_SOURCE_FIXED,
    WEB_PRICE_SOURCE_DISCOUNT_PERCENT,
}


def _normalize_web_price_source(value: Optional[str], *, strict: bool = False) -> str:
    normalized = (value or WEB_PRICE_SOURCE_DEFAULT).strip().lower()
    if normalized not in WEB_PRICE_SOURCE_OPTIONS:
        if strict:
            raise ValueError("Origen de precio web inválido")
        return WEB_PRICE_SOURCE_DEFAULT
    return normalized


def resolve_web_product_sale_price(product: models.Product) -> float:
    base_price = float(product.price or 0.0)
    source = _normalize_web_price_source(getattr(product, "web_price_source", None))
    value = float(getattr(product, "web_price_value", 0.0) or 0.0)
    if source == WEB_PRICE_SOURCE_FIXED:
        return round(max(0.0, value), 2)
    if source == WEB_PRICE_SOURCE_DISCOUNT_PERCENT:
        discount_percent = min(max(value, 0.0), 100.0)
        return round(max(0.0, base_price * (1.0 - (discount_percent / 100.0))), 2)
    return round(max(0.0, base_price), 2)


def resolve_web_compare_price(
    product: models.Product,
    *,
    sale_price: Optional[float] = None,
) -> Optional[float]:
    compare = getattr(product, "web_compare_price", None)
    if compare is None:
        return None
    normalized = float(compare)
    target_price = float(sale_price) if sale_price is not None else resolve_web_product_sale_price(product)
    if normalized <= target_price:
        return None
    return normalized


def _build_web_sale_price_sql_expression():
    source_col = func.lower(
        func.trim(func.coalesce(models.Product.web_price_source, WEB_PRICE_SOURCE_DEFAULT))
    )
    value_col = func.coalesce(models.Product.web_price_value, 0.0)
    base_col = func.coalesce(models.Product.price, 0.0)
    return case(
        (source_col == WEB_PRICE_SOURCE_FIXED, value_col),
        (
            source_col == WEB_PRICE_SOURCE_DISCOUNT_PERCENT,
            base_col * (1.0 - (value_col / 100.0)),
        ),
        else_=base_col,
    )


def _normalize_web_catalog_category_key(value: Optional[str]) -> str:
    normalized = _slugify_text(value or "")
    return normalized[:64]


def _humanize_web_catalog_category_key(value: str) -> str:
    chunks = [chunk for chunk in (value or "").replace("_", "-").split("-") if chunk]
    if not chunks:
        return "Categoria"
    return " ".join(chunk.capitalize() for chunk in chunks)


def _seed_default_web_catalog_categories(
    db: Session,
    *,
    tenant_id: Optional[int],
) -> None:
    existing_count = (
        db.query(func.count(models.WebCatalogCategory.id))
        .filter(models.WebCatalogCategory.tenant_id == tenant_id)
        .scalar()
    )
    now = datetime.utcnow()
    if int(existing_count or 0) == 0:
        rows = [
            models.WebCatalogCategory(
                tenant_id=tenant_id,
                key=item["key"],
                parent_key=None,
                name=item["name"],
                sort_order=int(item["sort_order"]),
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            for item in DEFAULT_WEB_CATALOG_CATEGORIES
        ]
        db.add_all(rows)
        db.commit()

    # Backfill any legacy keys already assigned to products but missing in category table.
    used_rows = (
        db.query(models.Product.web_category_key)
        .filter(
            models.Product.tenant_id == tenant_id if tenant_id is not None else true(),
            models.Product.web_category_key.isnot(None),
            func.trim(models.Product.web_category_key) != "",
        )
        .group_by(models.Product.web_category_key)
        .all()
    )
    existing_keys = {
        _normalize_web_catalog_category_key(item.key)
        for item in (
            db.query(models.WebCatalogCategory)
            .filter(models.WebCatalogCategory.tenant_id == tenant_id)
            .all()
        )
    }
    next_order_base = 1000
    pending_rows: list[models.WebCatalogCategory] = []
    for index, (raw_key,) in enumerate(used_rows):
        normalized_key = _normalize_web_catalog_category_key(raw_key)
        if not normalized_key or normalized_key in existing_keys:
            continue
        pending_rows.append(
            models.WebCatalogCategory(
                tenant_id=tenant_id,
                key=normalized_key,
                parent_key=None,
                name=_humanize_web_catalog_category_key(normalized_key),
                sort_order=next_order_base + index,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        existing_keys.add(normalized_key)
    if pending_rows:
        db.add_all(pending_rows)
        db.commit()


def _get_tenant_web_catalog_categories(
    db: Session,
    *,
    tenant_id: Optional[int],
    include_inactive: bool = False,
    ensure_seeded: bool = True,
) -> list[models.WebCatalogCategory]:
    if ensure_seeded:
        _seed_default_web_catalog_categories(db, tenant_id=tenant_id)
    query = db.query(models.WebCatalogCategory).filter(models.WebCatalogCategory.tenant_id == tenant_id)
    if not include_inactive:
        query = query.filter(models.WebCatalogCategory.is_active.is_(True))
    return (
        query.order_by(
            case((models.WebCatalogCategory.parent_key.is_(None), 0), else_=1).asc(),
            models.WebCatalogCategory.parent_key.asc(),
            models.WebCatalogCategory.sort_order.asc(),
            models.WebCatalogCategory.name.asc(),
            models.WebCatalogCategory.id.asc(),
        )
        .all()
    )


def _get_tenant_web_catalog_category_map(
    db: Session,
    *,
    tenant_id: Optional[int],
    include_inactive: bool = True,
    ensure_seeded: bool = True,
) -> dict[str, models.WebCatalogCategory]:
    rows = _get_tenant_web_catalog_categories(
        db,
        tenant_id=tenant_id,
        include_inactive=include_inactive,
        ensure_seeded=ensure_seeded,
    )
    mapping: dict[str, models.WebCatalogCategory] = {}
    for item in rows:
        normalized = _normalize_web_catalog_category_key(item.key)
        if normalized:
            mapping[normalized] = item
    return mapping


def _build_web_catalog_category_children_map(
    rows: Sequence[models.WebCatalogCategory],
) -> dict[Optional[str], list[str]]:
    children: dict[Optional[str], list[str]] = defaultdict(list)
    for item in rows:
        key = _normalize_web_catalog_category_key(item.key)
        if not key:
            continue
        parent_key = _normalize_web_catalog_category_key(item.parent_key)
        parent_ref = parent_key or None
        children[parent_ref].append(key)
    return children


def _get_web_catalog_category_level(
    key: str,
    category_map: dict[str, models.WebCatalogCategory],
) -> int:
    level = 1
    current_key = key
    visited: set[str] = set()
    while True:
        row = category_map.get(current_key)
        if row is None:
            return level
        parent_key = _normalize_web_catalog_category_key(row.parent_key)
        if not parent_key:
            return level
        if parent_key in visited:
            return level
        visited.add(parent_key)
        level += 1
        current_key = parent_key


def _get_web_catalog_descendant_keys(
    root_key: str,
    children_map: dict[Optional[str], list[str]],
) -> set[str]:
    descendants: set[str] = set()
    queue: list[str] = [root_key]
    while queue:
        current = queue.pop(0)
        if current in descendants:
            continue
        descendants.add(current)
        queue.extend(children_map.get(current, []))
    return descendants


def _is_leaf_web_catalog_category(
    key: str,
    children_map: dict[Optional[str], list[str]],
) -> bool:
    return len(children_map.get(key, [])) == 0


def resolve_web_catalog_category_label(
    db: Session,
    *,
    tenant_id: Optional[int],
    category_key: Optional[str],
) -> Optional[str]:
    normalized = _normalize_web_catalog_category_key(category_key)
    if not normalized:
        return None
    category = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=tenant_id,
        include_inactive=True,
        ensure_seeded=True,
    ).get(normalized)
    return category.name if category else None


def _should_sync_company_name_from_tenant(
    company_name: Optional[str],
    previous_tenant_name: Optional[str],
) -> bool:
    normalized_company = _normalize_label(company_name)
    if not normalized_company:
        return True
    if normalized_company in {"mi negocio", "mi empresa"}:
        return True
    previous_normalized = _normalize_label(previous_tenant_name)
    return bool(previous_normalized and normalized_company == previous_normalized)


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_tenant_by_slug(db: Session, slug: str) -> Optional[models.Tenant]:
    return (
        db.query(models.Tenant)
        .filter(models.Tenant.slug == slug)
        .first()
    )


def get_platform_user_by_email(db: Session, email: str) -> Optional[models.PlatformUser]:
    return (
        db.query(models.PlatformUser)
        .filter(func.lower(models.PlatformUser.email) == email.lower())
        .first()
    )


def get_platform_user(db: Session, user_id: int) -> Optional[models.PlatformUser]:
    return (
        db.query(models.PlatformUser)
        .filter(models.PlatformUser.id == user_id)
        .first()
    )


def create_platform_user(
    db: Session,
    *,
    email: str,
    password: str,
    name: str,
) -> models.PlatformUser:
    existing = get_platform_user_by_email(db, email)
    if existing:
        raise ValueError("Ya existe un usuario de plataforma con ese correo")
    user = models.PlatformUser(
        email=email.strip().lower(),
        name=name.strip(),
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def ensure_platform_user(
    db: Session,
    *,
    email: str,
    password: str,
    name: str,
) -> models.PlatformUser:
    existing = get_platform_user_by_email(db, email)
    if existing:
        return existing
    return create_platform_user(db, email=email, password=password, name=name)


def get_default_tenant_id(db: Session) -> Optional[int]:
    tenant = get_tenant_by_slug(db, "kensar")
    return tenant.id if tenant else None


def resolve_user_tenant_id(db: Session, user: Optional[models.PosUser]) -> Optional[int]:
    if user is None:
        return None
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        return None
    return int(tenant_id)


def list_tenants(db: Session) -> list[models.Tenant]:
    return (
        db.query(models.Tenant)
        .order_by(models.Tenant.created_at.desc(), models.Tenant.id.desc())
        .all()
    )


def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    return (
        db.query(models.Tenant)
        .filter(models.Tenant.id == tenant_id)
        .first()
    )


def get_tenant_trial_days_remaining(tenant: Optional[models.Tenant]) -> Optional[int]:
    if not tenant or not tenant.trial_ends_at:
        return None
    delta = tenant.trial_ends_at - datetime.utcnow()
    return max(0, int((delta.total_seconds() + 86399) // 86400))


def get_tenant_enabled_modules(tenant: Optional[models.Tenant]) -> List[str]:
    if not tenant:
        return tenant_modules.normalize_enabled_modules(None)
    return tenant_modules.normalize_enabled_modules(tenant.enabled_modules)


def normalize_module_user_access(value: Optional[Dict[str, Any]]) -> Dict[str, List[int]]:
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, List[int]] = {}
    for module_id, raw_user_ids in value.items():
        if not isinstance(module_id, str):
            continue
        module_key = module_id.strip()
        if not module_key:
            continue
        if module_key not in tenant_modules.MODULE_IDS:
            continue
        if not isinstance(raw_user_ids, list):
            continue
        seen: set[int] = set()
        cleaned_ids: List[int] = []
        for raw_user_id in raw_user_ids:
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            if user_id <= 0 or user_id in seen:
                continue
            seen.add(user_id)
            cleaned_ids.append(user_id)
        normalized[module_key] = cleaned_ids
    return normalized


def can_user_access_tenant_module(
    tenant: Optional[models.Tenant],
    module_id: str,
    user: Optional[models.PosUser] = None,
) -> bool:
    enabled_modules = get_tenant_enabled_modules(tenant)
    if module_id not in enabled_modules:
        return False
    if not tenant:
        return True
    access_map = normalize_module_user_access(getattr(tenant, "module_user_access", None))
    allowed_ids = access_map.get(module_id)
    if not allowed_ids:
        return True
    if not user:
        return False
    return int(user.id) in set(allowed_ids)


def build_tenant_module_access_map(
    tenant: Optional[models.Tenant],
    user: Optional[models.PosUser],
) -> Dict[str, bool]:
    enabled_modules = get_tenant_enabled_modules(tenant)
    return {
        module_id: can_user_access_tenant_module(tenant, module_id, user=user)
        for module_id in enabled_modules
    }


def build_tenant_session_read(
    tenant: Optional[models.Tenant],
    user: Optional[models.PosUser] = None,
) -> Optional[schemas.TenantSessionRead]:
    if not tenant:
        return None
    return schemas.TenantSessionRead(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        lifecycle_stage=(tenant.lifecycle_stage or "active"),
        trial_started_at=tenant.trial_started_at,
        trial_ends_at=tenant.trial_ends_at,
        trial_days_remaining=get_tenant_trial_days_remaining(tenant),
        enabled_modules=get_tenant_enabled_modules(tenant),
        module_access=build_tenant_module_access_map(tenant, user),
    )


def get_tenant_access_issue(tenant: Optional[models.Tenant]) -> Optional[str]:
    if not tenant or not tenant.is_active:
        return "Empresa inválida o inactiva"
    stage = tenant.lifecycle_stage or "active"
    if stage == "suspended":
        return "Esta empresa está suspendida. Contáctanos para reactivar el acceso."
    if stage == "inactive":
        return "Esta empresa está inactiva. Contáctanos para reactivar el acceso."
    if stage == "archived":
        return "Esta empresa fue archivada y ya no está disponible."
    if stage == "demo" and tenant.trial_ends_at and tenant.trial_ends_at < datetime.utcnow():
        return "Tu demo expiró. Contáctanos para activar tu empresa."
    return None


def record_demo_signup_audit(
    db: Session,
    *,
    tenant_id: Optional[int],
    email: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> models.DemoSignupAudit:
    row = models.DemoSignupAudit(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        ip_address=_clean_field(ip_address),
        user_agent=_clean_field(user_agent),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_recent_demo_signups_by_email(
    db: Session,
    email: str,
    *,
    since: datetime,
) -> list[models.DemoSignupAudit]:
    return (
        db.query(models.DemoSignupAudit)
        .filter(func.lower(models.DemoSignupAudit.email) == email.lower())
        .filter(models.DemoSignupAudit.created_at >= since)
        .order_by(models.DemoSignupAudit.created_at.desc())
        .all()
    )


def count_recent_demo_signups_by_ip(
    db: Session,
    ip_address: str,
    *,
    since: datetime,
) -> int:
    return int(
        db.query(func.count(models.DemoSignupAudit.id))
        .filter(models.DemoSignupAudit.ip_address == ip_address)
        .filter(models.DemoSignupAudit.created_at >= since)
        .scalar()
        or 0
    )


def get_active_demo_by_email(
    db: Session,
    email: str,
) -> Optional[models.PosUser]:
    user = get_pos_user_by_email_global(db, email)
    if not user or not user.tenant_id:
        return None
    tenant = get_tenant(db, int(user.tenant_id))
    if not tenant:
        return None
    if tenant.lifecycle_stage != "demo":
        return None
    if tenant.trial_ends_at and tenant.trial_ends_at < datetime.utcnow():
        return None
    return user


def get_tenant_primary_admin(
    db: Session,
    tenant_id: int,
) -> Optional[models.PosUser]:
    admin = (
        db.query(models.PosUser)
        .filter(models.PosUser.tenant_id == tenant_id)
        .filter(models.PosUser.role == "Administrador")
        .order_by(models.PosUser.created_at.asc(), models.PosUser.id.asc())
        .first()
    )
    if admin:
        return admin
    return (
        db.query(models.PosUser)
        .filter(models.PosUser.tenant_id == tenant_id)
        .order_by(models.PosUser.created_at.asc(), models.PosUser.id.asc())
        .first()
    )


def get_tenant_company_settings(
    db: Session,
    tenant_id: int,
) -> Optional[models.PosSettings]:
    return (
        db.query(models.PosSettings)
        .filter(models.PosSettings.tenant_id == tenant_id)
        .order_by(models.PosSettings.id.asc())
        .first()
    )


_DEFAULT_WEB_PERSONALIZATION_BINDINGS: dict[str, dict[str, str]] = {
    "campana_clasica_mediana": {},
    "campana_clasica_grande": {},
    "campana_cromada_mediana": {},
    "campana_cromada_grande": {},
    "guiro_mediano": {},
    "guiro_grande": {},
    "maraca_par": {},
}


def _normalize_web_personalization_bindings(value: Any) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {
        key: dict(default_value) for key, default_value in _DEFAULT_WEB_PERSONALIZATION_BINDINGS.items()
    }
    if not isinstance(value, dict):
        return result
    for variant_key in result.keys():
        row = value.get(variant_key)
        if not isinstance(row, dict):
            continue
        normalized: dict[str, str] = {}
        for field in (
            "product_id",
            "product_sku",
            "product_name",
            "product_slug",
            "service_id",
            "service_sku",
            "service_name",
        ):
            raw = row.get(field)
            normalized[field] = str(raw).strip() if raw is not None else ""
        result[variant_key] = normalized
    return result


def get_public_web_personalization_bindings(
    db: Session,
    tenant_id: Optional[int] = None,
) -> dict[str, dict[str, str]]:
    effective_tenant_id = tenant_id if tenant_id is not None else resolve_public_catalog_tenant_id(db)
    settings = get_pos_settings(db, tenant_id=effective_tenant_id)
    return _normalize_web_personalization_bindings(settings.web_personalization_bindings)


def build_platform_tenant_read(
    db: Session,
    tenant: models.Tenant,
) -> schemas.PlatformTenantRead:
    admin_user = get_tenant_primary_admin(db, tenant.id)
    settings = get_tenant_company_settings(db, tenant.id)
    return schemas.PlatformTenantRead(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        is_active=tenant.is_active,
        lifecycle_stage=(tenant.lifecycle_stage or "active"),
        trial_started_at=tenant.trial_started_at,
        trial_ends_at=tenant.trial_ends_at,
        converted_at=tenant.converted_at,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        trial_days_remaining=get_tenant_trial_days_remaining(tenant),
        enabled_modules=get_tenant_enabled_modules(tenant),
        module_user_access=normalize_module_user_access(tenant.module_user_access),
        module_catalog=tenant_modules.get_tenant_module_catalog(),
        admin_user=(
            schemas.PlatformTenantAdminRead(
                id=admin_user.id,
                name=admin_user.name,
                email=admin_user.email,
                phone=admin_user.phone,
                status=admin_user.status,
                created_at=admin_user.created_at,
            )
            if admin_user
            else None
        ),
        company_details=(
            schemas.PlatformTenantCompanyRead(
                company_name=settings.company_name or tenant.name,
                tax_id=settings.tax_id,
                address=settings.address,
                contact_email=settings.contact_email,
                contact_phone=settings.contact_phone,
            )
            if settings
            else None
        ),
    )


def list_platform_tenant_reads(db: Session) -> list[schemas.PlatformTenantRead]:
    tenants = list_tenants(db)
    return [build_platform_tenant_read(db, tenant) for tenant in tenants]


def list_platform_tenant_users(
    db: Session,
    tenant_id: int,
) -> list[models.PosUser]:
    return (
        db.query(models.PosUser)
        .filter(models.PosUser.tenant_id == tenant_id)
        .order_by(models.PosUser.created_at.asc(), models.PosUser.id.asc())
        .all()
    )


def create_tenant_with_admin(
    db: Session,
    payload: schemas.PlatformTenantCreateRequest,
) -> tuple[models.Tenant, models.PosUser]:
    slug = payload.slug.strip().lower()
    name = payload.name.strip()

    existing_tenant = get_tenant_by_slug(db, slug)
    if existing_tenant:
        raise ValueError("Ya existe una empresa con ese slug")

    existing_user = (
        db.query(models.PosUser)
        .filter(func.lower(models.PosUser.email) == payload.admin_email.lower())
        .first()
    )
    if existing_user:
        raise ValueError("Ya existe un usuario con ese correo")

    tenant = models.Tenant(
        slug=slug,
        name=name,
        is_active=True,
        lifecycle_stage="active",
        enabled_modules=tenant_modules.normalize_enabled_modules([]),
        module_user_access={},
    )
    db.add(tenant)
    db.flush()

    admin_user = models.PosUser(
        tenant_id=tenant.id,
        name=payload.admin_name.strip(),
        email=payload.admin_email.strip().lower(),
        role="Administrador",
        status="Activo",
        is_active=True,
        password_hash=hash_password(payload.admin_password),
        phone=_clean_field(payload.admin_phone),
    )
    db.add(admin_user)

    settings = models.PosSettings(
        tenant_id=tenant.id,
        company_name=name,
    )
    db.add(settings)

    db.commit()
    db.refresh(tenant)
    db.refresh(admin_user)
    return tenant, admin_user


def update_tenant(
    db: Session,
    tenant: models.Tenant,
    payload: schemas.PlatformTenantUpdateRequest,
) -> models.Tenant:
    previous_tenant_name = (tenant.name or "").strip()
    data = payload.model_dump(exclude_unset=True)
    name_changed = False
    if "name" in data and data["name"] is not None:
        tenant.name = data["name"].strip()
        name_changed = True
    if "is_active" in data and data["is_active"] is not None:
        tenant.is_active = bool(data["is_active"])
    if "lifecycle_stage" in data and data["lifecycle_stage"] is not None:
        tenant.lifecycle_stage = data["lifecycle_stage"]
        if tenant.lifecycle_stage == "active":
            tenant.converted_at = tenant.converted_at or datetime.utcnow()
    if "enabled_modules" in data and data["enabled_modules"] is not None:
        tenant.enabled_modules = tenant_modules.normalize_enabled_modules(
            data["enabled_modules"]
        )
    if "module_user_access" in data and data["module_user_access"] is not None:
        incoming_access = normalize_module_user_access(data["module_user_access"])
        tenant_user_ids = {
            int(row.id)
            for row in db.query(models.PosUser.id)
            .filter(models.PosUser.tenant_id == tenant.id)
            .all()
        }
        sanitized_access: Dict[str, List[int]] = {}
        for module_id, user_ids in incoming_access.items():
            sanitized_ids = [user_id for user_id in user_ids if user_id in tenant_user_ids]
            sanitized_access[module_id] = sanitized_ids
        tenant.module_user_access = sanitized_access

    if name_changed:
        settings = (
            db.query(models.PosSettings)
            .filter(models.PosSettings.tenant_id == tenant.id)
            .first()
        )
        if settings:
            if _should_sync_company_name_from_tenant(
                settings.company_name,
                previous_tenant_name,
            ):
                settings.company_name = tenant.name
        else:
            db.add(
                models.PosSettings(
                    tenant_id=tenant.id,
                    company_name=tenant.name,
                )
            )

    db.commit()
    db.refresh(tenant)
    return tenant


def create_demo_tenant_with_admin(
    db: Session,
    payload: schemas.DemoStartRequest,
) -> tuple[models.Tenant, models.PosUser]:
    name = payload.company_name.strip()
    slug = re.sub(r"[^a-z0-9-]+", "-", payload.company_name.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")[:64] or "demo"
    base_slug = slug
    suffix = 2
    while get_tenant_by_slug(db, slug):
        slug = f"{base_slug[: max(1, 64 - len(str(suffix)) - 1)]}-{suffix}"
        suffix += 1

    existing_user = (
        db.query(models.PosUser)
        .filter(func.lower(models.PosUser.email) == payload.admin_email.lower())
        .first()
    )
    if existing_user:
        raise ValueError("Ya existe un usuario con ese correo")

    now = datetime.utcnow()
    tenant = models.Tenant(
        slug=slug,
        name=name,
        is_active=True,
        lifecycle_stage="demo",
        trial_started_at=now,
        trial_ends_at=now + timedelta(days=7),
        enabled_modules=tenant_modules.normalize_enabled_modules([]),
        module_user_access={},
    )
    db.add(tenant)
    db.flush()

    admin_user = models.PosUser(
        tenant_id=tenant.id,
        name=payload.admin_name.strip(),
        email=payload.admin_email.strip().lower(),
        role="Administrador",
        status="Activo",
        is_active=True,
        password_hash=hash_password(payload.password),
        phone=_clean_field(payload.admin_phone),
    )
    db.add(admin_user)
    db.add(
        models.PosSettings(
            tenant_id=tenant.id,
            company_name=name,
            contact_email=payload.admin_email.strip().lower(),
            contact_phone=_clean_field(payload.company_phone) or _clean_field(payload.admin_phone),
            address=_clean_field(payload.company_city),
            ticket_footer=(
                f"Demo {payload.business_type.strip()}"
                if payload.business_type and payload.business_type.strip()
                else None
            ),
        )
    )
    db.commit()
    db.refresh(tenant)
    db.refresh(admin_user)
    return tenant, admin_user


def extend_tenant_trial(
    db: Session,
    tenant: models.Tenant,
    *,
    extra_days: int,
) -> models.Tenant:
    base = tenant.trial_ends_at if tenant.trial_ends_at and tenant.trial_ends_at > datetime.utcnow() else datetime.utcnow()
    tenant.trial_ends_at = base + timedelta(days=extra_days)
    tenant.trial_started_at = tenant.trial_started_at or datetime.utcnow()
    tenant.lifecycle_stage = "demo"
    tenant.is_active = True
    db.commit()
    db.refresh(tenant)
    return tenant


def convert_tenant_to_active(db: Session, tenant: models.Tenant) -> models.Tenant:
    tenant.lifecycle_stage = "active"
    tenant.is_active = True
    tenant.converted_at = datetime.utcnow()
    db.commit()
    db.refresh(tenant)
    return tenant


def _build_proportional_payments(
    total_amount: float,
    source_payments: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    if total_amount <= 0:
        return []

    normalized: list[tuple[str, float]] = []
    for method, amount in source_payments:
        method_name = (method or "").strip()
        numeric = float(amount or 0.0)
        if not method_name or numeric <= 0:
            continue
        normalized.append((method_name, numeric))

    if not normalized:
        return [("cash", total_amount)]

    if len(normalized) == 1:
        return [(normalized[0][0], total_amount)]

    source_total = sum(amount for _, amount in normalized)
    if source_total <= 0:
        return [(normalized[0][0], total_amount)]

    distributed: list[tuple[str, float]] = []
    allocated = 0.0
    for method, amount in normalized[:-1]:
        proportional = round((amount / source_total) * total_amount, 2)
        distributed.append((method, proportional))
        allocated += proportional

    tail = round(total_amount - allocated, 2)
    if tail < 0:
        tail = 0.0
    distributed.append((normalized[-1][0], tail))

    # Ajuste final para evitar desfaces por redondeo.
    diff = round(total_amount - sum(amount for _, amount in distributed), 2)
    if abs(diff) > 0.001:
        method, amount = distributed[-1]
        distributed[-1] = (method, round(max(0.0, amount + diff), 2))

    # Si algun renglón quedó en 0 por redondeo, lo excluimos.
    filtered = [(method, amount) for method, amount in distributed if amount > 0]
    return filtered if filtered else [(normalized[0][0], total_amount)]


def create_receiving_lot(
    db: Session,
    payload: schemas.ReceivingLotCreate,
    created_by_user_id: int | None = None,
    stock_device_name: str | None = None,
) -> models.ReceivingLot:
    effective_tenant_id = get_default_tenant_id(db)
    if created_by_user_id:
        creator = db.query(models.PosUser).filter(models.PosUser.id == created_by_user_id).first()
        if creator:
            effective_tenant_id = resolve_user_tenant_id(db, creator)
    source_reference = _clean_field(payload.source_reference)
    supplier_name = _clean_field(payload.supplier_name)
    invoice_reference = _clean_field(payload.invoice_reference)
    if payload.purchase_type == "invoice" and not source_reference:
        source_reference = invoice_reference
    lot = models.ReceivingLot(
        tenant_id=effective_tenant_id,
        purchase_type=payload.purchase_type,
        origin_name=payload.origin_name.strip(),
        stock_device_id=_clean_field(payload.stock_device_id),
        stock_device_name=_clean_field(stock_device_name),
        source_reference=source_reference,
        supplier_name=supplier_name,
        invoice_reference=invoice_reference,
        notes=_clean_field(payload.notes),
        status="open",
        created_by_user_id=created_by_user_id,
    )
    db.add(lot)
    db.flush()

    if not lot.lot_number:
        lot.lot_number = f"RC-{lot.id:06d}"

    db.commit()
    db.refresh(lot)
    return lot


def acquire_receiving_lot_creation_lock(db: Session) -> None:
    backend = db.bind.dialect.name if db.bind and db.bind.dialect else ""
    if backend == "postgresql":
        # Serializa creación de lotes para evitar carreras web/app.
        db.execute(text("SELECT pg_advisory_xact_lock(91827432)"))


def get_receiving_lot(
    db: Session,
    lot_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.ReceivingLot]:
    query = db.query(models.ReceivingLot).filter(models.ReceivingLot.id == lot_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ReceivingLot.tenant_id == effective_tenant_id)
    return query.first()


def get_receiving_lot_by_number(
    db: Session,
    lot_number: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.ReceivingLot]:
    query = db.query(models.ReceivingLot).filter(models.ReceivingLot.lot_number == lot_number)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ReceivingLot.tenant_id == effective_tenant_id)
    return query.first()


def list_receiving_lots(
    db: Session,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
    tenant_id: Optional[int] = None,
) -> List[models.ReceivingLot]:
    query = db.query(models.ReceivingLot)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ReceivingLot.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.ReceivingLot.status == status)
    return (
        query.order_by(models.ReceivingLot.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def count_receiving_lots(
    db: Session,
    status: str | None = None,
    tenant_id: Optional[int] = None,
) -> int:
    query = db.query(func.count(models.ReceivingLot.id))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ReceivingLot.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.ReceivingLot.status == status)
    return int(query.scalar() or 0)


def list_receiving_lot_items(
    db: Session,
    lot_id: int,
    tenant_id: Optional[int] = None,
) -> List[models.ReceivingLotItem]:
    query = db.query(models.ReceivingLotItem).filter(models.ReceivingLotItem.lot_id == lot_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ReceivingLotItem.tenant_id == effective_tenant_id)
    return (
        query.order_by(models.ReceivingLotItem.created_at.asc(), models.ReceivingLotItem.id.asc())
        .all()
    )


def get_receiving_lot_item(
    db: Session,
    lot_id: int,
    item_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.ReceivingLotItem]:
    query = (
        db.query(models.ReceivingLotItem)
        .filter(
            models.ReceivingLotItem.id == item_id,
            models.ReceivingLotItem.lot_id == lot_id,
        )
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ReceivingLotItem.tenant_id == effective_tenant_id)
    return query.first()


def search_receiving_products(
    db: Session,
    q: str,
    skip: int = 0,
    limit: int = 20,
    include_inactive: bool = False,
    tenant_id: Optional[int] = None,
) -> List[models.Product]:
    query = db.query(models.Product)
    if not include_inactive:
        query = query.filter(models.Product.active.is_(True))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        normalized_term = term.lower()
        normalized_term_no_zeros = normalized_term.lstrip("0")
        barcode_expr = func.lower(func.coalesce(models.Product.barcode, ""))
        sku_expr = func.lower(func.coalesce(models.Product.sku, ""))
        name_expr = func.lower(func.coalesce(models.Product.name, ""))
        barcode_no_zeros_expr = func.ltrim(barcode_expr, "0")
        query = query.filter(
            or_(
                models.Product.name.ilike(like),
                models.Product.sku.ilike(like),
                models.Product.barcode.ilike(like),
            )
        )
        exact_barcode_rank = case((barcode_expr == normalized_term, 0), else_=1)
        exact_barcode_no_zeros_rank = case(
            (
                and_(
                    normalized_term_no_zeros != "",
                    barcode_no_zeros_expr == normalized_term_no_zeros,
                ),
                0,
            ),
            else_=1,
        )
        exact_sku_rank = case((sku_expr == normalized_term, 0), else_=1)
        barcode_prefix_rank = case((barcode_expr.like(f"{normalized_term}%"), 0), else_=1)
        sku_prefix_rank = case((sku_expr.like(f"{normalized_term}%"), 0), else_=1)
        name_prefix_rank = case((name_expr.like(f"{normalized_term}%"), 0), else_=1)
        query = query.order_by(
            exact_barcode_rank.asc(),
            exact_barcode_no_zeros_rank.asc(),
            exact_sku_rank.asc(),
            barcode_prefix_rank.asc(),
            sku_prefix_rank.asc(),
            name_prefix_rank.asc(),
            models.Product.name.asc(),
            models.Product.id.asc(),
        )
    else:
        query = query.order_by(models.Product.name.asc(), models.Product.id.asc())

    return query.offset(skip).limit(limit).all()


def resolve_receiving_product_by_barcode(
    db: Session,
    scan_code: str,
    *,
    include_inactive: bool = False,
    tenant_id: Optional[int] = None,
) -> Optional[models.Product]:
    normalized = "".join(ch for ch in (scan_code or "").strip().lower() if ch.isprintable() and not ch.isspace())
    if normalized.startswith("]") and len(normalized) >= 3 and normalized[1].isalnum() and normalized[2].isalnum():
        normalized = normalized[3:]
    if not normalized:
        return None

    normalized_no_zeros = normalized.lstrip("0")
    query = db.query(models.Product)
    if not include_inactive:
        query = query.filter(models.Product.active.is_(True))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)

    barcode_expr = func.lower(func.coalesce(models.Product.barcode, ""))
    barcode_no_zeros_expr = func.ltrim(barcode_expr, "0")
    filters = [barcode_expr == normalized]
    if normalized_no_zeros:
        filters.append(barcode_no_zeros_expr == normalized_no_zeros)

    exact_query = (
        query.filter(or_(*filters))
        .order_by(
            case((barcode_expr == normalized, 0), else_=1).asc(),
            models.Product.name.asc(),
            models.Product.id.asc(),
        )
    )
    return exact_query.first()


def _next_numeric_code(values: List[str]) -> str:
    max_num = 0
    max_len = 1
    for raw in values:
        clean = (raw or "").strip()
        if not clean or not clean.isdigit():
            continue
        number = int(clean)
        if number > max_num:
            max_num = number
        if len(clean) > max_len:
            max_len = len(clean)

    next_num = max_num + 1
    next_str = str(next_num)
    if len(next_str) < max_len:
        return next_str.zfill(max_len)
    return next_str


def get_next_product_codes(
    db: Session,
    tenant_id: Optional[int] = None,
) -> tuple[str, str]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    sku_values = [
        row[0]
        for row in db.query(models.Product.sku)
        .filter(models.Product.sku.isnot(None))
        .filter(models.Product.tenant_id == effective_tenant_id if effective_tenant_id is not None else true())
        .all()
    ]
    barcode_values = [
        row[0]
        for row in db.query(models.Product.barcode)
        .filter(models.Product.barcode.isnot(None))
        .filter(models.Product.tenant_id == effective_tenant_id if effective_tenant_id is not None else true())
        .all()
    ]
    return _next_numeric_code(sku_values), _next_numeric_code(barcode_values)


def _acquire_product_codes_lock(db: Session) -> None:
    backend = db.bind.dialect.name if db.bind and db.bind.dialect else ""
    if backend == "postgresql":
        # Lock transaccional para serializar generación de SKU/barcode.
        db.execute(text("SELECT pg_advisory_xact_lock(91827431)"))


def create_receiving_product_quick(
    db: Session,
    payload: schemas.ReceivingProductQuickCreate,
    tenant_id: Optional[int] = None,
) -> models.Product:
    _acquire_product_codes_lock(db)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)

    for _ in range(8):
        next_sku, next_barcode = get_next_product_codes(db, tenant_id=effective_tenant_id)
        product_data = schemas.ProductCreate(
            sku=next_sku,
            name=payload.name.strip(),
            price=float(payload.price),
            cost=float(payload.cost if payload.cost is not None else 0),
            barcode=next_barcode,
            label_format=_clean_field(payload.label_format),
            unit=None,
            stock_min=0,
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
            allow_price_change=False,
            active=True,
            service=False,
            includes_tax=False,
            group_name=payload.group_name.strip(),
            brand=_clean_field(payload.brand),
            supplier=_clean_field(payload.supplier),
        )

        try:
            return create_product(db, product_data, tenant_id=effective_tenant_id)
        except IntegrityError:
            db.rollback()
            continue

    raise ValueError(
        "No se pudo generar SKU/código de barras únicos. Intenta de nuevo."
    )


def add_receiving_lot_item(
    db: Session,
    lot: models.ReceivingLot,
    product: models.Product,
    qty_received: float,
    unit_cost: float | None = None,
    notes: str | None = None,
) -> models.ReceivingLotItem:
    effective_tenant_id = lot.tenant_id if lot.tenant_id is not None else get_default_tenant_id(db)
    existing_query = (
        db.query(models.ReceivingLotItem)
        .filter(
            models.ReceivingLotItem.lot_id == lot.id,
            models.ReceivingLotItem.product_id == product.id,
        )
    )
    if effective_tenant_id is not None:
        existing_query = existing_query.filter(models.ReceivingLotItem.tenant_id == effective_tenant_id)
    existing = existing_query.first()

    if existing:
        if existing.tenant_id is None and effective_tenant_id is not None:
            existing.tenant_id = effective_tenant_id
        existing.qty_received = float(existing.qty_received or 0) + float(qty_received)
        if unit_cost is not None:
            existing.unit_cost_snapshot = float(unit_cost)
        existing.label_format_snapshot = resolve_product_label_format(
            group_name=product.group_name,
            label_format=product.label_format,
        )
        if notes is not None:
            existing.notes = _clean_field(notes)
        db.commit()
        db.refresh(existing)
        return existing

    item = models.ReceivingLotItem(
        tenant_id=effective_tenant_id,
        lot_id=lot.id,
        product_id=product.id,
        product_name_snapshot=product.name,
        sku_snapshot=product.sku,
        barcode_snapshot=product.barcode,
        label_format_snapshot=resolve_product_label_format(
            group_name=product.group_name,
            label_format=product.label_format,
        ),
        qty_received=float(qty_received),
        unit_cost_snapshot=float(unit_cost if unit_cost is not None else product.cost or 0),
        unit_price_snapshot=float(product.price or 0),
        is_new_product=False,
        notes=_clean_field(notes),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_receiving_lot_item(
    db: Session,
    item: models.ReceivingLotItem,
    qty_received: float,
    unit_cost: float | None = None,
    notes: str | None = None,
) -> models.ReceivingLotItem:
    item.qty_received = float(qty_received)
    expected_labels = max(0, int(math.ceil(float(item.qty_received or 0))))
    current_printed = max(0, int(item.labels_printed_qty or 0))
    if current_printed > expected_labels:
        item.labels_printed_qty = expected_labels
    if unit_cost is not None:
        item.unit_cost_snapshot = float(unit_cost)
    if notes is not None:
        item.notes = _clean_field(notes)
    db.commit()
    db.refresh(item)
    return item


def mark_receiving_lot_item_labels_printed(
    db: Session,
    item: models.ReceivingLotItem,
    copies: int,
) -> models.ReceivingLotItem:
    expected_labels = max(0, int(math.ceil(float(item.qty_received or 0))))
    current_printed = max(0, int(item.labels_printed_qty or 0))
    to_add = max(0, int(copies or 0))
    next_printed = min(expected_labels, current_printed + to_add)
    item.labels_printed_qty = next_printed
    db.commit()
    db.refresh(item)
    return item


def delete_receiving_lot_item(
    db: Session,
    item: models.ReceivingLotItem,
) -> None:
    db.delete(item)
    db.commit()


def close_receiving_lot(
    db: Session,
    lot: models.ReceivingLot,
    closed_by_user_id: int | None = None,
) -> models.ReceivingLot:
    effective_tenant_id = lot.tenant_id if lot.tenant_id is not None else get_default_tenant_id(db)

    items_query = db.query(models.ReceivingLotItem).filter(
        models.ReceivingLotItem.lot_id == lot.id
    )
    if effective_tenant_id is not None:
        items_query = items_query.filter(models.ReceivingLotItem.tenant_id == effective_tenant_id)
    items = items_query.all()

    # Proteccion contra duplicados: si ya existen movimientos del lote, no los recrea.
    existing_movements_query = db.query(models.InventoryMovement).filter(
        models.InventoryMovement.reference_type == "receiving_lot",
        models.InventoryMovement.reference_id == lot.id,
    )
    if effective_tenant_id is not None:
        existing_movements_query = existing_movements_query.filter(
            models.InventoryMovement.tenant_id == effective_tenant_id
        )
    has_existing_lot_movements = existing_movements_query.first() is not None

    if not has_existing_lot_movements and items:
        qty_by_product: dict[int, float] = defaultdict(float)
        name_by_product: dict[int, str] = {}
        for item in items:
            product_id = int(item.product_id)
            qty_by_product[product_id] += float(item.qty_received or 0.0)
            if product_id not in name_by_product:
                name_by_product[product_id] = item.product_name_snapshot or f"#{product_id}"

        for product_id, total_qty in qty_by_product.items():
            if total_qty <= 0:
                continue
            movement_notes_parts = [
                f"lote:{lot.lot_number or lot.id}",
                f"origen:{lot.origin_name}",
            ]
            if lot.purchase_type:
                movement_notes_parts.append(f"tipo:{lot.purchase_type}")
            if lot.supplier_name:
                movement_notes_parts.append(f"proveedor:{lot.supplier_name}")
            if lot.invoice_reference:
                movement_notes_parts.append(f"factura:{lot.invoice_reference}")

            movement = models.InventoryMovement(
                tenant_id=effective_tenant_id,
                product_id=product_id,
                qty_delta=abs(float(total_qty)),
                reason="purchase",
                notes=" | ".join(movement_notes_parts),
                reference_type="receiving_lot",
                reference_id=lot.id,
                created_by_user_id=closed_by_user_id,
            )
            db.add(movement)

    lot.status = "closed"
    lot.closed_by_user_id = closed_by_user_id
    lot.closed_at = datetime.utcnow()
    db.commit()
    db.refresh(lot)
    return lot


def update_receiving_lot(
    db: Session,
    lot: models.ReceivingLot,
    *,
    purchase_type: str,
    source_reference: str | None = None,
    supplier_name: str | None = None,
    invoice_reference: str | None = None,
    notes: str | None = None,
) -> models.ReceivingLot:
    lot.purchase_type = purchase_type
    lot.source_reference = _clean_field(source_reference)
    lot.supplier_name = _clean_field(supplier_name)
    lot.invoice_reference = _clean_field(invoice_reference)
    lot.notes = _clean_field(notes)
    if purchase_type == "invoice" and not lot.source_reference:
        lot.source_reference = lot.invoice_reference
    db.commit()
    db.refresh(lot)
    return lot


def update_receiving_lot_support_file(
    db: Session,
    lot: models.ReceivingLot,
    *,
    support_file_name: str | None,
    support_file_url: str | None,
    support_file_size: int | None,
) -> models.ReceivingLot:
    lot.support_file_name = _clean_field(support_file_name)
    lot.support_file_url = _clean_field(support_file_url)
    lot.support_file_size = support_file_size
    db.commit()
    db.refresh(lot)
    return lot


def cancel_receiving_lot(
    db: Session,
    lot: models.ReceivingLot,
) -> models.ReceivingLot:
    lot.status = "cancelled"
    db.commit()
    db.refresh(lot)
    return lot


def acquire_manual_movement_document_creation_lock(db: Session) -> None:
    backend = db.bind.dialect.name if db.bind and db.bind.dialect else ""
    if backend == "postgresql":
        # Serializa creación de documentos manuales para blindar numeración.
        db.execute(text("SELECT pg_advisory_xact_lock(91827433)"))


def _manual_movement_prefix(kind: str) -> str:
    return {
        "salida_manual": "SM",
        "venta_manual": "VM",
        "ajuste": "AJ",
        "perdida_dano": "PD",
    }.get(kind, "MM")


def _serialize_manual_header(header: Dict[str, Any] | None) -> str:
    clean = header or {}
    return json.dumps(clean, ensure_ascii=False)


def _deserialize_manual_header(raw: str | None) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def create_manual_movement_document(
    db: Session,
    payload: schemas.ManualMovementDocumentCreate,
    created_by_user_id: int | None = None,
    tenant_id: int | None = None,
) -> models.ManualMovementDocument:
    document = models.ManualMovementDocument(
        tenant_id=tenant_id if tenant_id is not None else get_default_tenant_id(db),
        kind=payload.kind,
        status="open",
        origin_name=(payload.origin_name or "Metrik web").strip() or "Metrik web",
        header_json=_serialize_manual_header(payload.header),
        notes=_clean_field(payload.notes),
        created_by_user_id=created_by_user_id,
    )
    db.add(document)
    db.flush()
    if not document.document_number:
        prefix = _manual_movement_prefix(document.kind)
        document.document_number = f"{prefix}-{document.id:06d}"
    db.commit()
    db.refresh(document)
    return document


def get_manual_movement_document(
    db: Session,
    document_id: int,
    tenant_id: int | None = None,
) -> models.ManualMovementDocument | None:
    query = db.query(models.ManualMovementDocument).filter(
        models.ManualMovementDocument.id == document_id
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ManualMovementDocument.tenant_id == effective_tenant_id)
    return query.first()


def list_manual_movement_documents(
    db: Session,
    *,
    status: str | None = None,
    kind: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    skip: int = 0,
    limit: int = 50,
    tenant_id: int | None = None,
) -> List[models.ManualMovementDocument]:
    query = db.query(models.ManualMovementDocument)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ManualMovementDocument.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.ManualMovementDocument.status == status)
    if kind:
        query = query.filter(models.ManualMovementDocument.kind == kind)
    order_col = (
        models.ManualMovementDocument.closed_at
        if status == "closed"
        else models.ManualMovementDocument.created_at
    )
    if date_from is not None:
        query = query.filter(order_col >= date_from)
    if date_to is not None:
        query = query.filter(order_col < date_to)
    return (
        query.order_by(order_col.desc(), models.ManualMovementDocument.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def count_manual_movement_documents(
    db: Session,
    *,
    status: str | None = None,
    kind: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    tenant_id: int | None = None,
) -> int:
    query = db.query(func.count(models.ManualMovementDocument.id))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ManualMovementDocument.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.ManualMovementDocument.status == status)
    if kind:
        query = query.filter(models.ManualMovementDocument.kind == kind)
    order_col = (
        models.ManualMovementDocument.closed_at
        if status == "closed"
        else models.ManualMovementDocument.created_at
    )
    if date_from is not None:
        query = query.filter(order_col >= date_from)
    if date_to is not None:
        query = query.filter(order_col < date_to)
    return int(query.scalar() or 0)


def list_manual_movement_document_lines(
    db: Session,
    document_id: int,
    tenant_id: int | None = None,
) -> List[models.ManualMovementDocumentLine]:
    query = db.query(models.ManualMovementDocumentLine).filter(
        models.ManualMovementDocumentLine.document_id == document_id
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ManualMovementDocumentLine.tenant_id == effective_tenant_id)
    return query.order_by(models.ManualMovementDocumentLine.id.asc()).all()


def update_manual_movement_document_header(
    db: Session,
    document: models.ManualMovementDocument,
    *,
    header: Dict[str, Any] | None = None,
    notes: str | None = None,
) -> models.ManualMovementDocument:
    document.header_json = _serialize_manual_header(header)
    document.notes = _clean_field(notes)
    db.commit()
    db.refresh(document)
    return document


def replace_manual_movement_document_lines(
    db: Session,
    document: models.ManualMovementDocument,
    lines_payload: List[schemas.ManualMovementDocumentLineInput],
    tenant_id: int | None = None,
) -> List[models.ManualMovementDocumentLine]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    db.query(models.ManualMovementDocumentLine).filter(
        models.ManualMovementDocumentLine.document_id == document.id
    ).delete(synchronize_session=False)

    if not lines_payload:
        db.commit()
        return []

    product_ids = [line.product_id for line in lines_payload]
    products = (
        db.query(models.Product)
        .filter(models.Product.id.in_(product_ids))
        .filter(models.Product.tenant_id == effective_tenant_id if effective_tenant_id is not None else true())
        .all()
    )
    products_by_id = {product.id: product for product in products}

    created_rows: List[models.ManualMovementDocumentLine] = []
    for line in lines_payload:
        product = products_by_id.get(line.product_id)
        if not product:
            continue
        row = models.ManualMovementDocumentLine(
            tenant_id=effective_tenant_id,
            document_id=document.id,
            product_id=product.id,
            product_name_snapshot=product.name,
            sku_snapshot=product.sku,
            barcode_snapshot=product.barcode,
            qty=float(line.qty),
            unit_cost_snapshot=float(line.unit_cost) if line.unit_cost is not None else None,
            unit_price_snapshot=float(line.unit_price) if line.unit_price is not None else None,
            notes=_clean_field(line.notes),
        )
        db.add(row)
        created_rows.append(row)
    db.commit()
    for row in created_rows:
        db.refresh(row)
    return created_rows


def cancel_manual_movement_document(
    db: Session,
    document: models.ManualMovementDocument,
) -> models.ManualMovementDocument:
    document.status = "cancelled"
    db.commit()
    db.refresh(document)
    return document


def close_manual_movement_document(
    db: Session,
    document: models.ManualMovementDocument,
    *,
    closed_by_user_id: int | None,
    external_reference_type: str | None = None,
    external_reference_id: int | None = None,
    tenant_id: int | None = None,
) -> models.ManualMovementDocument:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    lines = list_manual_movement_document_lines(db, document.id, tenant_id=effective_tenant_id)
    header = _deserialize_manual_header(document.header_json)

    if document.kind in {"salida_manual", "ajuste", "perdida_dano"}:
        for line in lines:
            qty = float(line.qty or 0.0)
            if qty <= 0:
                continue
            reason = "transfer_out"
            qty_delta = -abs(qty)
            if document.kind == "ajuste":
                reason = "adjustment"
                # En ajuste se permite positivo/negativo vía header.
                direction = (header.get("adjust_direction") or "in").strip().lower()
                qty_delta = abs(qty) if direction == "in" else -abs(qty)
            elif document.kind == "perdida_dano":
                damage_type = (header.get("damage_type") or "loss").strip().lower()
                reason = "damage" if damage_type == "damage" else "loss"

            note_parts = [
                f"Documento:{document.document_number or document.id}",
                f"Tipo:{document.kind}",
                document.notes or "",
                line.notes or "",
            ]
            movement = models.InventoryMovement(
                tenant_id=effective_tenant_id,
                product_id=line.product_id,
                qty_delta=qty_delta,
                reason=reason,
                notes=" | ".join(part for part in note_parts if part),
                reference_type=document.kind,
                reference_id=document.id,
                created_by_user_id=closed_by_user_id,
            )
            db.add(movement)

    document.status = "closed"
    document.closed_by_user_id = closed_by_user_id
    document.closed_at = datetime.utcnow()
    document.external_reference_type = _clean_field(external_reference_type)
    document.external_reference_id = external_reference_id
    db.commit()
    db.refresh(document)
    return document


def parse_manual_movement_header(document: models.ManualMovementDocument) -> Dict[str, Any]:
    return _deserialize_manual_header(document.header_json)


def revoke_user_sessions(
    db: Session,
    user_id: int,
    reason: str = "replaced",
    session_type: str | None = None,
) -> None:
    now = datetime.utcnow()
    query = db.query(models.PosSession).filter(
        models.PosSession.user_id == user_id,
        models.PosSession.revoked_at.is_(None),
    )
    if session_type:
        query = query.filter(models.PosSession.session_type == session_type)
    query.update(
        {
            models.PosSession.revoked_at: now,
            models.PosSession.revoked_reason: reason,
        },
        synchronize_session=False,
    )
    db.commit()


def create_pos_session(
    db: Session,
    user_id: int,
    token: str,
    session_type: str,
    expires_at: datetime,
    station_id: str | None = None,
    device_id: str | None = None,
) -> models.PosSession:
    user = db.query(models.PosUser).filter(models.PosUser.id == user_id).first()
    effective_tenant_id = (
        user.tenant_id if user and user.tenant_id is not None else get_default_tenant_id(db)
    )
    session = models.PosSession(
        tenant_id=effective_tenant_id,
        user_id=user_id,
        token_hash=_session_token_hash(token),
        session_type=session_type,
        station_id=station_id,
        device_id=device_id,
        created_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_session_by_token(db: Session, token: str) -> models.PosSession | None:
    token_hash = _session_token_hash(token)
    return (
        db.query(models.PosSession)
        .filter(models.PosSession.token_hash == token_hash)
        .first()
    )


def get_active_pos_session_conflict(
    db: Session,
    user_id: int,
    current_station_id: str | None = None,
    session_types: Sequence[str] | None = ("pos", "tablet"),
) -> models.PosSession | None:
    now = datetime.utcnow()
    query = db.query(models.PosSession).filter(
        models.PosSession.user_id == user_id,
        models.PosSession.revoked_at.is_(None),
        models.PosSession.expires_at > now,
    )
    if session_types:
        query = query.filter(models.PosSession.session_type.in_(list(session_types)))
    if current_station_id:
        query = query.filter(
            or_(
                models.PosSession.station_id.is_(None),
                models.PosSession.station_id != current_station_id,
            )
        )
    return query.order_by(models.PosSession.created_at.desc()).first()


# ===================== PRODUCTS =====================


def _normalized_text(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _weighted_quantile(values: List[float], weights: List[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total_weight = sum(max(0.0, w) for _, w in pairs)
    if total_weight <= 0:
        return float(pairs[len(pairs) // 2][0])
    target = min(max(q, 0.0), 1.0) * total_weight
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += max(0.0, weight)
        if cumulative >= target:
            return float(value)
    return float(pairs[-1][0])


def _compute_markup_stats(rows: List[dict[str, Any]]) -> Optional[dict[str, float]]:
    if not rows:
        return None
    values = [float(row["markup"]) for row in rows]
    weights = [float(row["weight"]) for row in rows]
    p25 = _weighted_quantile(values, weights, 0.25)
    p50 = _weighted_quantile(values, weights, 0.5)
    p75 = _weighted_quantile(values, weights, 0.75)
    iqr = p75 - p25
    lower = p25 - (1.5 * iqr)
    upper = p75 + (1.5 * iqr)
    trimmed = [
        row
        for row in rows
        if float(row["markup"]) >= lower
        and float(row["markup"]) <= upper
    ]
    if len(trimmed) < max(3, int(len(rows) * 0.4)):
        trimmed = rows
    values2 = [float(row["markup"]) for row in trimmed]
    weights2 = [float(row["weight"]) for row in trimmed]
    return {
        "p25": _weighted_quantile(values2, weights2, 0.25),
        "p50": _weighted_quantile(values2, weights2, 0.5),
        "p75": _weighted_quantile(values2, weights2, 0.75),
        "sample_size": float(len(trimmed)),
        "original_size": float(len(rows)),
    }


def suggest_product_cost(
    db: Session,
    *,
    tenant_id: Optional[int],
    price: float,
    group_name: Optional[str] = None,
    brand: Optional[str] = None,
    supplier: Optional[str] = None,
    exclude_product_id: Optional[int] = None,
    default_markup_percent: float = 50.0,
) -> schemas.ProductCostSuggestionResponse:
    safe_price = max(0.0, float(price or 0.0))
    if safe_price <= 0:
        raise ValueError("El precio debe ser mayor que cero para sugerir costo.")

    base_query = db.query(
        models.Product.id,
        models.Product.price,
        models.Product.cost,
        models.Product.group_name,
        models.Product.brand,
        models.Product.supplier,
        models.Product.updated_at,
    ).filter(
        models.Product.active.is_(True),
        models.Product.price > 0,
        models.Product.cost > 0,
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        base_query = base_query.filter(models.Product.tenant_id == effective_tenant_id)
    if exclude_product_id is not None:
        base_query = base_query.filter(models.Product.id != int(exclude_product_id))
    source_rows = base_query.limit(50000).all()

    now = datetime.utcnow()
    half_life_days = 180.0
    prepared: List[dict[str, Any]] = []
    for row in source_rows:
        cost = float(row.cost or 0.0)
        sale_price = float(row.price or 0.0)
        if cost <= 0 or sale_price <= 0:
            continue
        markup = ((sale_price - cost) / cost) * 100.0
        if markup < -95.0 or markup > 1500.0:
            continue
        updated_at = row.updated_at if isinstance(row.updated_at, datetime) else now
        age_days = max(0.0, (now - updated_at).total_seconds() / 86400.0)
        recency_weight = math.exp(-math.log(2) * (age_days / half_life_days))
        prepared.append(
            {
                "id": int(row.id),
                "group": _normalized_text(row.group_name),
                "brand": _normalized_text(row.brand),
                "supplier": _normalized_text(row.supplier),
                "markup": float(markup),
                "weight": max(0.05, recency_weight),
            }
        )

    target_group = _normalized_text(group_name)
    target_brand = _normalized_text(brand)
    target_supplier = _normalized_text(supplier)

    strategies: List[tuple[str, List[dict[str, Any]]]] = []
    if target_brand and target_group and target_supplier:
        strategies.append(
            (
                "brand_group_supplier",
                [
                    row
                    for row in prepared
                    if row["brand"] == target_brand
                    and row["group"] == target_group
                    and row["supplier"] == target_supplier
                ],
            )
        )
    if target_brand and target_group:
        strategies.append(
            (
                "brand_group",
                [
                    row
                    for row in prepared
                    if row["brand"] == target_brand and row["group"] == target_group
                ],
            )
        )
    if target_supplier and target_group:
        strategies.append(
            (
                "supplier_group",
                [
                    row
                    for row in prepared
                    if row["supplier"] == target_supplier and row["group"] == target_group
                ],
            )
        )
    if target_group:
        strategies.append(("group", [row for row in prepared if row["group"] == target_group]))
    if target_supplier:
        strategies.append(
            (
                "supplier",
                [row for row in prepared if row["supplier"] == target_supplier],
            )
        )
    strategies.append(("global", prepared))

    chosen_method = "default"
    chosen_stats: Optional[dict[str, float]] = None
    for method, rows in strategies:
        stats = _compute_markup_stats(rows)
        if not stats:
            continue
        if stats["sample_size"] >= 8:
            chosen_method = method
            chosen_stats = stats
            break
        if chosen_stats is None:
            chosen_method = method
            chosen_stats = stats

    if chosen_stats is None:
        chosen_markup = float(default_markup_percent)
        suggested_cost = safe_price / (1.0 + (chosen_markup / 100.0))
        return schemas.ProductCostSuggestionResponse(
            suggested_cost=round(max(0.0, suggested_cost), 2),
            range_min_cost=round(max(0.0, suggested_cost), 2),
            range_max_cost=round(max(0.0, suggested_cost), 2),
            confidence_score=0.2,
            confidence_label="baja",
            method="default",
            sample_size=0,
            markup_used=round(chosen_markup, 4),
            markup_p25=round(chosen_markup, 4),
            markup_p50=round(chosen_markup, 4),
            markup_p75=round(chosen_markup, 4),
            recency_half_life_days=int(half_life_days),
            notes="Sin muestra histórica válida; se usó markup por defecto.",
        )

    markup_p25 = float(chosen_stats["p25"])
    markup_p50 = float(chosen_stats["p50"])
    markup_p75 = float(chosen_stats["p75"])
    suggested_cost = safe_price / (1.0 + (markup_p50 / 100.0))
    range_min_cost = safe_price / (1.0 + (markup_p75 / 100.0))
    range_max_cost = safe_price / (1.0 + (markup_p25 / 100.0))

    sample_size = int(chosen_stats["sample_size"])
    confidence = min(0.95, 0.25 + min(sample_size, 80) / 100.0)
    dispersion = max(0.0, markup_p75 - markup_p25)
    confidence *= max(0.55, 1.0 - min(dispersion, 200.0) / 300.0)
    if chosen_method == "global":
        confidence *= 0.85
    confidence = max(0.2, min(0.95, confidence))

    if confidence >= 0.75:
        confidence_label = "alta"
    elif confidence >= 0.5:
        confidence_label = "media"
    else:
        confidence_label = "baja"

    method_labels = {
        "brand_group_supplier": "marca+grupo+proveedor",
        "brand_group": "marca+grupo",
        "supplier_group": "proveedor+grupo",
        "group": "grupo",
        "supplier": "proveedor",
        "global": "global",
        "default": "default",
    }
    return schemas.ProductCostSuggestionResponse(
        suggested_cost=round(max(0.0, suggested_cost), 2),
        range_min_cost=round(max(0.0, min(range_min_cost, range_max_cost)), 2),
        range_max_cost=round(max(0.0, max(range_min_cost, range_max_cost)), 2),
        confidence_score=round(confidence, 4),
        confidence_label=confidence_label,
        method=chosen_method,
        method_label=method_labels.get(chosen_method, chosen_method),
        sample_size=sample_size,
        markup_used=round(markup_p50, 4),
        markup_p25=round(markup_p25, 4),
        markup_p50=round(markup_p50, 4),
        markup_p75=round(markup_p75, 4),
        recency_half_life_days=int(half_life_days),
        notes="Sugerencia calculada con markups históricos, recencia y limpieza de outliers.",
    )


def get_products(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.Product)
    if tenant_id is not None:
        query = query.filter(models.Product.tenant_id == tenant_id)
    products = (
        query.order_by(models.Product.id.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    group_names = {p.group_name for p in products if p.group_name}
    group_map = {}
    if group_names:
        groups_query = db.query(models.ProductGroup).filter(models.ProductGroup.path.in_(group_names))
        if tenant_id is not None:
            groups_query = groups_query.filter(models.ProductGroup.tenant_id == tenant_id)
        groups = groups_query.all()
        group_map = {g.path: g for g in groups}

    for product in products:
        product.group_meta = group_map.get(product.group_name or "")

    return products


def _web_publication_sale_price_expression():
    price_source = func.lower(func.coalesce(models.Product.web_price_source, "base"))
    base_price = func.coalesce(models.Product.price, 0.0)
    price_value = func.coalesce(models.Product.web_price_value, 0.0)
    return case(
        (price_source == "fixed", price_value),
        (price_source == "discount_percent", base_price * (1 - (price_value / 100.0))),
        else_=base_price,
    )


def _web_publication_discounted_expression():
    return and_(
        func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "visible",
        models.Product.web_compare_price.is_not(None),
        models.Product.web_compare_price > _web_publication_sale_price_expression(),
    )


def _web_publication_configured_expression():
    has_non_empty_web_name = and_(
        models.Product.web_name.is_not(None),
        func.trim(models.Product.web_name) != "",
    )
    has_non_empty_web_category = and_(
        models.Product.web_category_key.is_not(None),
        func.trim(models.Product.web_category_key) != "",
    )
    has_non_empty_short_description = and_(
        models.Product.web_short_description.is_not(None),
        func.trim(models.Product.web_short_description) != "",
    )
    has_non_empty_long_description = and_(
        models.Product.web_long_description.is_not(None),
        func.trim(models.Product.web_long_description) != "",
    )
    has_non_empty_badge = and_(
        models.Product.web_badge_text.is_not(None),
        func.trim(models.Product.web_badge_text) != "",
    )
    has_non_empty_gallery = and_(
        models.Product.web_gallery_urls.is_not(None),
        func.trim(models.Product.web_gallery_urls) != "",
    )
    has_non_empty_whatsapp_message = and_(
        models.Product.web_whatsapp_message.is_not(None),
        func.trim(models.Product.web_whatsapp_message) != "",
    )
    has_non_empty_warranty = and_(
        models.Product.web_warranty_text.is_not(None),
        func.trim(models.Product.web_warranty_text) != "",
    )
    return or_(
        models.Product.web_published.is_(True),
        models.Product.web_featured.is_(True),
        has_non_empty_web_name,
        has_non_empty_web_category,
        has_non_empty_short_description,
        has_non_empty_long_description,
        has_non_empty_badge,
        models.Product.web_compare_price.is_not(None),
        func.lower(func.coalesce(models.Product.web_price_source, "base")) != "base",
        models.Product.web_price_value.is_not(None),
        has_non_empty_gallery,
        func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "consultar",
        models.Product.web_visible_when_out_of_stock.is_(False),
        has_non_empty_whatsapp_message,
        has_non_empty_warranty,
        func.coalesce(models.Product.web_sort_order, 0) > 0,
    )


def search_comercio_web_catalog_products(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    q: Optional[str] = None,
    published_only: Optional[bool] = None,
    configured_only: Optional[bool] = None,
    skip: int = 0,
    limit: int = 60,
):
    query = db.query(models.Product)
    if tenant_id is not None:
        query = query.filter(models.Product.tenant_id == tenant_id)
    if published_only is True:
        query = query.filter(models.Product.web_published.is_(True))
    elif published_only is False:
        query = query.filter(models.Product.web_published.is_(False))
    is_configured_publication = _web_publication_configured_expression()
    if configured_only is True:
        query = query.filter(is_configured_publication)
    elif configured_only is False:
        query = query.filter(not_(is_configured_publication))

    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                models.Product.name.ilike(like),
                models.Product.web_name.ilike(like),
                models.Product.sku.ilike(like),
                models.Product.barcode.ilike(like),
                models.Product.brand.ilike(like),
                models.Product.group_name.ilike(like),
            )
        )

    products = (
        query.order_by(
            models.Product.web_published.desc(),
            models.Product.web_featured.desc(),
            models.Product.updated_at.desc(),
            models.Product.id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )
    group_names = {p.group_name for p in products if p.group_name}
    group_map = {}
    if group_names:
        groups_query = db.query(models.ProductGroup).filter(models.ProductGroup.path.in_(group_names))
        if tenant_id is not None:
            groups_query = groups_query.filter(models.ProductGroup.tenant_id == tenant_id)
        groups = groups_query.all()
        group_map = {g.path: g for g in groups}

    for product in products:
        product.group_meta = group_map.get(product.group_name or "")
    return products


def list_comercio_web_publications_page(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    q: Optional[str] = None,
    field: str = "all",
    status_filter: str = "all",
    featured_filter: str = "all",
    badge_filter: str = "all",
    stock_filter: str = "all",
    category_key: Optional[str] = None,
    subcategory_key: Optional[str] = None,
    order: str = "newest",
    active_only: bool = True,
    skip: int = 0,
    limit: int = 50,
):
    configured_expr = _web_publication_configured_expression()
    discounted_expr = _web_publication_discounted_expression()
    has_badge_expr = and_(
        models.Product.web_badge_text.is_not(None),
        func.trim(models.Product.web_badge_text) != "",
    )
    has_image_expr = or_(
        and_(
            models.Product.image_url.is_not(None),
            func.trim(models.Product.image_url) != "",
        ),
        and_(
            models.Product.image_thumb_url.is_not(None),
            func.trim(models.Product.image_thumb_url) != "",
        ),
    )

    stock_subquery = _get_catalog_stock_subquery(db, tenant_id)
    qty_on_hand_col = func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand")

    query = (
        db.query(models.Product, qty_on_hand_col)
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(configured_expr)
    )
    if tenant_id is not None:
        query = query.filter(models.Product.tenant_id == tenant_id)

    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        if field == "name":
            query = query.filter(
                or_(
                    models.Product.name.ilike(like),
                    models.Product.web_name.ilike(like),
                )
            )
        elif field == "sku":
            query = query.filter(models.Product.sku.ilike(like))
        elif field == "brand":
            query = query.filter(models.Product.brand.ilike(like))
        elif field == "group":
            query = query.filter(models.Product.group_name.ilike(like))
        elif field == "badge":
            query = query.filter(models.Product.web_badge_text.ilike(like))
        else:
            query = query.filter(
                or_(
                    models.Product.name.ilike(like),
                    models.Product.web_name.ilike(like),
                    models.Product.sku.ilike(like),
                    models.Product.brand.ilike(like),
                    models.Product.group_name.ilike(like),
                    models.Product.web_badge_text.ilike(like),
                    models.Product.web_short_description.ilike(like),
                )
            )

    if status_filter == "featured":
        query = query.filter(models.Product.web_featured.is_(True))
    elif status_filter == "discounted":
        query = query.filter(discounted_expr)
    elif status_filter == "consult":
        query = query.filter(
            func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "consultar"
        )
    elif status_filter == "published":
        query = query.filter(models.Product.web_published.is_(True))
    elif status_filter == "paused":
        query = query.filter(
            or_(
                models.Product.web_published.is_(False),
                models.Product.web_published.is_(None),
            )
        )

    if featured_filter == "featured":
        query = query.filter(models.Product.web_featured.is_(True))
    elif featured_filter == "standard":
        query = query.filter(
            or_(
                models.Product.web_featured.is_(False),
                models.Product.web_featured.is_(None),
            )
        )

    if badge_filter == "with_badge":
        query = query.filter(has_badge_expr)
    elif badge_filter == "without_badge":
        query = query.filter(not_(has_badge_expr))

    if stock_filter == "with_stock":
        query = query.filter(qty_on_hand_col > 0)
    elif stock_filter == "without_stock":
        query = query.filter(qty_on_hand_col <= 0)
    elif stock_filter == "without_image":
        query = query.filter(
            and_(
                qty_on_hand_col > 0,
                not_(has_image_expr),
            )
        )

    if active_only and status_filter != "paused":
        query = query.filter(models.Product.web_published.is_(True))

    category_map = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=tenant_id,
        include_inactive=True,
        ensure_seeded=True,
    )
    children_map = _build_web_catalog_category_children_map(list(category_map.values()))
    category_keys_filter: Optional[set[str]] = None
    subcategory_keys_filter: Optional[set[str]] = None

    normalized_category_key = _normalize_web_catalog_category_key(category_key)
    if normalized_category_key:
        if normalized_category_key in category_map:
            category_keys_filter = _get_web_catalog_descendant_keys(normalized_category_key, children_map)
        else:
            category_keys_filter = set()

    normalized_subcategory_key = _normalize_web_catalog_category_key(subcategory_key)
    if normalized_subcategory_key:
        if normalized_subcategory_key in category_map:
            subcategory_keys_filter = _get_web_catalog_descendant_keys(
                normalized_subcategory_key,
                children_map,
            )
        else:
            subcategory_keys_filter = set()

    if category_keys_filter is not None and subcategory_keys_filter is not None:
        filter_keys = category_keys_filter.intersection(subcategory_keys_filter)
    elif subcategory_keys_filter is not None:
        filter_keys = subcategory_keys_filter
    else:
        filter_keys = category_keys_filter

    if filter_keys is not None:
        if filter_keys:
            query = query.filter(models.Product.web_category_key.in_(filter_keys))
        else:
            query = query.filter(models.Product.id == -1)

    normalized_order = (order or "newest").strip().lower()
    web_price_expr = case(
        (
            func.lower(func.coalesce(models.Product.web_price_source, "base")) == "fixed",
            func.coalesce(models.Product.web_price_value, 0),
        ),
        (
            func.lower(func.coalesce(models.Product.web_price_source, "base")) == "discount_percent",
            models.Product.price
            * (
                1
                - (
                    func.least(
                        100.0,
                        func.greatest(0.0, func.coalesce(models.Product.web_price_value, 0)),
                    )
                    / 100.0
                )
            ),
        ),
        else_=models.Product.price,
    )
    publication_created_null_order = case(
        (models.Product.web_published_at.is_(None), 1),
        else_=0,
    )
    if normalized_order == "oldest":
        order_by = [
            publication_created_null_order.asc(),
            models.Product.web_published_at.asc(),
            models.Product.id.asc(),
        ]
    elif normalized_order == "alphabetical":
        order_by = [
            func.lower(func.coalesce(models.Product.web_name, models.Product.name, "")).asc(),
            models.Product.id.asc(),
        ]
    elif normalized_order == "price_asc":
        order_by = [
            web_price_expr.asc(),
            models.Product.id.asc(),
        ]
    elif normalized_order == "price_desc":
        order_by = [
            web_price_expr.desc(),
            models.Product.id.desc(),
        ]
    else:
        order_by = [
            publication_created_null_order.asc(),
            models.Product.web_published_at.desc(),
            models.Product.id.desc(),
        ]

    total = query.count()
    rows = (
        query.order_by(*order_by)
        .offset(skip)
        .limit(limit)
        .all()
    )
    items = [product for product, _qty_on_hand in rows]

    group_names = {p.group_name for p in items if p.group_name}
    group_map = {}
    if group_names:
        groups_query = db.query(models.ProductGroup).filter(models.ProductGroup.path.in_(group_names))
        if tenant_id is not None:
            groups_query = groups_query.filter(models.ProductGroup.tenant_id == tenant_id)
        groups = groups_query.all()
        group_map = {g.path: g for g in groups}
    for product, qty_on_hand in rows:
        product.group_meta = group_map.get(product.group_name or "")
        product.qty_on_hand = float(qty_on_hand or 0.0)

    stats_query = db.query(
        func.count(models.Product.id).label("configured"),
        func.sum(case((models.Product.web_published.is_(True), 1), else_=0)).label("published"),
        func.sum(case((models.Product.web_featured.is_(True), 1), else_=0)).label("featured"),
        func.sum(case((discounted_expr, 1), else_=0)).label("discounted"),
        func.sum(
            case(
                (
                    func.lower(func.coalesce(models.Product.web_price_mode, "visible"))
                    == "consultar",
                    1,
                ),
                else_=0,
            )
        ).label("consult"),
        func.sum(
            case(
                (
                    and_(
                        models.Product.web_published.is_(True),
                        func.coalesce(stock_subquery.c.qty_on_hand, 0) > 0,
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("with_stock"),
        func.sum(
            case(
                (
                    and_(
                        models.Product.web_published.is_(True),
                        func.coalesce(stock_subquery.c.qty_on_hand, 0) <= 0,
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("without_stock"),
        func.sum(
            case(
                (
                    and_(
                        models.Product.web_published.is_(True),
                        func.coalesce(stock_subquery.c.qty_on_hand, 0) > 0,
                        not_(has_image_expr),
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("without_image"),
    ).outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id).filter(configured_expr)
    if tenant_id is not None:
        stats_query = stats_query.filter(models.Product.tenant_id == tenant_id)
    stats_row = stats_query.first()

    return {
        "items": items,
        "total": int(total or 0),
        "skip": int(skip),
        "limit": int(limit),
        "stats": {
            "configured": int((stats_row.configured if stats_row else 0) or 0),
            "published": int((stats_row.published if stats_row else 0) or 0),
            "featured": int((stats_row.featured if stats_row else 0) or 0),
            "discounted": int((stats_row.discounted if stats_row else 0) or 0),
            "consult": int((stats_row.consult if stats_row else 0) or 0),
            "with_stock": int((stats_row.with_stock if stats_row else 0) or 0),
            "without_stock": int((stats_row.without_stock if stats_row else 0) or 0),
            "without_image": int((stats_row.without_image if stats_row else 0) or 0),
        },
    }


def _seed_comercio_web_home_sliders(
    db: Session,
    *,
    tenant_id: Optional[int],
) -> None:
    existing = (
        db.query(func.count(models.WebCatalogHomeSlider.id))
        .filter(models.WebCatalogHomeSlider.tenant_id == tenant_id)
        .scalar()
    )
    if int(existing or 0) > 0:
        return
    now = datetime.utcnow()
    rows: list[models.WebCatalogHomeSlider] = []
    for slot in range(1, 6):
        rows.append(
            models.WebCatalogHomeSlider(
                tenant_id=tenant_id,
                slot=slot,
                enabled=False,
                image_url=None,
                alt_text=None,
                cta_label=None,
                cta_x_percent=50,
                cta_y_percent=80,
                link_type="catalogo",
                link_value=None,
                sort_order=slot,
                created_at=now,
                updated_at=now,
            )
        )
    db.add_all(rows)
    db.commit()


def _normalize_slider_link_type(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"sin_link", "catalogo", "categoria", "subcategoria", "personalizacion", "contacto", "url_interna"}:
        return normalized
    return "catalogo"


def _normalize_slider_text(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip()
    return normalized or None


def _validate_home_slider_link(
    db: Session,
    *,
    tenant_id: Optional[int],
    link_type: str,
    link_value: Optional[str],
) -> str | None:
    clean_value = _normalize_slider_text(link_value)
    if link_type == "sin_link":
        return None
    if link_type == "catalogo":
        return None
    if link_type == "categoria":
        if not clean_value:
            raise ValueError("Debes elegir una categoría para este slider.")
        category_map = _get_tenant_web_catalog_category_map(
            db,
            tenant_id=tenant_id,
            include_inactive=False,
            ensure_seeded=True,
        )
        if clean_value not in category_map:
            raise ValueError("La categoría configurada para este slider no existe o está inactiva.")
        return clean_value
    if link_type == "subcategoria":
        if not clean_value:
            raise ValueError("Debes elegir categoría y subcategoría para este slider.")
        if "::" not in clean_value:
            raise ValueError("La subcategoría debe incluir categoría padre y subcategoría.")
        parent_key, child_key = [item.strip() for item in clean_value.split("::", 1)]
        if not parent_key or not child_key:
            raise ValueError("La subcategoría debe incluir categoría padre y subcategoría.")
        category_map = _get_tenant_web_catalog_category_map(
            db,
            tenant_id=tenant_id,
            include_inactive=False,
            ensure_seeded=True,
        )
        parent_row = category_map.get(parent_key)
        child_row = category_map.get(child_key)
        if not parent_row or not child_row:
            raise ValueError("La categoría/subcategoría configurada no existe o está inactiva.")
        child_parent = _normalize_web_catalog_category_key(child_row.parent_key)
        if child_parent != parent_key:
            raise ValueError("La subcategoría no pertenece a la categoría seleccionada.")
        return f"{parent_key}::{child_key}"
    if link_type == "personalizacion":
        return None
    if link_type == "contacto":
        return clean_value or "inicio"
    if link_type == "url_interna":
        if not clean_value:
            raise ValueError("Debes escribir una ruta interna para este slider.")
        if not clean_value.startswith("/") or clean_value.startswith("//"):
            raise ValueError("La ruta interna debe iniciar con `/`.")
        if "://" in clean_value:
            raise ValueError("Solo se permiten rutas internas del sitio.")
        return clean_value
    return None


def list_comercio_web_home_sliders(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
) -> list[schemas.ComercioWebHomeSliderRead]:
    _seed_comercio_web_home_sliders(db, tenant_id=tenant_id)
    rows = (
        db.query(models.WebCatalogHomeSlider)
        .filter(models.WebCatalogHomeSlider.tenant_id == tenant_id)
        .order_by(models.WebCatalogHomeSlider.slot.asc(), models.WebCatalogHomeSlider.id.asc())
        .all()
    )
    return [
        schemas.ComercioWebHomeSliderRead(
            id=row.id,
            slot=int(row.slot or 0),
            enabled=bool(row.enabled),
            image_url=row.image_url,
            mobile_image_url=row.mobile_image_url,
            alt_text=row.alt_text,
            cta_label=row.cta_label,
            cta_x_percent=float(row.cta_x_percent if row.cta_x_percent is not None else 50),
            cta_y_percent=float(row.cta_y_percent if row.cta_y_percent is not None else 80),
            link_type=_normalize_slider_link_type(row.link_type),
            link_value=row.link_value,
            sort_order=int(row.sort_order or 0),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def update_comercio_web_home_slider(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    slot: int,
    payload: schemas.ComercioWebHomeSliderUpdate,
) -> schemas.ComercioWebHomeSliderRead:
    if slot < 1 or slot > 5:
        raise ValueError("Slot inválido. Debe estar entre 1 y 5.")
    _seed_comercio_web_home_sliders(db, tenant_id=tenant_id)
    row = (
        db.query(models.WebCatalogHomeSlider)
        .filter(
            models.WebCatalogHomeSlider.tenant_id == tenant_id,
            models.WebCatalogHomeSlider.slot == slot,
        )
        .first()
    )
    if not row:
        raise ValueError("Slider no encontrado")

    data = payload.model_dump(exclude_unset=True)
    next_link_type = _normalize_slider_link_type(data.get("link_type", row.link_type))
    next_link_value = _validate_home_slider_link(
        db,
        tenant_id=tenant_id,
        link_type=next_link_type,
        link_value=data.get("link_value", row.link_value),
    )
    next_image_url = _normalize_slider_text(data.get("image_url", row.image_url))
    next_mobile_image_url = _normalize_slider_text(data.get("mobile_image_url", row.mobile_image_url))
    next_enabled = bool(data.get("enabled", row.enabled))
    if next_enabled and not next_image_url:
        raise ValueError("No puedes activar un slider sin imagen.")

    if "enabled" in data:
        row.enabled = next_enabled
    if "image_url" in data:
        row.image_url = next_image_url
    if "mobile_image_url" in data:
        row.mobile_image_url = next_mobile_image_url
    if "alt_text" in data:
        row.alt_text = _normalize_slider_text(data.get("alt_text"))
    if "cta_label" in data:
        row.cta_label = _normalize_slider_text(data.get("cta_label"))
    if "cta_x_percent" in data:
        row.cta_x_percent = max(0.0, min(100.0, float(data.get("cta_x_percent") or 0)))
    if "cta_y_percent" in data:
        row.cta_y_percent = max(0.0, min(100.0, float(data.get("cta_y_percent") or 0)))
    row.link_type = next_link_type
    row.link_value = next_link_value
    if "sort_order" in data:
        row.sort_order = int(data.get("sort_order") or 0)
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)

    return schemas.ComercioWebHomeSliderRead(
        id=row.id,
        slot=int(row.slot or 0),
        enabled=bool(row.enabled),
        image_url=row.image_url,
        mobile_image_url=row.mobile_image_url,
        alt_text=row.alt_text,
        cta_label=row.cta_label,
        cta_x_percent=float(row.cta_x_percent if row.cta_x_percent is not None else 50),
        cta_y_percent=float(row.cta_y_percent if row.cta_y_percent is not None else 80),
        link_type=_normalize_slider_link_type(row.link_type),
        link_value=row.link_value,
        sort_order=int(row.sort_order or 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_public_web_home_sliders(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
) -> list[schemas.WebCatalogHomeSlider]:
    items = list_comercio_web_home_sliders(db, tenant_id=tenant_id)
    filtered = [
        item
        for item in items
        if item.enabled and bool((item.image_url or "").strip())
    ]
    filtered.sort(key=lambda item: (int(item.sort_order or 0), int(item.slot or 0)))
    return [
        schemas.WebCatalogHomeSlider(
            slot=item.slot,
            image_url=item.image_url,
            mobile_image_url=item.mobile_image_url,
            alt_text=item.alt_text,
            cta_label=item.cta_label,
            cta_x_percent=item.cta_x_percent,
            cta_y_percent=item.cta_y_percent,
            link_type=item.link_type,
            link_value=item.link_value,
            sort_order=item.sort_order,
        )
        for item in filtered[:5]
    ]


def list_comercio_web_catalog_categories(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    include_inactive: bool = True,
) -> list[schemas.ComercioWebCatalogCategoryRead]:
    rows = _get_tenant_web_catalog_categories(
        db,
        tenant_id=tenant_id,
        include_inactive=include_inactive,
        ensure_seeded=True,
    )
    category_map = {
        _normalize_web_catalog_category_key(item.key): item
        for item in rows
        if _normalize_web_catalog_category_key(item.key)
    }
    children_map = _build_web_catalog_category_children_map(rows)
    count_rows = (
        db.query(
            models.Product.web_category_key,
            func.count(models.Product.id).label("product_count"),
        )
        .filter(
            models.Product.tenant_id == tenant_id if tenant_id is not None else true(),
            models.Product.web_category_key.isnot(None),
            func.trim(models.Product.web_category_key) != "",
        )
        .group_by(models.Product.web_category_key)
        .all()
    )
    counts = {
        _normalize_web_catalog_category_key(path): int(total or 0)
        for path, total in count_rows
        if path
    }
    serialized: list[schemas.ComercioWebCatalogCategoryRead] = []
    for item in rows:
        key = _normalize_web_catalog_category_key(item.key)
        parent_key = _normalize_web_catalog_category_key(item.parent_key) or None
        descendant_keys = _get_web_catalog_descendant_keys(key, children_map) if key else set()
        product_count = sum(counts.get(descendant_key, 0) for descendant_key in descendant_keys)
        parent_name = category_map[parent_key].name if parent_key and parent_key in category_map else None
        serialized.append(
            schemas.ComercioWebCatalogCategoryRead(
                id=item.id,
                key=item.key,
                parent_key=parent_key,
                name=item.name,
                image_url=item.image_url,
                tile_color=item.tile_color,
                home_featured=bool(item.home_featured),
                home_featured_order=int(item.home_featured_order or 0),
                sort_order=int(item.sort_order or 0),
                is_active=bool(item.is_active),
                level=_get_web_catalog_category_level(key, category_map) if key else 1,
                has_children=not _is_leaf_web_catalog_category(key, children_map) if key else False,
                parent_name=parent_name,
                product_count=product_count,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
        )
    return serialized


def create_comercio_web_catalog_category(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    payload: schemas.ComercioWebCatalogCategoryCreate,
) -> schemas.ComercioWebCatalogCategoryRead:
    key = _normalize_web_catalog_category_key(payload.key)
    if not key:
        raise ValueError("Clave de categoría inválida")
    parent_key = _normalize_web_catalog_category_key(payload.parent_key) or None
    if parent_key and parent_key == key:
        raise ValueError("La categoría no puede ser su propio padre")
    parent_row = None
    if parent_key:
        parent_row = (
            db.query(models.WebCatalogCategory)
            .filter(
                models.WebCatalogCategory.tenant_id == tenant_id,
                models.WebCatalogCategory.key == parent_key,
            )
            .first()
        )
        if not parent_row:
            raise ValueError("La categoría padre no existe")
    existing = (
        db.query(models.WebCatalogCategory)
        .filter(
            models.WebCatalogCategory.tenant_id == tenant_id,
            models.WebCatalogCategory.key == key,
        )
        .first()
    )
    if existing:
        raise ValueError("Ya existe una categoría con esa clave")
    now = datetime.utcnow()
    row = models.WebCatalogCategory(
        tenant_id=tenant_id,
        key=key,
        parent_key=parent_key,
        name=payload.name.strip(),
        image_url=(payload.image_url or None),
        tile_color=(payload.tile_color or None),
        home_featured=bool(payload.home_featured),
        home_featured_order=int(payload.home_featured_order or 0),
        sort_order=int(payload.sort_order or 0),
        is_active=bool(payload.is_active),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    category_rows = (
        db.query(models.WebCatalogCategory)
        .filter(models.WebCatalogCategory.tenant_id == tenant_id)
        .all()
    )
    category_map = {
        _normalize_web_catalog_category_key(item.key): item
        for item in category_rows
        if _normalize_web_catalog_category_key(item.key)
    }
    children_map = _build_web_catalog_category_children_map(category_rows)
    normalized_key = _normalize_web_catalog_category_key(row.key)
    normalized_parent_key = _normalize_web_catalog_category_key(row.parent_key) or None
    return schemas.ComercioWebCatalogCategoryRead(
        id=row.id,
        key=row.key,
        parent_key=row.parent_key,
        name=row.name,
        image_url=row.image_url,
        tile_color=row.tile_color,
        home_featured=bool(row.home_featured),
        home_featured_order=int(row.home_featured_order or 0),
        sort_order=int(row.sort_order or 0),
        is_active=bool(row.is_active),
        level=_get_web_catalog_category_level(normalized_key, category_map) if normalized_key else 1,
        has_children=not _is_leaf_web_catalog_category(normalized_key, children_map) if normalized_key else False,
        parent_name=parent_row.name if parent_row else None,
        product_count=0,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def update_comercio_web_catalog_category(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    category_id: int,
    payload: schemas.ComercioWebCatalogCategoryUpdate,
) -> schemas.ComercioWebCatalogCategoryRead:
    row = (
        db.query(models.WebCatalogCategory)
        .filter(
            models.WebCatalogCategory.id == category_id,
            models.WebCatalogCategory.tenant_id == tenant_id,
        )
        .first()
    )
    if not row:
        raise ValueError("Categoría no encontrada")
    data = payload.model_dump(exclude_unset=True)
    old_key = _normalize_web_catalog_category_key(row.key)
    current_parent_key = _normalize_web_catalog_category_key(row.parent_key) or None
    next_key = old_key
    if "key" in data:
        next_key = _normalize_web_catalog_category_key(data.get("key"))
        if not next_key:
            raise ValueError("Clave de categoría inválida")
        duplicate = (
            db.query(models.WebCatalogCategory.id)
            .filter(
                models.WebCatalogCategory.tenant_id == tenant_id,
                models.WebCatalogCategory.key == next_key,
                models.WebCatalogCategory.id != row.id,
            )
            .first()
        )
        if duplicate:
            raise ValueError("Ya existe una categoría con esa clave")
        row.key = next_key
    next_parent_key = current_parent_key
    if "parent_key" in data:
        next_parent_key = _normalize_web_catalog_category_key(data.get("parent_key")) or None
        if next_parent_key and next_parent_key == next_key:
            raise ValueError("La categoría no puede ser su propio padre")
        if next_parent_key:
            parent_row = (
                db.query(models.WebCatalogCategory)
                .filter(
                    models.WebCatalogCategory.tenant_id == tenant_id,
                    models.WebCatalogCategory.key == next_parent_key,
                    models.WebCatalogCategory.id != row.id,
                )
                .first()
            )
            if not parent_row:
                raise ValueError("La categoría padre no existe")
            # Prevent circular parent chains.
            category_rows = (
                db.query(models.WebCatalogCategory)
                .filter(models.WebCatalogCategory.tenant_id == tenant_id)
                .all()
            )
            parent_map = {
                _normalize_web_catalog_category_key(item.key): _normalize_web_catalog_category_key(item.parent_key) or None
                for item in category_rows
                if _normalize_web_catalog_category_key(item.key)
            }
            parent_map[next_key] = next_parent_key
            visited: set[str] = set()
            cursor = next_parent_key
            while cursor:
                if cursor in visited:
                    break
                if cursor == next_key:
                    raise ValueError("No se puede asignar una categoría hija como padre")
                visited.add(cursor)
                cursor = parent_map.get(cursor)
        row.parent_key = next_parent_key
    if "name" in data:
        row.name = (data.get("name") or "").strip()
    if "image_url" in data:
        row.image_url = data.get("image_url") or None
    if "tile_color" in data:
        row.tile_color = data.get("tile_color") or None
    if "home_featured" in data:
        row.home_featured = bool(data.get("home_featured"))
    if "home_featured_order" in data:
        row.home_featured_order = int(data.get("home_featured_order") or 0)
    if "sort_order" in data:
        row.sort_order = int(data.get("sort_order") or 0)
    if "is_active" in data:
        row.is_active = bool(data.get("is_active"))
    row.updated_at = datetime.utcnow()
    db.add(row)

    # Keep published assignments aligned when key changes.
    next_key = _normalize_web_catalog_category_key(row.key)
    if old_key and next_key and old_key != next_key:
        (
            db.query(models.Product)
            .filter(
                models.Product.tenant_id == tenant_id if tenant_id is not None else true(),
                models.Product.web_category_key == old_key,
            )
            .update({models.Product.web_category_key: next_key}, synchronize_session=False)
        )
        (
            db.query(models.WebCatalogCategory)
            .filter(
                models.WebCatalogCategory.tenant_id == tenant_id if tenant_id is not None else true(),
                models.WebCatalogCategory.parent_key == old_key,
            )
            .update({models.WebCatalogCategory.parent_key: next_key}, synchronize_session=False)
        )
    db.commit()
    db.refresh(row)
    category_rows = (
        db.query(models.WebCatalogCategory)
        .filter(models.WebCatalogCategory.tenant_id == tenant_id)
        .all()
    )
    category_map = {
        _normalize_web_catalog_category_key(item.key): item
        for item in category_rows
        if _normalize_web_catalog_category_key(item.key)
    }
    children_map = _build_web_catalog_category_children_map(category_rows)
    assigned_count = (
        db.query(func.count(models.Product.id))
        .filter(
            models.Product.tenant_id == tenant_id if tenant_id is not None else true(),
            models.Product.web_category_key == row.key,
        )
        .scalar()
    )
    normalized_key = _normalize_web_catalog_category_key(row.key)
    normalized_parent_key = _normalize_web_catalog_category_key(row.parent_key) or None
    parent_name = (
        category_map[normalized_parent_key].name
        if normalized_parent_key and normalized_parent_key in category_map
        else None
    )
    return schemas.ComercioWebCatalogCategoryRead(
        id=row.id,
        key=row.key,
        parent_key=row.parent_key,
        name=row.name,
        image_url=row.image_url,
        tile_color=row.tile_color,
        home_featured=bool(row.home_featured),
        home_featured_order=int(row.home_featured_order or 0),
        sort_order=int(row.sort_order or 0),
        is_active=bool(row.is_active),
        level=_get_web_catalog_category_level(normalized_key, category_map) if normalized_key else 1,
        has_children=not _is_leaf_web_catalog_category(normalized_key, children_map) if normalized_key else False,
        parent_name=parent_name,
        product_count=int(assigned_count or 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def delete_comercio_web_catalog_category(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    category_id: int,
) -> None:
    row = (
        db.query(models.WebCatalogCategory)
        .filter(
            models.WebCatalogCategory.id == category_id,
            models.WebCatalogCategory.tenant_id == tenant_id,
        )
        .first()
    )
    if not row:
        raise ValueError("Categoría no encontrada")
    child_count = (
        db.query(func.count(models.WebCatalogCategory.id))
        .filter(
            models.WebCatalogCategory.tenant_id == tenant_id if tenant_id is not None else true(),
            models.WebCatalogCategory.parent_key == row.key,
        )
        .scalar()
    )
    if int(child_count or 0) > 0:
        raise ValueError("No puedes eliminar una categoría que tiene subcategorías")
    assigned_count = (
        db.query(func.count(models.Product.id))
        .filter(
            models.Product.tenant_id == tenant_id if tenant_id is not None else true(),
            models.Product.web_category_key == row.key,
        )
        .scalar()
    )
    if int(assigned_count or 0) > 0:
        raise ValueError("No puedes eliminar una categoría con productos asignados")
    db.delete(row)
    db.commit()


def _normalize_web_description_template_key(value: Optional[str]) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9_-]+", "_", ascii_text)
    slug = re.sub(r"_+", "_", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("_-")[:64]


def _web_description_template_key_candidates(value: Optional[str]) -> list[str]:
    normalized = _normalize_web_description_template_key(value)
    if not normalized:
        return []
    candidates = [normalized]
    hyphen_variant = normalized.replace("_", "-")
    underscore_variant = normalized.replace("-", "_")
    for item in [hyphen_variant, underscore_variant]:
        if item and item not in candidates:
            candidates.append(item)
    return candidates


def _parse_web_description_template_keywords(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    clean: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(normalized)
    return clean


def _serialize_web_description_template_keywords(keywords: Sequence[str]) -> str:
    clean: list[str] = []
    seen: set[str] = set()
    for item in keywords:
        normalized = (item or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(normalized)
    return json.dumps(clean, ensure_ascii=False)


def _serialize_web_description_template(
    row: models.WebCatalogDescriptionTemplate,
) -> schemas.ComercioWebDescriptionTemplateRead:
    return schemas.ComercioWebDescriptionTemplateRead(
        id=row.id,
        template_key=(row.template_key or "").strip(),
        label=(row.label or "").strip() or "Plantilla",
        assigned_category_key=_normalize_web_catalog_category_key(row.assigned_category_key) or None,
        keywords=_parse_web_description_template_keywords(row.keywords_json),
        paragraph1=row.paragraph1 or "",
        paragraph2=row.paragraph2 or "",
        paragraph3=row.paragraph3 or "",
        closing=row.closing or "",
        sort_order=int(row.sort_order or 0),
        created_by_user_id=row.created_by_user_id,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _seed_default_web_description_templates(
    db: Session,
    *,
    tenant_id: Optional[int],
) -> None:
    existing_count = (
        db.query(func.count(models.WebCatalogDescriptionTemplate.id))
        .filter(models.WebCatalogDescriptionTemplate.tenant_id == tenant_id)
        .scalar()
    )
    if int(existing_count or 0) > 0:
        return
    now = datetime.utcnow()
    rows = [
        models.WebCatalogDescriptionTemplate(
            tenant_id=tenant_id,
            template_key=_normalize_web_description_template_key(item["template_key"]),
            label=item["label"],
            assigned_category_key=_normalize_web_catalog_category_key(item.get("assigned_category_key")) or None,
            keywords_json=_serialize_web_description_template_keywords(item.get("keywords", [])),
            paragraph1=item.get("paragraph1") or "",
            paragraph2=item.get("paragraph2") or "",
            paragraph3=item.get("paragraph3") or "",
            closing=item.get("closing") or DEFAULT_WEB_DESCRIPTION_TEMPLATE_CLOSING,
            sort_order=int(item.get("sort_order") or 0),
            created_at=now,
            updated_at=now,
        )
        for item in DEFAULT_WEB_DESCRIPTION_TEMPLATES
    ]
    db.add_all(rows)
    db.commit()


def list_comercio_web_description_templates(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
) -> list[schemas.ComercioWebDescriptionTemplateRead]:
    _seed_default_web_description_templates(db, tenant_id=tenant_id)
    rows = (
        db.query(models.WebCatalogDescriptionTemplate)
        .filter(models.WebCatalogDescriptionTemplate.tenant_id == tenant_id)
        .order_by(
            models.WebCatalogDescriptionTemplate.sort_order.asc(),
            models.WebCatalogDescriptionTemplate.label.asc(),
            models.WebCatalogDescriptionTemplate.id.asc(),
        )
        .all()
    )
    return [_serialize_web_description_template(row) for row in rows]


def create_comercio_web_description_template(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    payload: schemas.ComercioWebDescriptionTemplateCreate,
    actor_user_id: Optional[int] = None,
) -> schemas.ComercioWebDescriptionTemplateRead:
    template_key = _normalize_web_description_template_key(payload.template_key)
    if not template_key:
        raise ValueError("La clave interna de la plantilla es inválida")
    key_candidates = _web_description_template_key_candidates(template_key)
    duplicate = (
        db.query(models.WebCatalogDescriptionTemplate.id)
        .filter(
            models.WebCatalogDescriptionTemplate.tenant_id == tenant_id,
            models.WebCatalogDescriptionTemplate.template_key.in_(key_candidates),
        )
        .first()
    )
    if duplicate:
        raise ValueError("Ya existe una plantilla con esa clave")
    now = datetime.utcnow()
    row = models.WebCatalogDescriptionTemplate(
        tenant_id=tenant_id,
        template_key=template_key,
        label=(payload.label or "").strip(),
        assigned_category_key=_normalize_web_catalog_category_key(payload.assigned_category_key) or None,
        keywords_json=_serialize_web_description_template_keywords(payload.keywords),
        paragraph1=payload.paragraph1 or "",
        paragraph2=payload.paragraph2 or "",
        paragraph3=payload.paragraph3 or "",
        closing=payload.closing or "",
        sort_order=int(payload.sort_order or 0),
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_web_description_template(row)


def update_comercio_web_description_template(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    template_key: str,
    payload: schemas.ComercioWebDescriptionTemplateUpdate,
    actor_user_id: Optional[int] = None,
) -> schemas.ComercioWebDescriptionTemplateRead:
    key_candidates = _web_description_template_key_candidates(template_key)
    row = (
        db.query(models.WebCatalogDescriptionTemplate)
        .filter(
            models.WebCatalogDescriptionTemplate.tenant_id == tenant_id,
            models.WebCatalogDescriptionTemplate.template_key.in_(key_candidates),
        )
        .first()
    )
    if not row:
        raise ValueError("Plantilla no encontrada")
    data = payload.model_dump(exclude_unset=True)
    if "template_key" in data:
        next_template_key = _normalize_web_description_template_key(data.get("template_key"))
        if not next_template_key:
            raise ValueError("La clave interna de la plantilla es inválida")
        next_key_candidates = _web_description_template_key_candidates(next_template_key)
        duplicate = (
            db.query(models.WebCatalogDescriptionTemplate.id)
            .filter(
                models.WebCatalogDescriptionTemplate.tenant_id == tenant_id,
                models.WebCatalogDescriptionTemplate.template_key.in_(next_key_candidates),
                models.WebCatalogDescriptionTemplate.id != row.id,
            )
            .first()
        )
        if duplicate:
            raise ValueError("Ya existe una plantilla con esa clave")
        row.template_key = next_template_key
    if "label" in data:
        row.label = (data.get("label") or "").strip()
    if "assigned_category_key" in data:
        row.assigned_category_key = _normalize_web_catalog_category_key(data.get("assigned_category_key")) or None
    if "keywords" in data:
        row.keywords_json = _serialize_web_description_template_keywords(data.get("keywords") or [])
    if "paragraph1" in data:
        row.paragraph1 = data.get("paragraph1") or ""
    if "paragraph2" in data:
        row.paragraph2 = data.get("paragraph2") or ""
    if "paragraph3" in data:
        row.paragraph3 = data.get("paragraph3") or ""
    if "closing" in data:
        row.closing = data.get("closing") or ""
    if "sort_order" in data:
        row.sort_order = int(data.get("sort_order") or 0)
    row.updated_by_user_id = actor_user_id
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_web_description_template(row)


def delete_comercio_web_description_template(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    template_key: str,
) -> None:
    key_candidates = _web_description_template_key_candidates(template_key)
    row = (
        db.query(models.WebCatalogDescriptionTemplate)
        .filter(
            models.WebCatalogDescriptionTemplate.tenant_id == tenant_id,
            models.WebCatalogDescriptionTemplate.template_key.in_(key_candidates),
        )
        .first()
    )
    if not row:
        raise ValueError("Plantilla no encontrada")
    total = (
        db.query(func.count(models.WebCatalogDescriptionTemplate.id))
        .filter(models.WebCatalogDescriptionTemplate.tenant_id == tenant_id)
        .scalar()
    )
    if int(total or 0) <= 1:
        raise ValueError("Debes mantener al menos una plantilla")
    db.delete(row)
    db.commit()


def reset_comercio_web_description_templates(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
) -> list[schemas.ComercioWebDescriptionTemplateRead]:
    (
        db.query(models.WebCatalogDescriptionTemplate)
        .filter(models.WebCatalogDescriptionTemplate.tenant_id == tenant_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    _seed_default_web_description_templates(db, tenant_id=tenant_id)
    rows = (
        db.query(models.WebCatalogDescriptionTemplate)
        .filter(models.WebCatalogDescriptionTemplate.tenant_id == tenant_id)
        .all()
    )
    if actor_user_id is not None:
        now = datetime.utcnow()
        for row in rows:
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
            db.add(row)
        db.commit()
    return list_comercio_web_description_templates(db, tenant_id=tenant_id)


def _normalize_discount_code(value: str) -> str:
    return (value or "").strip().upper()

def _normalize_discount_code_type(value: Optional[str]) -> str:
    normalized = (value or "percent").strip().lower()
    return "fixed_amount" if normalized == "fixed_amount" else "percent"

def _resolve_discount_code_snapshot_values(
    *,
    discount_type: Optional[str],
    discount_value: Optional[float],
    discount_percent: Optional[float],
) -> tuple[str, float, float]:
    normalized_type = _normalize_discount_code_type(discount_type)
    if normalized_type == "fixed_amount":
        fixed_value = max(0.0, float(discount_value or 0.0))
        return normalized_type, fixed_value, 0.0
    percent_value = min(100.0, max(0.0, float(discount_percent or discount_value or 0.0)))
    return normalized_type, percent_value, percent_value

def _compute_coupon_discount_amount(
    subtotal_base: float,
    *,
    discount_type: Optional[str],
    discount_value: Optional[float],
    discount_percent: Optional[float],
) -> float:
    subtotal = max(0.0, float(subtotal_base or 0.0))
    normalized_type, value, percent = _resolve_discount_code_snapshot_values(
        discount_type=discount_type,
        discount_value=discount_value,
        discount_percent=discount_percent,
    )
    if subtotal <= 0:
        return 0.0
    if normalized_type == "fixed_amount":
        return min(round(value, 2), subtotal)
    return min(round(subtotal * (percent / 100.0), 2), subtotal)


def list_comercio_web_discount_codes_page(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    q: Optional[str] = None,
    active_only: Optional[bool] = None,
    skip: int = 0,
    limit: int = 50,
) -> schemas.ComercioWebDiscountCodePage:
    query = db.query(models.WebDiscountCode)
    if tenant_id is not None:
        query = query.filter(models.WebDiscountCode.tenant_id == tenant_id)

    term = (q or "").strip()
    if term:
        query = query.filter(models.WebDiscountCode.code.ilike(f"%{term}%"))

    if active_only is True:
        query = query.filter(models.WebDiscountCode.is_active.is_(True))
    elif active_only is False:
        query = query.filter(models.WebDiscountCode.is_active.is_(False))

    total = query.count()
    rows = (
        query.order_by(
            models.WebDiscountCode.is_active.desc(),
            models.WebDiscountCode.updated_at.desc(),
            models.WebDiscountCode.id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )
    return schemas.ComercioWebDiscountCodePage(items=rows, total=total, skip=skip, limit=limit)


def create_comercio_web_discount_code(
    db: Session,
    *,
    tenant_id: Optional[int],
    payload: schemas.ComercioWebDiscountCodeCreate,
    actor_user_id: Optional[int] = None,
) -> models.WebDiscountCode:
    code = _normalize_discount_code(payload.code)
    if not code:
        raise ValueError("El código no puede estar vacío")
    if payload.ends_at and payload.starts_at and payload.ends_at < payload.starts_at:
        raise ValueError("La fecha fin debe ser mayor o igual a la fecha inicio")
    if payload.max_uses is not None and int(payload.max_uses) < 1:
        raise ValueError("El uso máximo debe ser mayor a 0")
    discount_type = _normalize_discount_code_type(payload.discount_type)
    discount_value = float(payload.discount_value or 0.0)
    if discount_value <= 0:
        raise ValueError("El valor del descuento debe ser mayor a 0")
    if discount_type == "percent" and discount_value > 100:
        raise ValueError("El descuento porcentual no puede ser mayor a 100")

    existing = db.query(models.WebDiscountCode).filter(
        models.WebDiscountCode.tenant_id == tenant_id,
        models.WebDiscountCode.code == code,
    ).first()
    if existing:
        raise ValueError("Ya existe un código con ese nombre")

    row = models.WebDiscountCode(
        tenant_id=tenant_id,
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        discount_percent=(
            discount_value
            if discount_type == "percent"
            else 0.0
        ),
        is_active=bool(payload.is_active),
        max_uses=int(payload.max_uses) if payload.max_uses is not None else None,
        uses_count=0,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_comercio_web_discount_code(
    db: Session,
    *,
    tenant_id: Optional[int],
    discount_code_id: int,
    payload: schemas.ComercioWebDiscountCodeUpdate,
) -> models.WebDiscountCode:
    row = db.query(models.WebDiscountCode).filter(
        models.WebDiscountCode.id == discount_code_id,
        models.WebDiscountCode.tenant_id == tenant_id,
    ).first()
    if not row:
        raise ValueError("Código de descuento no encontrado")

    data = payload.model_dump(exclude_unset=True)
    if "code" in data:
        normalized_code = _normalize_discount_code(str(data["code"]))
        if not normalized_code:
            raise ValueError("El código no puede estar vacío")
        duplicate = db.query(models.WebDiscountCode).filter(
            models.WebDiscountCode.tenant_id == tenant_id,
            models.WebDiscountCode.code == normalized_code,
            models.WebDiscountCode.id != row.id,
        ).first()
        if duplicate:
            raise ValueError("Ya existe un código con ese nombre")
        row.code = normalized_code

    if (
        ("discount_type" in data and data["discount_type"] is not None)
        or ("discount_value" in data and data["discount_value"] is not None)
        or ("discount_percent" in data and data["discount_percent"] is not None)
    ):
        next_type = _normalize_discount_code_type(data.get("discount_type", row.discount_type))
        if "discount_value" in data and data["discount_value"] is not None:
            next_value = float(data["discount_value"])
        elif "discount_percent" in data and data["discount_percent"] is not None:
            next_value = float(data["discount_percent"])
        else:
            next_value = float(row.discount_value or row.discount_percent or 0.0)

        if next_value <= 0:
            raise ValueError("El valor del descuento debe ser mayor a 0")
        if next_type == "percent" and next_value > 100:
            raise ValueError("El descuento porcentual no puede ser mayor a 100")

        row.discount_type = next_type
        row.discount_value = next_value
        row.discount_percent = next_value if next_type == "percent" else 0.0
    if "is_active" in data and data["is_active"] is not None:
        row.is_active = bool(data["is_active"])
    if "max_uses" in data:
        max_uses = data.get("max_uses")
        if max_uses is not None and int(max_uses) < 1:
            raise ValueError("El uso máximo debe ser mayor a 0")
        row.max_uses = int(max_uses) if max_uses is not None else None
    if "starts_at" in data:
        row.starts_at = data["starts_at"]
    if "ends_at" in data:
        row.ends_at = data["ends_at"]

    if row.ends_at and row.starts_at and row.ends_at < row.starts_at:
        raise ValueError("La fecha fin debe ser mayor o igual a la fecha inicio")

    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_comercio_web_discount_code_usage_page(
    db: Session,
    *,
    tenant_id: Optional[int],
    discount_code_id: int,
    skip: int = 0,
    limit: int = 50,
) -> schemas.ComercioWebDiscountCodeUsagePage:
    discount_code = (
        db.query(models.WebDiscountCode)
        .filter(
            models.WebDiscountCode.id == discount_code_id,
            models.WebDiscountCode.tenant_id == tenant_id,
        )
        .first()
    )
    if not discount_code:
        raise ValueError("Código de descuento no encontrado")

    query = db.query(models.WebOrder).filter(
        models.WebOrder.tenant_id == tenant_id,
        models.WebOrder.coupon_discount_code_id == discount_code_id,
    )
    total = query.count()
    rows = (
        query.order_by(
            func.coalesce(models.WebOrder.coupon_consumed_at, models.WebOrder.created_at).desc(),
            models.WebOrder.created_at.desc(),
            models.WebOrder.id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )

    items = [
        schemas.ComercioWebDiscountCodeUsageRow(
            order_id=int(row.id),
            document_number=row.document_number,
            customer_name=row.customer_name,
            customer_email=row.customer_email,
            total=float(row.total or 0.0),
            currency=(row.currency or "COP"),
            order_status=str(row.status or "pending_payment"),
            payment_status=str(row.payment_status or "pending"),
            used_at=row.coupon_consumed_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return schemas.ComercioWebDiscountCodeUsagePage(items=items, total=total, skip=skip, limit=limit)


def get_catalog_version(
    db: Session,
    tenant_id: Optional[int] = None,
):
    products_query = db.query(models.Product)
    groups_query = db.query(models.ProductGroup)
    if tenant_id is not None:
        products_query = products_query.filter(models.Product.tenant_id == tenant_id)
        groups_query = groups_query.filter(models.ProductGroup.tenant_id == tenant_id)

    products_ts = products_query.with_entities(func.max(models.Product.updated_at)).scalar()
    groups_ts = groups_query.with_entities(func.max(models.ProductGroup.updated_at)).scalar()
    products_count = products_query.with_entities(func.count(models.Product.id)).scalar()
    groups_count = groups_query.with_entities(func.count(models.ProductGroup.id)).scalar()
    updated_at = max(
        [ts for ts in [products_ts, groups_ts] if ts is not None],
        default=None,
    )
    return products_ts, groups_ts, updated_at, products_count, groups_count


def get_product_by_sku(
    db: Session,
    sku: str,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.Product).filter(models.Product.sku == sku)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    return query.first()


def get_product_by_barcode(
    db: Session,
    barcode: str,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.Product).filter(models.Product.barcode == barcode)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    return query.first()


# 🔹 Obtener producto por ID
def _attach_group_meta(db: Session, product: Optional[models.Product]):
    if not product:
        return None
    if not getattr(product, "group_name", None):
        product.group_meta = None
        return product
    group = get_product_group_by_path(db, product.group_name, tenant_id=product.tenant_id)
    product.group_meta = group
    return product


def get_product(
    db: Session,
    product_id: int,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.Product).filter(models.Product.id == product_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    product = query.first()
    return _attach_group_meta(db, product)


def create_product(
    db: Session,
    product_in: schemas.ProductCreate,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    web_price_source = _normalize_web_price_source(product_in.web_price_source, strict=True)
    web_price_value = (
        float(product_in.web_price_value) if product_in.web_price_value is not None else None
    )
    if web_price_source == WEB_PRICE_SOURCE_DEFAULT:
        web_price_value = None
    elif web_price_value is None:
        raise ValueError("Debes indicar un valor para el precio web")
    elif web_price_source == WEB_PRICE_SOURCE_FIXED and web_price_value <= 0:
        raise ValueError("El precio web fijo debe ser mayor que cero")
    elif (
        web_price_source == WEB_PRICE_SOURCE_DISCOUNT_PERCENT
        and (web_price_value <= 0 or web_price_value >= 100)
    ):
        raise ValueError("El descuento web debe estar entre 0 y 100")

    base_price = float(product_in.price or 0.0)
    if web_price_source == WEB_PRICE_SOURCE_FIXED:
        sale_price = float(web_price_value or 0.0)
    elif web_price_source == WEB_PRICE_SOURCE_DISCOUNT_PERCENT:
        discount_percent = float(web_price_value or 0.0)
        sale_price = max(0.0, base_price * (1.0 - (discount_percent / 100.0)))
    else:
        sale_price = max(0.0, base_price)
    compare_price = (
        float(product_in.web_compare_price)
        if product_in.web_compare_price is not None and float(product_in.web_compare_price) > sale_price
        else None
    )
    normalized_web_category_key = _normalize_web_catalog_category_key(product_in.web_category_key)
    if normalized_web_category_key:
        category_rows = _get_tenant_web_catalog_categories(
            db,
            tenant_id=effective_tenant_id,
            include_inactive=True,
            ensure_seeded=True,
        )
        category_map = {
            _normalize_web_catalog_category_key(item.key): item
            for item in category_rows
            if _normalize_web_catalog_category_key(item.key)
        }
        if normalized_web_category_key not in category_map:
            raise ValueError("Categoría web inválida")
        if not bool(category_map[normalized_web_category_key].is_active):
            raise ValueError("La categoría web seleccionada está inactiva")
    if product_in.web_published and not normalized_web_category_key:
        raise ValueError("Debes asignar una categoría web antes de publicar")

    db_product = models.Product(
        tenant_id=effective_tenant_id,
        sku=product_in.sku,
        name=product_in.name,
        price=product_in.price,
        cost=product_in.cost,
        barcode=product_in.barcode,
        label_format=resolve_product_label_format(
            group_name=product_in.group_name,
            label_format=product_in.label_format,
        ),
        unit=product_in.unit,
        image_url=product_in.image_url,
        image_thumb_url=product_in.image_thumb_url,
        tile_color=product_in.tile_color,
        stock_min=product_in.stock_min,
        preferred_qty=product_in.preferred_qty,
        reorder_point=product_in.reorder_point,
        low_stock_alert=product_in.low_stock_alert,
        allow_price_change=product_in.allow_price_change,
        active=product_in.active,
        service=product_in.service,
        includes_tax=product_in.includes_tax,
        is_investment=product_in.is_investment,
        investment_status=("active" if product_in.is_investment else "archived"),
        investment_enabled_at=(datetime.utcnow() if product_in.is_investment else None),
        investment_disabled_at=(None if product_in.is_investment else datetime.utcnow()),
        group_name=product_in.group_name,
        brand=product_in.brand,
        supplier=product_in.supplier,
        web_name=product_in.web_name,
        web_slug=build_product_web_slug(
            product_in.web_slug or product_in.web_name or product_in.name,
            product_in.sku,
        ),
        web_published=product_in.web_published,
        web_published_at=(datetime.utcnow() if product_in.web_published else None),
        web_featured=product_in.web_featured,
        web_short_description=product_in.web_short_description,
        web_long_description=product_in.web_long_description,
        web_compare_price=compare_price,
        web_price_source=web_price_source,
        web_price_value=web_price_value,
        web_badge_text=product_in.web_badge_text,
        web_category_key=(normalized_web_category_key or None),
        web_sort_order=product_in.web_sort_order,
        web_visible_when_out_of_stock=product_in.web_visible_when_out_of_stock,
        web_price_mode=product_in.web_price_mode,
        web_whatsapp_message=product_in.web_whatsapp_message,
        web_warranty_text=product_in.web_warranty_text,
        web_video_url=((product_in.web_video_url or "").strip() or None),
        web_gallery_urls=(
            json.dumps(_parse_product_gallery_urls(product_in.web_gallery_urls), ensure_ascii=False)
            if _parse_product_gallery_urls(product_in.web_gallery_urls)
            else None
        ),
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return _attach_group_meta(db, db_product)


# 🔹 Actualizar producto (ignorando valores None)
def update_product(
    db: Session,
    db_product: models.Product,
    product_in: schemas.ProductBase,
):
    data = product_in.dict(exclude_unset=True)
    if "web_gallery_urls" in data:
        gallery = _parse_product_gallery_urls(data.get("web_gallery_urls"))
        data["web_gallery_urls"] = json.dumps(gallery, ensure_ascii=False) if gallery else None
        if gallery:
            data["image_url"] = gallery[0]
            data["image_thumb_url"] = gallery[0]
        elif "image_url" not in data:
            data["image_url"] = None
            data["image_thumb_url"] = None
    if "web_video_url" in data:
        data["web_video_url"] = ((data.get("web_video_url") or "").strip() or None)
    if "web_slug" in data:
        data["web_slug"] = build_product_web_slug(
            data.get("web_slug") or data.get("web_name") or data.get("name") or db_product.name,
            data.get("sku") or db_product.sku,
        )
    if "web_category_key" in data:
        next_category = _normalize_web_catalog_category_key(data.get("web_category_key"))
        data["web_category_key"] = next_category or None
        if next_category:
            category_rows = _get_tenant_web_catalog_categories(
                db,
                tenant_id=db_product.tenant_id,
                include_inactive=True,
                ensure_seeded=True,
            )
            category_map = {
                _normalize_web_catalog_category_key(item.key): item
                for item in category_rows
                if _normalize_web_catalog_category_key(item.key)
            }
            if next_category not in category_map:
                raise ValueError("Categoría web inválida")
            if not bool(category_map[next_category].is_active):
                raise ValueError("La categoría web seleccionada está inactiva")
    if "web_price_source" in data:
        data["web_price_source"] = _normalize_web_price_source(
            data.get("web_price_source"),
            strict=True,
        )
    next_web_price_source = _normalize_web_price_source(
        data.get("web_price_source", db_product.web_price_source)
    )
    next_web_price_value_raw = data.get("web_price_value", db_product.web_price_value)
    next_web_price_value: Optional[float]
    if next_web_price_source == WEB_PRICE_SOURCE_DEFAULT:
        next_web_price_value = None
        data["web_price_value"] = None
    else:
        if next_web_price_value_raw is None:
            raise ValueError("Debes indicar un valor para el precio web")
        next_web_price_value = float(next_web_price_value_raw)
        if next_web_price_source == WEB_PRICE_SOURCE_FIXED:
            if next_web_price_value <= 0:
                raise ValueError("El precio web fijo debe ser mayor que cero")
        elif next_web_price_source == WEB_PRICE_SOURCE_DISCOUNT_PERCENT:
            if next_web_price_value <= 0 or next_web_price_value >= 100:
                raise ValueError("El descuento web debe estar entre 0 y 100")
        data["web_price_value"] = next_web_price_value

    next_base_price = float(data.get("price", db_product.price) or 0.0)
    if next_web_price_source == WEB_PRICE_SOURCE_FIXED:
        next_sale_price = float(next_web_price_value or 0.0)
    elif next_web_price_source == WEB_PRICE_SOURCE_DISCOUNT_PERCENT:
        discount_percent = float(next_web_price_value or 0.0)
        next_sale_price = max(0.0, next_base_price * (1.0 - (discount_percent / 100.0)))
    else:
        next_sale_price = max(0.0, next_base_price)
    if "web_compare_price" in data:
        compare_value = data.get("web_compare_price")
        if compare_value is None:
            data["web_compare_price"] = None
        else:
            next_compare_price = float(compare_value)
            data["web_compare_price"] = (
                next_compare_price if next_compare_price > next_sale_price else None
            )
    elif (
        db_product.web_compare_price is not None
        and float(db_product.web_compare_price) <= next_sale_price
    ):
        data["web_compare_price"] = None
    next_web_published = bool(data.get("web_published", db_product.web_published))
    next_web_category = _normalize_web_catalog_category_key(
        data.get("web_category_key", db_product.web_category_key)
    )
    if next_web_published and not next_web_category:
        raise ValueError("Debes asignar una categoría web antes de publicar")
    if next_web_published and next_web_category:
        category_rows = _get_tenant_web_catalog_categories(
            db,
            tenant_id=db_product.tenant_id,
            include_inactive=True,
            ensure_seeded=True,
        )
        category_map = {
            _normalize_web_catalog_category_key(item.key): item
            for item in category_rows
            if _normalize_web_catalog_category_key(item.key)
        }
        category_def = category_map.get(_normalize_web_catalog_category_key(next_web_category))
        if category_def is None:
            raise ValueError("Categoría web inválida")
        if not bool(category_def.is_active):
            raise ValueError("La categoría web seleccionada está inactiva")
    now = datetime.utcnow()
    if (
        next_web_published
        and not bool(db_product.web_published)
        and db_product.web_published_at is None
    ):
        data["web_published_at"] = now
    if "is_investment" in data:
        next_is_investment = bool(data.get("is_investment"))
        if next_is_investment:
            if not db_product.is_investment or db_product.investment_enabled_at is None:
                data["investment_enabled_at"] = now
            data["investment_status"] = "active"
            data["investment_disabled_at"] = None
        else:
            data["investment_enabled_at"] = None
            data["investment_disabled_at"] = now
            data["investment_status"] = "archived"
    if "investment_status" in data:
        next_status = (data.get("investment_status") or "").strip().lower()
        if next_status in {"active", "paused", "archived"}:
            explicit_is_investment = "is_investment" in data
            next_is_investment = bool(data.get("is_investment", db_product.is_investment))

            # Si el request desactiva explícitamente inversión, ese valor manda
            # y evita que investment_status reactive el producto.
            if explicit_is_investment and not next_is_investment:
                data["investment_status"] = "archived"
                data["investment_enabled_at"] = None
                data["investment_disabled_at"] = now
            else:
                if not next_is_investment:
                    data["is_investment"] = True
                    next_is_investment = True
                if next_status == "active":
                    data["investment_enabled_at"] = now
                    data["investment_disabled_at"] = None
                else:
                    if db_product.investment_enabled_at is None:
                        data["investment_enabled_at"] = now
                    data["investment_disabled_at"] = now
    if "label_format" in data or "group_name" in data:
        next_group_name = data.get("group_name", db_product.group_name)
        if "label_format" in data:
            data["label_format"] = resolve_product_label_format(
                group_name=next_group_name,
                label_format=data.get("label_format"),
            )
        elif "group_name" in data:
            data["label_format"] = resolve_product_label_format(
                group_name=next_group_name,
                label_format=None,
            )
    for field, value in data.items():
        setattr(db_product, field, value)
    db.commit()
    db.refresh(db_product)
    return _attach_group_meta(db, db_product)


# 🔹 Eliminar producto
def delete_product(db: Session, db_product: models.Product):
    db.delete(db_product)
    db.commit()


def create_product_audit_log(
    db: Session,
    *,
    product_id: int,
    action: str,
    actor_user: Optional[models.PosUser] = None,
    changes: Optional[Dict[str, Any]] = None,
) -> models.ProductAuditLog:
    effective_tenant_id = (
        actor_user.tenant_id if actor_user and actor_user.tenant_id is not None else get_default_tenant_id(db)
    )
    entry = models.ProductAuditLog(
        tenant_id=effective_tenant_id,
        product_id=product_id,
        action=action,
        actor_user_id=actor_user.id if actor_user else None,
        actor_name=(actor_user.name if actor_user else None),
        actor_email=(actor_user.email if actor_user else None),
        changes=changes or None,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_product_audit_logs(
    db: Session,
    *,
    product_id: int,
    limit: int = 100,
    tenant_id: Optional[int] = None,
) -> List[models.ProductAuditLog]:
    safe_limit = min(max(limit, 1), 200)
    query = db.query(models.ProductAuditLog).filter(models.ProductAuditLog.product_id == product_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ProductAuditLog.tenant_id == effective_tenant_id)
    return query.order_by(models.ProductAuditLog.created_at.desc(), models.ProductAuditLog.id.desc()).limit(safe_limit).all()


def list_recent_product_audit_logs(
    db: Session,
    *,
    limit: int = 10,
    tenant_id: Optional[int] = None,
) -> List[models.ProductAuditLog]:
    safe_limit = min(max(limit, 1), 200)
    query = db.query(models.ProductAuditLog)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ProductAuditLog.tenant_id == effective_tenant_id)
    return query.order_by(models.ProductAuditLog.created_at.desc(), models.ProductAuditLog.id.desc()).limit(safe_limit).all()


# ===================== PAYMENT METHODS =====================


def _normalize_slug(slug: str) -> str:
    return slug.strip().lower()


def list_payment_methods(
    db: Session,
    include_deleted: bool = False,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.PaymentMethod)
    if tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == tenant_id)
    if not include_deleted:
        query = query.filter(models.PaymentMethod.deleted_at.is_(None))
    return (
        query.order_by(models.PaymentMethod.order_index.asc(), models.PaymentMethod.id.asc())
        .all()
    )


def get_payment_method(
    db: Session,
    method_id: int,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.PaymentMethod).filter(models.PaymentMethod.id == method_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == effective_tenant_id)
    return query.first()


def get_payment_method_by_slug(
    db: Session,
    slug: str,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.PaymentMethod).filter(models.PaymentMethod.slug == slug)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == effective_tenant_id)
    return query.first()


def _count_active_payment_methods(
    db: Session,
    exclude_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
) -> int:
    query = db.query(models.PaymentMethod).filter(
        models.PaymentMethod.is_active.is_(True),
        models.PaymentMethod.deleted_at.is_(None),
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == effective_tenant_id)
    if exclude_id is not None:
        query = query.filter(models.PaymentMethod.id != exclude_id)
    return query.count()


def _ensure_slug_available(
    db: Session,
    slug: str,
    current_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
) -> None:
    normalized = _normalize_slug(slug)
    query = db.query(models.PaymentMethod).filter(models.PaymentMethod.slug == normalized)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == effective_tenant_id)
    if current_id is not None:
        query = query.filter(models.PaymentMethod.id != current_id)
    if query.first():
        raise ValueError("Ya existe un método con ese slug")


def _next_order_index(db: Session, tenant_id: Optional[int] = None) -> int:
    query = db.query(func.max(models.PaymentMethod.order_index))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == effective_tenant_id)
    max_value = query.scalar()
    return int(max_value or 0) + 10


def create_payment_method(
    db: Session,
    payload: schemas.PaymentMethodCreate,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    slug = _normalize_slug(payload.slug)
    _ensure_slug_available(db, slug, tenant_id=effective_tenant_id)
    order_index = (
        payload.order_index
        if payload.order_index is not None
        else _next_order_index(db, tenant_id=effective_tenant_id)
    )
    method = models.PaymentMethod(
        tenant_id=effective_tenant_id,
        name=payload.name.strip(),
        slug=slug,
        description=payload.description,
        is_active=payload.is_active,
        allow_change=payload.allow_change,
        order_index=order_index,
        color=payload.color,
        icon=payload.icon,
    )
    db.add(method)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "payment_methods" in str(exc).lower() and "slug" in str(exc).lower():
            raise ValueError("Ya existe un método con ese slug") from exc
        raise
    db.refresh(method)
    return method


def update_payment_method(
    db: Session,
    method: models.PaymentMethod,
    payload: schemas.PaymentMethodUpdate,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else method.tenant_id
    data = payload.model_dump(exclude_unset=True)
    if "slug" in data:
        slug = _normalize_slug(data["slug"])
        _ensure_slug_available(
            db,
            slug,
            current_id=method.id,
            tenant_id=effective_tenant_id,
        )
        data["slug"] = slug
    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()

    if "is_active" in data and data["is_active"] is False and method.is_active:
        if _count_active_payment_methods(
            db,
            exclude_id=method.id,
            tenant_id=effective_tenant_id,
        ) == 0:
            raise ValueError("Debe existir al menos un método de pago activo")

    for field, value in data.items():
        setattr(method, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "payment_methods" in str(exc).lower() and "slug" in str(exc).lower():
            raise ValueError("Ya existe un método con ese slug") from exc
        raise
    db.refresh(method)
    return method


def toggle_payment_method(
    db: Session,
    method: models.PaymentMethod,
    is_active: bool,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else method.tenant_id
    if not is_active and method.is_active:
        if _count_active_payment_methods(
            db,
            exclude_id=method.id,
            tenant_id=effective_tenant_id,
        ) == 0:
            raise ValueError("Debe existir al menos un método de pago activo")
    method.is_active = is_active
    db.commit()
    db.refresh(method)
    return method


def reorder_payment_methods(
    db: Session,
    reorder_items: List[schemas.PaymentMethodReorderItem],
    tenant_id: Optional[int] = None,
):
    ids = [item.id for item in reorder_items]
    query = db.query(models.PaymentMethod).filter(models.PaymentMethod.id.in_(ids))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PaymentMethod.tenant_id == effective_tenant_id)
    methods = query.all()
    methods_map = {m.id: m for m in methods}
    if len(methods_map) != len(ids):
        raise ValueError("Algún método de pago no existe")
    for item in reorder_items:
        methods_map[item.id].order_index = item.order_index
    db.commit()
    return list_payment_methods(db, tenant_id=effective_tenant_id)


def delete_payment_method(
    db: Session,
    method: models.PaymentMethod,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else method.tenant_id
    if method.is_active and _count_active_payment_methods(
        db,
        exclude_id=method.id,
        tenant_id=effective_tenant_id,
    ) == 0:
        raise ValueError("Debe existir al menos un método de pago activo")
    method.deleted_at = datetime.utcnow()
    method.is_active = False
    db.commit()
    return method


# ===================== PRODUCT GROUPS =====================


def list_product_groups(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.ProductGroup)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ProductGroup.tenant_id == effective_tenant_id)
    return query.order_by(models.ProductGroup.path.asc()).offset(skip).limit(limit).all()


def get_product_group(
    db: Session,
    group_id: int,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.ProductGroup).filter(models.ProductGroup.id == group_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ProductGroup.tenant_id == effective_tenant_id)
    return query.first()


def get_product_group_by_path(
    db: Session,
    path: str,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.ProductGroup).filter(models.ProductGroup.path == path)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.ProductGroup.tenant_id == effective_tenant_id)
    return query.first()


def _get_catalog_stock_subquery(db: Session, tenant_id: Optional[int]):
    return (
        db.query(
            models.InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .filter(
            models.InventoryMovement.tenant_id == tenant_id
            if tenant_id is not None
            else true()
        )
        .group_by(models.InventoryMovement.product_id)
        .subquery()
    )


def resolve_public_catalog_tenant_id(db: Session) -> Optional[int]:
    return get_default_tenant_id(db)


def resolve_product_web_slug(product: models.Product) -> str:
    manual = (product.web_slug or "").strip()
    if manual:
        return build_product_web_slug(manual)
    return build_product_web_slug(product.name, product.sku)


def resolve_product_web_name(product: models.Product) -> str:
    manual = (product.web_name or "").strip()
    if manual:
        return manual
    return product.name


def resolve_web_product_stock_status(
    product: models.Product,
    qty_on_hand: Optional[float],
) -> str:
    if product.service:
        return "service"
    price_mode = (product.web_price_mode or "visible").strip().lower()
    if price_mode == "consultar" and qty_on_hand is None:
        return "consultar"
    qty = float(qty_on_hand or 0.0)
    if qty <= 0:
        return "out_of_stock"
    if product.low_stock_alert and product.stock_min > 0 and qty <= product.stock_min:
        return "low_stock"
    return "in_stock"


def _build_web_catalog_filters(
    db: Session,
    tenant_id: Optional[int],
    q: Optional[str] = None,
    category: Optional[str] = None,
    featured: Optional[bool] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
):
    stock_subquery = _get_catalog_stock_subquery(db, tenant_id)
    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)
    query = (
        db.query(models.Product)
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .filter(
            models.Product.active.is_(True),
            models.Product.web_published.is_(True),
        )
    )
    if tenant_id is not None:
        query = query.filter(models.Product.tenant_id == tenant_id)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                models.Product.name.ilike(like),
                models.Product.sku.ilike(like),
                models.Product.barcode.ilike(like),
                models.Product.brand.ilike(like),
                models.Product.group_name.ilike(like),
                models.Product.web_category_key.ilike(like),
                models.Product.web_short_description.ilike(like),
            )
        )
    if featured is not None:
        query = query.filter(models.Product.web_featured.is_(featured))
    category_map_with_inactive = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=tenant_id,
        include_inactive=True,
        ensure_seeded=True,
    )
    inactive_category_keys = {
        _normalize_web_catalog_category_key(item.key)
        for item in category_map_with_inactive.values()
        if not bool(item.is_active)
    }
    if inactive_category_keys:
        query = query.filter(
            or_(
                models.Product.web_category_key.is_(None),
                ~models.Product.web_category_key.in_(inactive_category_keys),
            )
        )
    query = query.filter(
        or_(
            models.Product.web_visible_when_out_of_stock.is_(True),
            models.Product.web_visible_when_out_of_stock.is_(None),
            models.Product.service.is_(True),
            qty_col > 0,
        )
    )
    category_map = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=tenant_id,
        include_inactive=False,
        ensure_seeded=True,
    )
    children_map = _build_web_catalog_category_children_map(list(category_map.values()))
    if category:
        normalized_category = _normalize_web_catalog_category_key(category)
        if normalized_category:
            filter_keys = _get_web_catalog_descendant_keys(normalized_category, children_map)
            if filter_keys:
                query = query.filter(models.Product.web_category_key.in_(filter_keys))

    sale_price_col = _build_web_sale_price_sql_expression()
    price_bounds_query = query.filter(
        func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "visible"
    )
    min_value = price_bounds_query.with_entities(func.min(sale_price_col)).scalar()
    max_value = price_bounds_query.with_entities(func.max(sale_price_col)).scalar()

    if min_price is not None:
        query = query.filter(
            func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "visible",
            sale_price_col >= float(min_price),
        )
    if max_price is not None:
        query = query.filter(
            func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "visible",
            sale_price_col <= float(max_price),
        )

    category_rows = (
        query.with_entities(
            models.Product.web_category_key,
            func.count(models.Product.id),
        )
        .filter(models.Product.web_category_key.isnot(None))
        .group_by(models.Product.web_category_key)
        .order_by(func.count(models.Product.id).desc(), models.Product.web_category_key.asc())
        .all()
    )
    direct_category_counts = {
        _normalize_web_catalog_category_key(path): int(count or 0)
        for path, count in category_rows
        if path
    }
    category_defs = list(category_map.values())
    categories = [
        schemas.WebCatalogFilterOption(
            value=item.key,
            label=item.name,
            count=sum(
                direct_category_counts.get(descendant_key, 0)
                for descendant_key in _get_web_catalog_descendant_keys(
                    _normalize_web_catalog_category_key(item.key),
                    children_map,
                )
            ),
            level=_get_web_catalog_category_level(
                _normalize_web_catalog_category_key(item.key),
                category_map,
            ),
            parent_value=_normalize_web_catalog_category_key(item.parent_key) or None,
        )
        for item in category_defs
    ]

    brand_rows = (
        query.with_entities(
            models.Product.brand,
            func.count(models.Product.id),
        )
        .filter(models.Product.brand.isnot(None))
        .group_by(models.Product.brand)
        .order_by(func.count(models.Product.id).desc(), models.Product.brand.asc())
        .all()
    )
    brands = [
        schemas.WebCatalogFilterOption(
            value=brand,
            label=brand,
            count=int(count or 0),
        )
        for brand, count in brand_rows
        if brand
    ]
    return schemas.WebCatalogFilters(
        categories=categories,
        brands=brands,
        price_min=float(min_value) if min_value is not None else 0.0,
        price_max=float(max_value) if max_value is not None else 0.0,
    )


def get_web_catalog_version(
    db: Session,
    tenant_id: Optional[int] = None,
) -> schemas.WebCatalogVersion:
    effective_tenant_id = tenant_id if tenant_id is not None else resolve_public_catalog_tenant_id(db)
    products_query = db.query(models.Product).filter(
        models.Product.active.is_(True),
        models.Product.web_published.is_(True),
    )
    groups_query = (
        db.query(models.ProductGroup)
        .join(models.Product, models.Product.group_name == models.ProductGroup.path)
        .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
    )
    if effective_tenant_id is not None:
        products_query = products_query.filter(models.Product.tenant_id == effective_tenant_id)
        groups_query = groups_query.filter(
            models.ProductGroup.tenant_id == effective_tenant_id,
            models.Product.tenant_id == effective_tenant_id,
        )

    products_updated_at = products_query.with_entities(func.max(models.Product.updated_at)).scalar()
    groups_updated_at = groups_query.with_entities(func.max(models.ProductGroup.updated_at)).scalar()
    updated_at = max(
        [ts for ts in [products_updated_at, groups_updated_at] if ts is not None],
        default=None,
    )
    products_count = int(products_query.with_entities(func.count(models.Product.id)).scalar() or 0)
    groups_count = int(
        groups_query.with_entities(func.count(func.distinct(models.ProductGroup.id))).scalar() or 0
    )
    return schemas.WebCatalogVersion(
        updated_at=updated_at,
        products_count=products_count,
        groups_count=groups_count,
    )


def get_web_catalog_categories(
    db: Session,
    tenant_id: Optional[int] = None,
) -> List[schemas.WebCatalogCategory]:
    effective_tenant_id = tenant_id if tenant_id is not None else resolve_public_catalog_tenant_id(db)
    query = (
        db.query(
            models.Product.web_category_key,
            func.count(models.Product.id).label("product_count"),
        )
        .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
    )
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    rows = (
        query.group_by(models.Product.web_category_key)
        .order_by(func.count(models.Product.id).desc(), models.Product.web_category_key.asc())
        .all()
    )
    direct_counts = {
        _normalize_web_catalog_category_key(row.web_category_key): int(row.product_count or 0)
        for row in rows
        if row.web_category_key
    }
    categories = _get_tenant_web_catalog_categories(
        db,
        tenant_id=effective_tenant_id,
        include_inactive=False,
        ensure_seeded=True,
    )
    category_map = {
        _normalize_web_catalog_category_key(item.key): item
        for item in categories
        if _normalize_web_catalog_category_key(item.key)
    }
    children_map = _build_web_catalog_category_children_map(categories)
    return [
        schemas.WebCatalogCategory(
            id=item.key,
            path=item.key,
            parent_path=_normalize_web_catalog_category_key(item.parent_key) or None,
            level=_get_web_catalog_category_level(_normalize_web_catalog_category_key(item.key), category_map),
            has_children=not _is_leaf_web_catalog_category(
                _normalize_web_catalog_category_key(item.key),
                children_map,
            ),
            name=item.name,
            image_url=item.image_url,
            tile_color=item.tile_color,
            home_featured=bool(item.home_featured),
            home_featured_order=int(item.home_featured_order or 0),
            product_count=sum(
                direct_counts.get(descendant_key, 0)
                for descendant_key in _get_web_catalog_descendant_keys(
                    _normalize_web_catalog_category_key(item.key),
                    children_map,
                )
            ),
        )
        for item in categories
        if not _normalize_web_catalog_category_key(item.parent_key)
    ]


def get_web_catalog_products(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    q: Optional[str] = None,
    category: Optional[str] = None,
    brands: Optional[List[str]] = None,
    featured: Optional[bool] = None,
    sort: str = "recommended",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    page: int = 1,
    page_size: int = 24,
) -> schemas.WebCatalogProductList:
    effective_tenant_id = tenant_id if tenant_id is not None else resolve_public_catalog_tenant_id(db)
    category_map = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=effective_tenant_id,
        include_inactive=True,
        ensure_seeded=True,
    )
    children_map = _build_web_catalog_category_children_map(list(category_map.values()))
    inactive_category_keys = {
        _normalize_web_catalog_category_key(item.key)
        for item in category_map.values()
        if not bool(item.is_active)
    }
    stock_subquery = _get_catalog_stock_subquery(db, effective_tenant_id)

    query = (
        db.query(
            models.Product,
            func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand"),
            models.ProductGroup.display_name.label("category_name"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .outerjoin(
            models.ProductGroup,
            and_(
                models.ProductGroup.path == models.Product.group_name,
                models.ProductGroup.tenant_id == models.Product.tenant_id,
            ),
        )
        .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
    )
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    if inactive_category_keys:
        query = query.filter(
            or_(
                models.Product.web_category_key.is_(None),
                ~models.Product.web_category_key.in_(inactive_category_keys),
            )
        )

    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                models.Product.name.ilike(like),
                models.Product.sku.ilike(like),
                models.Product.barcode.ilike(like),
                models.Product.brand.ilike(like),
                models.Product.group_name.ilike(like),
                models.Product.web_category_key.ilike(like),
                models.Product.web_short_description.ilike(like),
            )
        )
    if category:
        normalized_category = _normalize_web_catalog_category_key(category)
        if normalized_category:
            filter_keys = _get_web_catalog_descendant_keys(normalized_category, children_map)
            if filter_keys:
                query = query.filter(models.Product.web_category_key.in_(filter_keys))
    normalized_brands = [
        item.strip().lower() for item in (brands or []) if isinstance(item, str) and item.strip()
    ]
    if normalized_brands:
        query = query.filter(
            models.Product.brand.isnot(None),
            func.lower(func.trim(models.Product.brand)).in_(normalized_brands),
        )
    if featured is not None:
        query = query.filter(models.Product.web_featured.is_(featured))

    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)
    web_sale_price_col = _build_web_sale_price_sql_expression()
    if min_price is not None:
        query = query.filter(
            func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "visible",
            web_sale_price_col >= float(min_price),
        )
    if max_price is not None:
        query = query.filter(
            func.lower(func.coalesce(models.Product.web_price_mode, "visible")) == "visible",
            web_sale_price_col <= float(max_price),
        )
    query = query.filter(
        or_(
            models.Product.web_visible_when_out_of_stock.is_(True),
            models.Product.web_visible_when_out_of_stock.is_(None),
            models.Product.service.is_(True),
            qty_col > 0,
        )
    )
    if sort == "price_asc":
        query = query.order_by(web_sale_price_col.asc(), models.Product.name.asc())
    elif sort == "price_desc":
        query = query.order_by(web_sale_price_col.desc(), models.Product.name.asc())
    elif sort == "name_desc":
        query = query.order_by(models.Product.name.desc())
    elif sort == "name_asc":
        query = query.order_by(models.Product.name.asc())
    else:
        query = query.order_by(
            models.Product.web_featured.desc(),
            models.Product.web_sort_order.asc(),
            case((qty_col > 0, 0), else_=1),
            models.Product.name.asc(),
        )

    total = query.count()
    skip = max(page - 1, 0) * page_size
    rows = query.offset(skip).limit(page_size).all()
    items: List[schemas.WebCatalogProductCard] = []
    for product, qty_on_hand, category_name in rows:
        category_def = category_map.get(_normalize_web_catalog_category_key(product.web_category_key))
        stock_status = resolve_web_product_stock_status(product, qty_on_hand)
        if stock_status == "out_of_stock" and product.web_visible_when_out_of_stock is False:
            continue
        price_mode = (product.web_price_mode or "visible").strip().lower()
        sale_price = resolve_web_product_sale_price(product)
        price = sale_price if price_mode == "visible" else None
        items.append(
            schemas.WebCatalogProductCard(
                id=product.id,
                sku=product.sku,
                slug=resolve_product_web_slug(product),
                name=resolve_product_web_name(product),
                badge_text=(product.web_badge_text or None),
                short_description=product.web_short_description,
                long_description=product.web_long_description,
                brand=product.brand,
                group_name=category_name or product.group_name,
                category_path=product.web_category_key,
                category_name=(category_def.name if category_def else None),
                image_url=product.image_url,
                image_thumb_url=product.image_thumb_url,
                gallery=_build_product_gallery_urls(product),
                video_url=product.web_video_url,
                price_mode=price_mode,
                price=price,
                compare_price=(resolve_web_compare_price(product, sale_price=sale_price) if price_mode == "visible" else None),
                stock_status=stock_status,
                featured=bool(product.web_featured),
            )
        )

    filters = _build_web_catalog_filters(
        db,
        tenant_id=effective_tenant_id,
        q=q,
        category=category,
        featured=featured,
        min_price=min_price,
        max_price=max_price,
    )
    return schemas.WebCatalogProductList(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
        filters=filters,
    )


def get_web_catalog_best_sellers(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    limit: int = 10,
    days: int = 90,
) -> tuple[List[schemas.WebCatalogProductCard], datetime]:
    effective_tenant_id = tenant_id if tenant_id is not None else resolve_public_catalog_tenant_id(db)
    safe_limit = max(1, min(int(limit or 10), 20))
    safe_days = max(7, min(int(days or 90), 365))

    now = datetime.utcnow()
    catalog_updated_at = (
        db.query(func.max(models.Product.updated_at))
        .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
        .filter(
            models.Product.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .scalar()
    )
    catalog_version_token = (
        int(catalog_updated_at.timestamp()) if isinstance(catalog_updated_at, datetime) else 0
    )
    cache_key_prefix = f"{effective_tenant_id}:{safe_limit}:{safe_days}:"
    cache_key = f"{cache_key_prefix}{catalog_version_token}"
    cache_entry = _WEB_BEST_SELLERS_CACHE.get(cache_key)
    if cache_entry:
        expires_at, cached_items, cached_updated_at = cache_entry
        if now < expires_at:
            return list(cached_items), cached_updated_at

    category_map = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=effective_tenant_id,
        include_inactive=True,
        ensure_seeded=True,
    )
    inactive_category_keys = {
        _normalize_web_catalog_category_key(item.key)
        for item in category_map.values()
        if not bool(item.is_active)
    }
    stock_subquery = _get_catalog_stock_subquery(db, effective_tenant_id)
    qty_col = func.coalesce(stock_subquery.c.qty_on_hand, 0)

    cutoff = now - timedelta(days=safe_days)
    ranked_rows = (
        db.query(
            models.SaleItem.product_id.label("product_id"),
            func.sum(func.coalesce(models.SaleItem.quantity, 0)).label("qty_sold"),
            func.sum(func.coalesce(models.SaleItem.total, 0)).label("gross_sold"),
            func.count(models.SaleItem.id).label("line_count"),
            func.max(models.Sale.created_at).label("last_sale_at"),
        )
        .join(models.Sale, models.Sale.id == models.SaleItem.sale_id)
        .join(models.Product, models.Product.id == models.SaleItem.product_id)
        .filter(models.Sale.created_at >= cutoff)
        .filter(models.Sale.voided_at.is_(None))
        .filter(func.coalesce(models.SaleItem.quantity, 0) > 0)
        .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
        .filter(
            models.Product.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .filter(
            models.Sale.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .group_by(models.SaleItem.product_id)
        .order_by(
            func.sum(func.coalesce(models.SaleItem.quantity, 0)).desc(),
            func.sum(func.coalesce(models.SaleItem.total, 0)).desc(),
            models.SaleItem.product_id.asc(),
        )
        .limit(max(safe_limit * 8, 40))
        .all()
    )
    ranked_ids = [int(row.product_id) for row in ranked_rows if getattr(row, "product_id", None)]
    metrics_by_product: Dict[int, Dict[str, Any]] = {}
    for row in ranked_rows:
        if not getattr(row, "product_id", None):
            continue
        product_id = int(row.product_id)
        metrics_by_product[product_id] = {
            "qty_sold": float(row.qty_sold or 0),
            "gross_sold": float(row.gross_sold or 0),
            "line_count": float(row.line_count or 0),
            "last_sale_at": row.last_sale_at,
        }

    query = (
        db.query(
            models.Product,
            func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand"),
            models.ProductGroup.display_name.label("category_name"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .outerjoin(
            models.ProductGroup,
            and_(
                models.ProductGroup.path == models.Product.group_name,
                models.ProductGroup.tenant_id == models.Product.tenant_id,
            ),
        )
        .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
        .filter(
            models.Product.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .filter(models.Product.id.in_(ranked_ids) if ranked_ids else false())
        .order_by(models.Product.id.asc())
    )

    base_rows = query.all()

    # Pool ampliado para mezclar rotación real + productos de identidad (ticket/margen/featured).
    pool_rows = list(base_rows)
    if len(pool_rows) < max(safe_limit * 3, 30):
        enrichment_rows = (
            db.query(
                models.Product,
                func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand"),
                models.ProductGroup.display_name.label("category_name"),
            )
            .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
            .outerjoin(
                models.ProductGroup,
                and_(
                    models.ProductGroup.path == models.Product.group_name,
                    models.ProductGroup.tenant_id == models.Product.tenant_id,
                ),
            )
            .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
            .filter(
                models.Product.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            )
            .filter(
                or_(
                    models.Product.web_featured.is_(True),
                    models.Product.web_sort_order <= 40,
                )
            )
            .order_by(
                models.Product.web_featured.desc(),
                models.Product.web_sort_order.asc(),
                models.Product.updated_at.desc(),
            )
            .limit(max(safe_limit * 6, 50))
            .all()
        )
        seen_pool_ids: set[int] = {int(product.id) for product, _, _ in pool_rows}
        for row in enrichment_rows:
            product = row[0]
            if int(product.id) in seen_pool_ids:
                continue
            pool_rows.append(row)
            seen_pool_ids.add(int(product.id))

    max_qty = max((m["qty_sold"] for m in metrics_by_product.values()), default=0.0)
    max_gross = max((m["gross_sold"] for m in metrics_by_product.values()), default=0.0)
    price_candidates = [
        float(resolve_web_product_sale_price(product) or 0)
        for product, _, _ in pool_rows
        if float(resolve_web_product_sale_price(product) or 0) > 0
    ]
    max_price = max(price_candidates, default=0.0)

    def _safe_norm(value: float, max_value: float) -> float:
        if max_value <= 0:
            return 0.0
        return max(0.0, min(1.0, value / max_value))

    def _compute_score(product: models.Product) -> float:
        metrics = metrics_by_product.get(int(product.id), {})
        qty_score = _safe_norm(float(metrics.get("qty_sold", 0.0) or 0.0), max_qty)
        gross_score = _safe_norm(float(metrics.get("gross_sold", 0.0) or 0.0), max_gross)
        last_sale_at = metrics.get("last_sale_at")
        recency_score = 0.0
        if isinstance(last_sale_at, datetime):
            age_days = max(0.0, (now - last_sale_at).total_seconds() / 86400.0)
            recency_score = math.exp(-age_days / max(14.0, safe_days / 3.0))
        sale_price = float(resolve_web_product_sale_price(product) or 0.0)
        cost_value = float(product.cost or 0.0)
        margin_score = 0.0
        if sale_price > 0:
            margin_score = max(0.0, min(1.0, (sale_price - cost_value) / sale_price))
        price_identity_score = _safe_norm(sale_price, max_price)
        featured_bonus = 1.0 if bool(product.web_featured) else 0.0

        # Prioriza ventas reales, pero empuja identidad/margen para no llenar de solo rotación.
        return (
            qty_score * 0.34
            + gross_score * 0.20
            + recency_score * 0.16
            + margin_score * 0.15
            + price_identity_score * 0.10
            + featured_bonus * 0.05
        )

    candidates: list[tuple[float, Any]] = []
    for product, qty_on_hand, category_name in pool_rows:
        normalized_category_key = _normalize_web_catalog_category_key(product.web_category_key)
        if normalized_category_key and normalized_category_key in inactive_category_keys:
            continue
        candidates.append((_compute_score(product), (product, qty_on_hand, category_name)))
    candidates.sort(key=lambda entry: (-entry[0], int(entry[1][0].web_sort_order or 999999), -int(entry[1][0].id or 0)))

    brand_cap = max(2, int(math.ceil(safe_limit * 0.35)))
    category_cap = max(2, int(math.ceil(safe_limit * 0.40)))
    brand_counts: Dict[str, int] = defaultdict(int)
    category_counts: Dict[str, int] = defaultdict(int)
    selected_rows: list[Any] = []
    selected_ids: set[int] = set()

    for _, row in candidates:
        product = row[0]
        pid = int(product.id)
        if pid in selected_ids:
            continue
        brand_key = (product.brand or "__none__").strip().lower()
        category_key = (_normalize_web_catalog_category_key(product.web_category_key) or (product.group_name or "__none__")).strip().lower()
        if brand_counts[brand_key] >= brand_cap:
            continue
        if category_counts[category_key] >= category_cap:
            continue
        selected_rows.append(row)
        selected_ids.add(pid)
        brand_counts[brand_key] += 1
        category_counts[category_key] += 1
        if len(selected_rows) >= safe_limit:
            break

    if len(selected_rows) < safe_limit:
        for _, row in candidates:
            product = row[0]
            pid = int(product.id)
            if pid in selected_ids:
                continue
            selected_rows.append(row)
            selected_ids.add(pid)
            if len(selected_rows) >= safe_limit:
                break

    items: List[schemas.WebCatalogProductCard] = []
    for product, qty_on_hand, category_name in selected_rows:
        normalized_category_key = _normalize_web_catalog_category_key(product.web_category_key)
        category_def = category_map.get(normalized_category_key)
        stock_status = resolve_web_product_stock_status(product, qty_on_hand)
        if stock_status == "out_of_stock" and product.web_visible_when_out_of_stock is False:
            continue
        price_mode = (product.web_price_mode or "visible").strip().lower()
        sale_price = resolve_web_product_sale_price(product)
        items.append(
            schemas.WebCatalogProductCard(
                id=product.id,
                sku=product.sku,
                slug=resolve_product_web_slug(product),
                name=resolve_product_web_name(product),
                badge_text=(product.web_badge_text or None),
                short_description=product.web_short_description,
                long_description=product.web_long_description,
                brand=product.brand,
                group_name=category_name or product.group_name,
                category_path=product.web_category_key,
                category_name=(category_def.name if category_def else None),
                image_url=product.image_url,
                image_thumb_url=product.image_thumb_url,
                gallery=_build_product_gallery_urls(product),
                video_url=product.web_video_url,
                price_mode=price_mode,
                price=(sale_price if price_mode == "visible" else None),
                compare_price=(resolve_web_compare_price(product, sale_price=sale_price) if price_mode == "visible" else None),
                stock_status=stock_status,
                featured=bool(product.web_featured),
            )
        )

    if len(items) < safe_limit:
        fallback_rows = (
            db.query(
                models.Product,
                func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand"),
                models.ProductGroup.display_name.label("category_name"),
            )
            .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
            .outerjoin(
                models.ProductGroup,
                and_(
                    models.ProductGroup.path == models.Product.group_name,
                    models.ProductGroup.tenant_id == models.Product.tenant_id,
                ),
            )
            .filter(models.Product.active.is_(True), models.Product.web_published.is_(True))
            .filter(
                models.Product.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            )
            .filter(~models.Product.id.in_(list(selected_ids)) if selected_ids else true())
            .filter(or_(models.Product.web_featured.is_(True), models.Product.web_sort_order <= 20))
            .order_by(
                models.Product.web_featured.desc(),
                models.Product.web_sort_order.asc(),
                models.Product.updated_at.desc(),
                models.Product.name.asc(),
            )
            .limit(max(safe_limit * 2, 20))
            .all()
        )
        for product, qty_on_hand, category_name in fallback_rows:
            normalized_category_key = _normalize_web_catalog_category_key(product.web_category_key)
            if normalized_category_key and normalized_category_key in inactive_category_keys:
                continue
            stock_status = resolve_web_product_stock_status(product, qty_on_hand)
            if stock_status == "out_of_stock" and product.web_visible_when_out_of_stock is False:
                continue
            category_def = category_map.get(normalized_category_key)
            price_mode = (product.web_price_mode or "visible").strip().lower()
            sale_price = resolve_web_product_sale_price(product)
            items.append(
                schemas.WebCatalogProductCard(
                    id=product.id,
                    sku=product.sku,
                    slug=resolve_product_web_slug(product),
                    name=resolve_product_web_name(product),
                    badge_text=(product.web_badge_text or None),
                    short_description=product.web_short_description,
                    long_description=product.web_long_description,
                    brand=product.brand,
                    group_name=category_name or product.group_name,
                    category_path=product.web_category_key,
                    category_name=(category_def.name if category_def else None),
                    image_url=product.image_url,
                    image_thumb_url=product.image_thumb_url,
                    gallery=_build_product_gallery_urls(product),
                    video_url=product.web_video_url,
                    price_mode=price_mode,
                    price=(sale_price if price_mode == "visible" else None),
                    compare_price=(resolve_web_compare_price(product, sale_price=sale_price) if price_mode == "visible" else None),
                    stock_status=stock_status,
                    featured=bool(product.web_featured),
                )
            )
            if len(items) >= safe_limit:
                break

    updated_at = now
    ttl_seconds = _web_best_sellers_cache_ttl_seconds()
    for stale_key in list(_WEB_BEST_SELLERS_CACHE.keys()):
        if stale_key.startswith(cache_key_prefix) and stale_key != cache_key:
            _WEB_BEST_SELLERS_CACHE.pop(stale_key, None)

    _WEB_BEST_SELLERS_CACHE[cache_key] = (
        now + timedelta(seconds=ttl_seconds),
        list(items),
        updated_at,
    )
    return items, updated_at


def get_web_catalog_product_by_slug(
    db: Session,
    slug: str,
    tenant_id: Optional[int] = None,
) -> Optional[schemas.WebCatalogProductDetail]:
    effective_tenant_id = tenant_id if tenant_id is not None else resolve_public_catalog_tenant_id(db)
    category_map = _get_tenant_web_catalog_category_map(
        db,
        tenant_id=effective_tenant_id,
        include_inactive=True,
        ensure_seeded=True,
    )
    inactive_category_keys = {
        _normalize_web_catalog_category_key(item.key)
        for item in category_map.values()
        if not bool(item.is_active)
    }
    stock_subquery = _get_catalog_stock_subquery(db, effective_tenant_id)
    rows = (
        db.query(
            models.Product,
            func.coalesce(stock_subquery.c.qty_on_hand, 0).label("qty_on_hand"),
            models.ProductGroup.display_name.label("category_name"),
        )
        .outerjoin(stock_subquery, stock_subquery.c.product_id == models.Product.id)
        .outerjoin(
            models.ProductGroup,
            and_(
                models.ProductGroup.path == models.Product.group_name,
                models.ProductGroup.tenant_id == models.Product.tenant_id,
            ),
        )
        .filter(
            models.Product.active.is_(True),
            models.Product.web_published.is_(True),
            models.Product.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true(),
        )
        .all()
    )
    normalized_slug = build_product_web_slug(slug)
    for product, qty_on_hand, category_name in rows:
        normalized_category_key = _normalize_web_catalog_category_key(product.web_category_key)
        if normalized_category_key and normalized_category_key in inactive_category_keys:
            continue
        category_def = category_map.get(_normalize_web_catalog_category_key(product.web_category_key))
        resolved_slug = resolve_product_web_slug(product)
        if resolved_slug != normalized_slug:
            continue
        stock_status = resolve_web_product_stock_status(product, qty_on_hand)
        if stock_status == "out_of_stock" and product.web_visible_when_out_of_stock is False:
            return None
        price_mode = (product.web_price_mode or "visible").strip().lower()
        sale_price = resolve_web_product_sale_price(product)
        return schemas.WebCatalogProductDetail(
            id=product.id,
            sku=product.sku,
            slug=resolved_slug,
            name=resolve_product_web_name(product),
            badge_text=(product.web_badge_text or None),
            featured=bool(product.web_featured),
            short_description=product.web_short_description,
            long_description=product.web_long_description,
            brand=product.brand,
            group_name=category_name or product.group_name,
            category_path=product.web_category_key,
            category_name=(category_def.name if category_def else None),
            image_url=product.image_url,
            image_thumb_url=product.image_thumb_url,
            gallery=_build_product_gallery_urls(product),
            video_url=product.web_video_url,
            price_mode=price_mode,
            price=(sale_price if price_mode == "visible" else None),
            compare_price=(resolve_web_compare_price(product, sale_price=sale_price) if price_mode == "visible" else None),
            stock_status=stock_status,
            warranty_text=product.web_warranty_text,
            specs={},
            whatsapp_message=product.web_whatsapp_message,
        )
    return None


def create_product_group(
    db: Session,
    group_in: schemas.ProductGroupCreate,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    existing = get_product_group_by_path(db, group_in.path, tenant_id=effective_tenant_id)
    if existing:
        raise ValueError("Ya existe un grupo con ese path")

    group = models.ProductGroup(
        tenant_id=effective_tenant_id,
        path=group_in.path,
        display_name=group_in.display_name,
        parent_path=group_in.parent_path,
        image_url=group_in.image_url,
        image_thumb_url=group_in.image_thumb_url,
        tile_color=group_in.tile_color,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def update_product_group(
    db: Session,
    group: models.ProductGroup,
    group_in: schemas.ProductGroupUpdate,
):
    data = group_in.model_dump(exclude_unset=True)
    if "path" in data and data["path"] != group.path:
        existing = get_product_group_by_path(db, data["path"], tenant_id=group.tenant_id)
        if existing:
            raise ValueError("Ya existe un grupo con ese path")

    for field, value in data.items():
        setattr(group, field, value)
    db.commit()
    db.refresh(group)
    return group


# ===================== SALES =====================


def create_sale(
    db: Session,
    sale_in: schemas.SaleCreate,
    created_by_user_id: int | None = None,
    tenant_id: int | None = None,
) -> models.Sale:
    """
    Crea una venta con sus ítems y pagos.

    - Si sale_in.payments viene con datos, usamos esa lista.
    - Si no, creamos un único pago con sale_in.payment_method y sale_in.paid_amount.
    - El total de la venta se calcula de forma "pro" a partir de los ítems
      (subtotal - descuentos por línea). Si la diferencia con sale_in.total
      es mínima, respetamos el valor enviado; si es grande, usamos el calculado.
    """

    # 1) Determinar pagos
    payments_data: List[schemas.SalePaymentCreate] = []

    if sale_in.payments and len(sale_in.payments) > 0:
        payments_data = list(sale_in.payments)
    else:
        payments_data = [
            schemas.SalePaymentCreate(
                method=sale_in.payment_method,
                amount=sale_in.paid_amount,
            )
        ]

    total_paid = sum(p.amount for p in payments_data)

    # 2) Calcular totales a partir de los ítems (forma pro)
    items_calc: List[dict] = []
    subtotal_items = 0.0
    total_discount = 0.0

    cart_discount_percent = float(
        getattr(sale_in, "cart_discount_percent", 0.0) or 0.0
    )

    for item_in in sale_in.items:
        # Precio original por unidad (si no viene, usamos unit_price)
        unit_price_original = float(
            getattr(item_in, "unit_price_original", None)
            or item_in.unit_price
        )

        quantity = float(item_in.quantity)
        line_discount_field = getattr(item_in, "line_discount_value", None)
        legacy_discount_field = getattr(item_in, "discount", None)

        if line_discount_field is not None:
            line_discount = float(line_discount_field or 0.0)
        elif legacy_discount_field is not None:
            line_discount = float(legacy_discount_field or 0.0)
        else:
            # Diferencia entre precio original y el cobrado
            line_discount = max(
                0.0,
                (unit_price_original - float(item_in.unit_price)) * quantity,
            )

        line_gross = quantity * unit_price_original
        line_net = max(0.0, line_gross - line_discount)

        unit_price_net = (
            line_net / quantity if quantity != 0 else float(item_in.unit_price)
        )

        subtotal_items += line_gross
        total_discount += line_discount

        items_calc.append(
            {
                "product_id": item_in.product_id,
                "product_sku": item_in.product_sku,
                "product_name": item_in.product_name,
                "product_barcode": item_in.product_barcode,
                "quantity": item_in.quantity,
                "unit_price": unit_price_net,
                "unit_price_original": unit_price_original,
                "discount": line_discount,
                "line_discount_value": line_discount,
                "total": line_net,
            }
        )

    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if tenant_id is None and created_by_user_id:
        creator = db.query(models.PosUser).filter(models.PosUser.id == created_by_user_id).first()
        if creator:
            effective_tenant_id = resolve_user_tenant_id(db, creator)

    customer_payload = {
        "customer_id": getattr(sale_in, "customer_id", None),
        "customer_name": getattr(sale_in, "customer_name", None),
        "customer_phone": getattr(sale_in, "customer_phone", None),
        "customer_email": getattr(sale_in, "customer_email", None),
        "customer_tax_id": getattr(sale_in, "customer_tax_id", None),
        "customer_address": getattr(sale_in, "customer_address", None),
    }

    if customer_payload["customer_id"] is not None:
        customer = get_pos_customer(
            db,
            customer_payload["customer_id"],
            tenant_id=effective_tenant_id,
        )
        if not customer or not customer.is_active:
            raise ValueError("El cliente seleccionado no existe o está inactivo")
        customer_payload.update(
            customer_id=customer.id,
            customer_name=customer.name,
            customer_phone=customer.phone,
            customer_email=customer.email,
            customer_tax_id=customer.tax_id,
            customer_address=customer.address,
        )
    else:
        for key in [
            "customer_name",
            "customer_phone",
            "customer_email",
            "customer_tax_id",
            "customer_address",
        ]:
            customer_payload[key] = _clean_field(customer_payload.get(key))

    subtotal_after_lines = max(0.0, subtotal_items - total_discount)
    surcharge_amount = float(getattr(sale_in, "surcharge_amount", 0.0) or 0.0)
    if surcharge_amount < 0:
        surcharge_amount = 0.0
    sale_total = max(0.0, float(sale_in.total))
    effective_without_surcharge = max(0.0, sale_total - surcharge_amount)
    cart_discount_value = max(0.0, subtotal_after_lines - effective_without_surcharge)
    surcharge_label = _clean_field(getattr(sale_in, "surcharge_label", None))

    change_amount = max(0.0, total_paid - sale_total)

    # Método principal de pago:
    if len(payments_data) == 1:
        main_method = payments_data[0].method
    else:
        main_method = "mixed"

    reservation_id = getattr(sale_in, "reservation_id", None)
    reservation: Optional[models.SaleNumberReservation] = None
    reserved_document_number: Optional[str] = None
    if reservation_id is not None:
        reservation = (
            db.query(models.SaleNumberReservation)
            .filter(
                models.SaleNumberReservation.id == reservation_id,
                models.SaleNumberReservation.status == "reserved",
                (
                    models.SaleNumberReservation.tenant_id == effective_tenant_id
                    if effective_tenant_id is not None
                    else true()
                ),
            )
            .first()
        )
        if not reservation:
            raise ValueError("La reserva de número de venta no es válida")
        sale_number_preassigned = reservation.sale_number
        reserved_document_number = reservation.document_number
    else:
        sale_number_preassigned = getattr(sale_in, "sale_number_preassigned", None)
    if sale_number_preassigned is not None:
        existing_sale_number = (
            db.query(models.Sale)
            .filter(
                models.Sale.sale_number == sale_number_preassigned,
                (
                    models.Sale.tenant_id == effective_tenant_id
                    if effective_tenant_id is not None
                    else true()
                ),
            )
            .first()
        )
        if existing_sale_number:
            raise ValueError(
                f"El número de ticket {sale_number_preassigned} ya existe en otra venta"
            )
        if reservation is None:
            existing_reservation = (
                db.query(models.SaleNumberReservation)
                .filter(
                    models.SaleNumberReservation.sale_number == sale_number_preassigned,
                    models.SaleNumberReservation.status == "reserved",
                    (
                        models.SaleNumberReservation.tenant_id == effective_tenant_id
                        if effective_tenant_id is not None
                        else true()
                    ),
                )
                .first()
            )
            if existing_reservation:
                raise ValueError(
                    f"El número de ticket {sale_number_preassigned} está reservado"
                )

    # 3) Crear la venta (aún sin sale_number / document_number)
    pos_name = _clean_field(getattr(sale_in, "pos_name", None))
    station_id = _resolve_station_id(
        db,
        getattr(sale_in, "station_id", None),
        tenant_id=effective_tenant_id,
    )
    is_pos_web = _is_pos_web_name(pos_name)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede registrar estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(db, pos_name, tenant_id=effective_tenant_id)
    if not station_id and not is_pos_web and _tenant_requires_station(
        db, tenant_id=effective_tenant_id
    ):
        raise ValueError("Debe seleccionar una estación para registrar la venta")
    if reservation is None and is_pos_web and sale_number_preassigned is None:
        for _ in range(3):
            auto_reservation = reserve_sale_number(
                db,
                pos_name=pos_name,
                station_id=station_id,
                reserved_by_user_id=created_by_user_id,
                tenant_id=effective_tenant_id,
            )
            duplicated_sale = (
                db.query(models.Sale)
                .filter(
                    or_(
                        models.Sale.sale_number == auto_reservation.sale_number,
                        models.Sale.document_number == auto_reservation.document_number,
                    ),
                    (
                        models.Sale.tenant_id == effective_tenant_id
                        if effective_tenant_id is not None
                        else true()
                    ),
                )
                .first()
            )
            if duplicated_sale:
                auto_reservation.status = "cancelled"
                db.add(auto_reservation)
                db.commit()
                continue
            reservation = auto_reservation
            sale_number_preassigned = reservation.sale_number
            reserved_document_number = reservation.document_number
            break
        if reservation is None:
            raise ValueError(
                "No se pudo reservar un ticket para POS web. Intenta de nuevo en unos segundos."
            )
    if reservation is None and not is_pos_web and _tenant_requires_station(
        db, tenant_id=effective_tenant_id
    ):
        raise ValueError("Debe reservar el número de venta antes de registrar.")
    if reservation:
        if reservation.station_id != station_id:
            raise ValueError("La reserva no corresponde a esta estación")
        if reservation.pos_name and pos_name and reservation.pos_name != pos_name:
            raise ValueError("La reserva no corresponde a este POS")

    sale = models.Sale(
        tenant_id=effective_tenant_id,
        total=sale_total,
        paid_amount=total_paid,
        change_amount=change_amount,
        main_payment_method=main_method,
        # compatibilidad con código existente (dashboards, etc.)
        payment_method=main_method,
        cart_discount_value=cart_discount_value,
        cart_discount_percent=cart_discount_percent,
        customer_id=customer_payload["customer_id"],
        customer_name=customer_payload["customer_name"],
        customer_phone=customer_payload["customer_phone"],
        customer_email=customer_payload["customer_email"],
        customer_tax_id=customer_payload["customer_tax_id"],
        customer_address=customer_payload["customer_address"],
        notes=sale_in.notes,
        pos_name=pos_name,
        station_id=station_id,
        vendor_name=sale_in.vendor_name,
        sale_number=sale_number_preassigned,
        document_number=reserved_document_number,
        surcharge_amount=surcharge_amount,
        surcharge_label=surcharge_label,
    )

    db.add(sale)
    try:
        db.flush()  # para obtener sale.id
    except IntegrityError as exc:
        db.rollback()
        error_text = str(exc).lower()
        if "sales" in error_text and "document_number" in error_text:
            detail = str(exc)
            document_match = re.search(r"\(document_number\)=\(([^)]+)\)", detail)
            document_number = (
                document_match.group(1).strip()
                if document_match
                else (reserved_document_number or f"V-{sale_number_preassigned or 0:06d}")
            )
            raise ValueError(
                f"El ticket {document_number} ya existe en esta empresa. Actualiza y vuelve a intentar."
            ) from exc
        if "sales" in error_text and "sale_number" in error_text:
            detail = str(exc)
            sale_match = re.search(r"\(sale_number\)=\(([^)]+)\)", detail)
            sale_number_value = (
                sale_match.group(1).strip()
                if sale_match
                else str(sale_number_preassigned or "")
            )
            raise ValueError(
                f"El número de ticket {sale_number_value} ya existe en esta empresa."
            ) from exc
        raise

    # 3b) Generar número de ticket y documento basados en id
    if sale.sale_number is None:
        sale.sale_number = sale.id

    if not sale.document_number:
        doc_number_source = sale.sale_number or sale.id
        sale.document_number = f"V-{doc_number_source:06d}"

    # 4) Crear ítems (ya conocemos sale.id)
    product_ids = [item_data["product_id"] for item_data in items_calc]
    product_flags = {}
    if product_ids:
        products = (
            db.query(models.Product)
            .filter(models.Product.id.in_(product_ids))
            .all()
        )
        product_flags = {product.id: product.service for product in products}

    for item_data in items_calc:
        db_item = models.SaleItem(
            tenant_id=effective_tenant_id,
            sale_id=sale.id,
            product_id=item_data["product_id"],
            product_sku=item_data["product_sku"],
            product_name=item_data["product_name"],
            product_barcode=item_data["product_barcode"],
            quantity=item_data["quantity"],
            unit_price=item_data["unit_price"],
            unit_price_original=item_data["unit_price_original"],
            discount=item_data["discount"],
            line_discount_value=item_data["line_discount_value"],
            total=item_data["total"],
        )
        db.add(db_item)

        if not product_flags.get(item_data["product_id"], False):
            movement = models.InventoryMovement(
                tenant_id=effective_tenant_id,
                product_id=item_data["product_id"],
                qty_delta=-abs(float(item_data["quantity"])),
                reason="sale",
                reference_type="sale",
                reference_id=sale.id,
                created_by_user_id=created_by_user_id,
            )
            db.add(movement)

    # 5) Crear registros de pagos
    #    Dejamos el PRIMERO como is_primary=True por ahora.
    for idx, pay in enumerate(payments_data):
        db_payment = models.SalePayment(
            tenant_id=effective_tenant_id,
            sale_id=sale.id,
            method=pay.method,
            amount=pay.amount,
            is_primary=(idx == 0),
        )
        db.add(db_payment)

    if reservation:
        reservation.status = "used"
        reservation.sale_id = sale.id
        db.add(reservation)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        error_text = str(exc).lower()
        if "sales" in error_text and "document_number" in error_text:
            raise ValueError(
                f"El ticket {sale.document_number} ya existe en esta empresa. Actualiza y vuelve a intentar."
            ) from exc
        if "sales" in error_text and "sale_number" in error_text:
            raise ValueError(
                f"El número de ticket {sale.sale_number} ya existe en esta empresa."
            ) from exc
        raise
    db.refresh(sale)
    return sale


# ===================== SEPARATED ORDERS =====================


def create_separated_order(
    db: Session,
    sale: models.Sale,
    separated_in: schemas.SeparatedOrderCreate,
) -> models.SeparatedOrder:
    if sale.separated_order:
        raise ValueError("La venta ya tiene un separado registrado")

    calculated_total = sum(float(item.total or 0.0) for item in sale.items)
    total_amount = calculated_total + float(sale.surcharge_amount or 0.0)
    if total_amount <= 0:
        total_amount = float(sale.total or 0.0)
    paid_amount = float(sale.paid_amount or 0.0)
    change_amount = float(sale.change_amount or 0.0)
    initial_payment = max(0.0, min(total_amount, paid_amount - change_amount))
    balance = max(0.0, total_amount - initial_payment)
    status = "pagado" if balance <= 0.01 else "reservado"

    barcode_value = sale.document_number or (
        str(sale.sale_number) if sale.sale_number is not None else None
    )

    order = models.SeparatedOrder(
        tenant_id=sale.tenant_id or get_default_tenant_id(db),
        sale_id=sale.id,
        customer_id=sale.customer_id,
        customer_name=sale.customer_name,
        customer_phone=sale.customer_phone,
        customer_email=sale.customer_email,
        total_amount=total_amount,
        initial_payment=initial_payment,
        balance=balance,
        due_date=separated_in.due_date,
        status=status,
        sale_document_number=sale.document_number or "",
        sale_number=sale.sale_number,
        barcode=barcode_value,
        notes=sale.notes,
        surcharge_amount=float(sale.surcharge_amount or 0.0),
        surcharge_label=sale.surcharge_label,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_separated_order(
    db: Session,
    order_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.SeparatedOrder]:
    query = db.query(models.SeparatedOrder).filter(models.SeparatedOrder.id == order_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SeparatedOrder.tenant_id == effective_tenant_id)
    return query.first()


def list_separated_orders(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    barcode: Optional[str] = None,
    sale_number: Optional[int] = None,
    customer: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
) -> List[models.SeparatedOrder]:
    query = db.query(models.SeparatedOrder)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SeparatedOrder.tenant_id == effective_tenant_id)
    if barcode:
        normalized = barcode.strip()
        query = query.filter(
            or_(
                models.SeparatedOrder.sale_document_number == normalized,
                models.SeparatedOrder.barcode == normalized,
            )
        )
    if sale_number is not None:
        query = query.filter(models.SeparatedOrder.sale_number == sale_number)
    if customer:
        query = query.filter(
            models.SeparatedOrder.customer_name.ilike(f"%{customer.strip()}%")
        )
    if status:
        query = query.filter(models.SeparatedOrder.status == status)
    if date_from is not None:
        query = query.filter(models.SeparatedOrder.created_at >= date_from)
    if date_to is not None:
        query = query.filter(models.SeparatedOrder.created_at < date_to)
    return (
        query.order_by(models.SeparatedOrder.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def add_separated_order_payment(
    db: Session,
    order: models.SeparatedOrder,
    payment_in: schemas.SeparatedOrderPaymentCreate,
) -> models.SeparatedOrder:
    if order.status == "cancelado":
        raise ValueError("No se pueden registrar abonos en un separado cancelado")
    if order.balance <= 0.01:
        raise ValueError("El separado ya está pagado")
    amount = float(payment_in.amount or 0.0)
    if amount <= 0:
        raise ValueError("El monto del abono debe ser mayor a cero")

    method_slug = (payment_in.method or "").strip().lower()
    forbidden = {"separado", "separated", "credit", "crédito", "credito"}
    if method_slug in forbidden:
        raise ValueError("El método de pago no está permitido para abonos")

    if amount - float(order.balance or 0.0) > 0.01:
        raise ValueError("El abono supera el saldo pendiente")

    effective_tenant_id = order.tenant_id if order.tenant_id is not None else get_default_tenant_id(db)
    station_id = _resolve_station_id(db, payment_in.station_id, tenant_id=effective_tenant_id)
    if not station_id and order.sale.station_id:
        station_id = _resolve_station_id(db, order.sale.station_id, tenant_id=effective_tenant_id)
    is_pos_web = _is_pos_web_name(order.sale.pos_name)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede registrar estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(
            db,
            order.sale.pos_name,
            tenant_id=effective_tenant_id,
        )
    if not station_id and not is_pos_web and _tenant_requires_station(
        db, tenant_id=effective_tenant_id
    ):
        raise ValueError("Debe seleccionar una estación para registrar el abono")

    payment = models.SeparatedOrderPayment(
        separated_order_id=order.id,
        method=payment_in.method,
        amount=amount,
        reference=payment_in.reference,
        note=payment_in.note,
        station_id=station_id,
    )
    db.add(payment)

    new_balance = max(0.0, float(order.balance or 0.0) - amount)
    order.balance = new_balance
    if new_balance <= 0.01:
        order.balance = 0.0
        order.status = "pagado"

    db.commit()
    db.refresh(order)
    return order


def complete_separated_order(
    db: Session,
    order: models.SeparatedOrder,
    notes: Optional[str] = None,
) -> models.SeparatedOrder:
    if order.status == "cancelado":
        raise ValueError("El separado está cancelado")
    if float(order.balance or 0.0) > 0.01:
        raise ValueError("Aún hay saldo pendiente por pagar")
    order.status = "pagado"
    order.completed_at = datetime.utcnow()
    if notes:
        order.notes = notes
    db.commit()
    db.refresh(order)
    return order


def cancel_separated_order(
    db: Session,
    order: models.SeparatedOrder,
    notes: Optional[str] = None,
) -> models.SeparatedOrder:
    if order.status == "pagado":
        raise ValueError("No se puede cancelar un separado pagado")
    order.status = "cancelado"
    order.cancelled_at = datetime.utcnow()
    if notes:
        order.notes = notes
    db.commit()
    db.refresh(order)
    return order


def get_sales(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.Sale)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Sale.tenant_id == effective_tenant_id)
    if date_from is not None:
        query = query.filter(models.Sale.created_at >= date_from)
    if date_to is not None:
        query = query.filter(models.Sale.created_at < date_to)
    return (
        query.order_by(models.Sale.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_sales_history_page(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    term: Optional[str] = None,
    customer: Optional[str] = None,
    payment_method: Optional[str] = None,
    pos_name: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> tuple[list[models.Sale], int]:
    query = db.query(models.Sale)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Sale.tenant_id == effective_tenant_id)

    if date_from is not None:
        query = query.filter(models.Sale.created_at >= date_from)
    if date_to is not None:
        query = query.filter(models.Sale.created_at < date_to)

    cleaned_term = (term or "").strip()
    if cleaned_term:
        like_term = f"%{cleaned_term}%"
        digits_only = "".join(ch for ch in cleaned_term if ch.isdigit())
        sale_number_filters = []
        if digits_only:
            try:
                sale_number_filters.append(models.Sale.sale_number == int(digits_only))
            except ValueError:
                pass
        sale_number_filters.append(cast(models.Sale.sale_number, String).ilike(like_term))
        query = query.outerjoin(
            models.SaleItem, models.SaleItem.sale_id == models.Sale.id
        ).filter(
            or_(
                models.Sale.document_number.ilike(like_term),
                *sale_number_filters,
                models.SaleItem.product_name.ilike(like_term),
                models.SaleItem.product_sku.ilike(like_term),
            )
        )

    cleaned_customer = (customer or "").strip()
    if cleaned_customer:
        query = query.filter(models.Sale.customer_name.ilike(f"%{cleaned_customer}%"))

    cleaned_payment = (payment_method or "").strip().lower()
    if cleaned_payment:
        if cleaned_payment == "separado":
            query = query.filter(models.Sale.separated_order.has())
        else:
            query = query.filter(
                or_(
                    func.lower(models.Sale.payment_method) == cleaned_payment,
                    func.lower(models.Sale.main_payment_method) == cleaned_payment,
                    models.Sale.payments.any(
                        func.lower(models.SalePayment.method) == cleaned_payment
                    ),
                )
            )

    cleaned_pos = (pos_name or "").strip()
    if cleaned_pos:
        query = query.filter(models.Sale.pos_name.ilike(f"%{cleaned_pos}%"))

    total = query.with_entities(func.count(func.distinct(models.Sale.id))).scalar() or 0
    rows = (
        query.distinct()
        .order_by(models.Sale.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return rows, int(total)


def get_next_sale_number(
    db: Session,
    pos_id: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> int:
    """Return the next available sale_number. pos_id reserved for future use."""

    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    sales_query = db.query(models.Sale)
    if effective_tenant_id is not None:
        sales_query = sales_query.filter(models.Sale.tenant_id == effective_tenant_id)
    max_sale_number = sales_query.with_entities(func.max(models.Sale.sale_number)).scalar()
    max_sale_id = sales_query.with_entities(func.max(models.Sale.id)).scalar()
    max_reserved_number = (
        db.query(func.max(models.SaleNumberReservation.sale_number))
        .filter(models.SaleNumberReservation.status == "reserved")
        .filter(
            models.SaleNumberReservation.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .scalar()
    )

    candidates = [
        value
        for value in [max_sale_number, max_sale_id, max_reserved_number]
        if value is not None
    ]
    current = int(max(candidates)) if candidates else 0
    return current + 1


def reserve_sale_number(
    db: Session,
    pos_name: Optional[str] = None,
    station_id: Optional[str] = None,
    reserved_by_user_id: Optional[int] = None,
    min_sale_number: Optional[int] = None,
    tenant_id: Optional[int] = None,
) -> models.SaleNumberReservation:
    pos_name_clean = _clean_field(pos_name)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    station_id = _resolve_station_id(db, station_id, tenant_id=effective_tenant_id)
    is_pos_web = _is_pos_web_name(pos_name_clean)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede registrar estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(db, pos_name_clean, tenant_id=effective_tenant_id)
    if not station_id and not is_pos_web and _tenant_requires_station(
        db, tenant_id=effective_tenant_id
    ):
        raise ValueError("Debe seleccionar una estación para registrar la venta")

    # Cancelar reservas antiguas para evitar saltos por reservas fantasma.
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    cleanup_query = db.query(models.SaleNumberReservation).filter(
        models.SaleNumberReservation.status == "reserved",
        models.SaleNumberReservation.created_at < cutoff,
    )
    if station_id:
        cleanup_query = cleanup_query.filter(
            models.SaleNumberReservation.station_id == station_id
        )
    elif pos_name_clean:
        cleanup_query = cleanup_query.filter(
            models.SaleNumberReservation.pos_name == pos_name_clean
        )
    if effective_tenant_id is not None:
        cleanup_query = cleanup_query.filter(
            models.SaleNumberReservation.tenant_id == effective_tenant_id
        )
    if cleanup_query.count() > 0:
        cleanup_query.update(
            {models.SaleNumberReservation.status: "cancelled"},
            synchronize_session=False,
        )
        db.commit()

    min_value = min_sale_number or 0
    base_next = get_next_sale_number(db, pos_id=station_id, tenant_id=effective_tenant_id)
    candidate = min_value if min_value > 0 else base_next

    for _ in range(50):
        next_number = candidate
        existing_sale = (
            db.query(models.Sale)
            .filter(models.Sale.sale_number == next_number)
            .filter(
                models.Sale.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            )
            .first()
        )
        if existing_sale:
            candidate += 1
            continue
        existing_reserved = (
            db.query(models.SaleNumberReservation)
            .filter(
                models.SaleNumberReservation.sale_number == next_number,
                models.SaleNumberReservation.status == "reserved",
                (
                    models.SaleNumberReservation.tenant_id == effective_tenant_id
                    if effective_tenant_id is not None
                    else true()
                ),
            )
            .first()
        )
        if existing_reserved:
            same_station = (
                station_id and existing_reserved.station_id == station_id
            )
            same_pos = (
                not station_id
                and pos_name_clean
                and existing_reserved.pos_name == pos_name_clean
            )
            if same_station or same_pos:
                existing_reserved.created_at = datetime.utcnow()
                existing_reserved.pos_name = pos_name_clean
                existing_reserved.station_id = station_id
                existing_reserved.reserved_by_user_id = reserved_by_user_id
                try:
                    db.commit()
                    db.refresh(existing_reserved)
                    return existing_reserved
                except IntegrityError:
                    db.rollback()
            candidate += 1
            continue

        existing_cancelled = (
            db.query(models.SaleNumberReservation)
            .filter(
                models.SaleNumberReservation.sale_number == next_number,
                (
                    models.SaleNumberReservation.tenant_id == effective_tenant_id
                    if effective_tenant_id is not None
                    else true()
                ),
            )
            .first()
        )
        if existing_cancelled and existing_cancelled.status == "cancelled":
            existing_cancelled.status = "reserved"
            existing_cancelled.created_at = datetime.utcnow()
            existing_cancelled.pos_name = pos_name_clean
            existing_cancelled.station_id = station_id
            existing_cancelled.reserved_by_user_id = reserved_by_user_id
            existing_cancelled.sale_id = None
            try:
                db.commit()
                db.refresh(existing_cancelled)
                return existing_cancelled
            except IntegrityError:
                db.rollback()
                candidate += 1
                continue

        document_number = f"V-{next_number:06d}"
        reservation = models.SaleNumberReservation(
            tenant_id=effective_tenant_id,
            sale_number=next_number,
            document_number=document_number,
            pos_name=pos_name_clean,
            station_id=station_id,
            reserved_by_user_id=reserved_by_user_id,
            status="reserved",
        )
        db.add(reservation)
        try:
            db.commit()
            db.refresh(reservation)
            return reservation
        except IntegrityError:
            db.rollback()
            candidate += 1
            continue

    raise ValueError("No se pudo reservar el número de venta")


def cancel_sale_reservation(
    db: Session,
    reservation_id: int,
    tenant_id: Optional[int] = None,
) -> models.SaleNumberReservation:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    reservation = (
        db.query(models.SaleNumberReservation)
        .filter(
            models.SaleNumberReservation.id == reservation_id,
            (
                models.SaleNumberReservation.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )
    if not reservation:
        raise ValueError("Reserva no encontrada")
    if reservation.status != "reserved":
        raise ValueError("La reserva ya no está disponible")
    reservation.status = "cancelled"
    db.commit()
    db.refresh(reservation)
    return reservation

def get_sale(
    db: Session,
    sale_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.Sale]:
    query = db.query(models.Sale).filter(models.Sale.id == sale_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Sale.tenant_id == effective_tenant_id)
    return query.first()


def get_sale_by_document(
    db: Session,
    document_number: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.Sale]:
    query = db.query(models.Sale).filter(models.Sale.document_number == document_number)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Sale.tenant_id == effective_tenant_id)
    return query.first()


def get_sale_return(
    db: Session,
    return_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.SaleReturn]:
    query = db.query(models.SaleReturn).filter(models.SaleReturn.id == return_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SaleReturn.tenant_id == effective_tenant_id)
    return query.first()


def list_returns(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.SaleReturn).options(joinedload(models.SaleReturn.sale))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SaleReturn.tenant_id == effective_tenant_id)
    if date_from is not None:
        query = query.filter(models.SaleReturn.created_at >= date_from)
    if date_to is not None:
        query = query.filter(models.SaleReturn.created_at < date_to)
    return (
        query.order_by(models.SaleReturn.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def list_changes(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
):
    query = db.query(models.SaleChange)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SaleChange.tenant_id == effective_tenant_id)
    if date_from is not None:
        query = query.filter(models.SaleChange.created_at >= date_from)
    if date_to is not None:
        query = query.filter(models.SaleChange.created_at < date_to)
    return (
        query.order_by(models.SaleChange.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_sale_change(
    db: Session,
    change_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.SaleChange]:
    query = db.query(models.SaleChange).filter(models.SaleChange.id == change_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SaleChange.tenant_id == effective_tenant_id)
    return query.first()


def create_return(
    db: Session,
    return_in: schemas.SaleReturnCreate,
    tenant_id: Optional[int] = None,
) -> models.SaleReturn:
    if not return_in.items or len(return_in.items) == 0:
        raise ValueError("La devolución debe incluir al menos un ítem")

    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    sale: Optional[models.Sale] = None
    if return_in.sale_id is not None:
        sale = get_sale(db, return_in.sale_id, tenant_id=effective_tenant_id)
    elif return_in.sale_document_number:
        sale = get_sale_by_document(
            db,
            return_in.sale_document_number,
            tenant_id=effective_tenant_id,
        )

    if not sale:
        raise ValueError(
            "No encontramos la venta asociada (usa sale_id o sale_document_number)"
        )

    sale_items = {item.id: item for item in sale.items}
    if not sale_items:
        raise ValueError("La venta seleccionada no tiene ítems registrados")

    confirmed_statuses = {"confirmed"}
    refunded_qty = defaultdict(float)
    for previous_return in sale.returns:
        if previous_return.status not in confirmed_statuses:
            continue
        for previous_item in previous_return.items:
            refunded_qty[previous_item.sale_item_id] += float(
                previous_item.quantity or 0.0
            )

    subtotal_after_lines = sum(float(item.total or 0.0) for item in sale.items)
    cart_discount_value = float(sale.cart_discount_value or 0.0)
    cart_share_per_unit = {}

    for item in sale.items:
        if float(item.quantity or 0) == 0:
            cart_share_per_unit[item.id] = 0.0
            continue

        if subtotal_after_lines > 0 and cart_discount_value > 0:
            share_total = (float(item.total or 0.0) / subtotal_after_lines) * cart_discount_value
            cart_share_per_unit[item.id] = share_total / float(item.quantity)
        else:
            cart_share_per_unit[item.id] = 0.0

    items_data = []
    total_refund = 0.0
    original_total_refund = 0.0

    for item_in in return_in.items:
        sale_item = sale_items.get(item_in.sale_item_id)
        if not sale_item:
            raise ValueError(
                f"El ítem {item_in.sale_item_id} no pertenece a la venta especificada"
            )

        requested_qty = float(item_in.quantity or 0.0)
        if requested_qty <= 0:
            raise ValueError("La cantidad a devolver debe ser mayor a cero")

        already_refunded = refunded_qty[sale_item.id]
        available_qty = float(sale_item.quantity or 0.0) - already_refunded
        if requested_qty - available_qty > 0.0001:
            raise ValueError(
                f"La cantidad disponible para el ítem {sale_item.id} es {available_qty},"
                " no se puede devolver más de lo vendido"
            )

        line_quantity = float(sale_item.quantity or 0.0)
        unit_net_after_line = (
            float(sale_item.total or 0.0) / line_quantity if line_quantity else 0.0
        )
        unit_cart_share = cart_share_per_unit.get(sale_item.id, 0.0)
        unit_refund_value = max(0.0, unit_net_after_line - unit_cart_share)
        line_total_refund = unit_refund_value * requested_qty

        # Descuento por línea correspondiente a la cantidad devuelta
        line_discount_per_unit = (
            float(sale_item.line_discount_value or 0.0) / line_quantity
            if line_quantity
            else 0.0
        )
        line_discount_value = line_discount_per_unit * requested_qty
        cart_discount_share_value = unit_cart_share * requested_qty

        items_data.append(
            {
                "sale_item": sale_item,
                "quantity": requested_qty,
                "reason": item_in.reason,
                "unit_price_original": float(sale_item.unit_price_original or 0.0),
                "unit_price_net": unit_net_after_line,
                "line_discount_value": line_discount_value,
                "cart_discount_share": cart_discount_share_value,
                "total_refund": line_total_refund,
            }
        )

        total_refund += line_total_refund
        refunded_qty[sale_item.id] += requested_qty

    original_total_refund = total_refund
    if total_refund <= 0:
        raise ValueError("El total calculado de la devolución debe ser mayor a cero")

    paid_total = float(sale.total or 0.0)
    if sale.is_separated and sale.separated_order:
        separated = sale.separated_order
        paid_total = float(separated.initial_payment or 0.0) + sum(
            float(payment.amount or 0.0) for payment in separated.payments
        )
    refunded_so_far = float(sale.refunded_total or 0.0)
    available_refund = max(0.0, paid_total - refunded_so_far)

    if sale.is_separated:
        if available_refund <= 0.0:
            raise ValueError(
                "No hay abonos disponibles para reembolsar en esta venta separada"
            )
        if total_refund - available_refund > 0.01:
            ratio = available_refund / total_refund if total_refund else 0.0
            for item_data in items_data:
                item_data["unit_price_net"] = float(item_data["unit_price_net"]) * ratio
                item_data["line_discount_value"] = (
                    float(item_data["line_discount_value"]) * ratio
                )
                item_data["cart_discount_share"] = (
                    float(item_data["cart_discount_share"]) * ratio
                )
                item_data["total_refund"] = float(item_data["total_refund"]) * ratio
            total_refund = available_refund
            pending_cancelled = max(0.0, original_total_refund - total_refund)
            note_prefix = (
                f"Reembolso limitado a abonos (${total_refund:,.0f}). "
                f"Saldo pendiente anulado (${pending_cancelled:,.0f})."
            )
            return_in.notes = (
                f"{note_prefix}\n{return_in.notes}"
                if return_in.notes
                else note_prefix
            )
    else:
        projected_total_refunded = refunded_so_far + total_refund
        if projected_total_refunded - float(sale.total or 0.0) > 0.01:
            raise ValueError("El total devuelto supera el total cobrado en la venta")

    if return_in.payments and len(return_in.payments) > 0:
        payments_payload = list(return_in.payments)
    else:
        source_payments: list[tuple[str, float]] = []
        payment_adjustments, _ = _collect_sale_adjustments(
            db,
            [sale.id],
            tenant_id=sale.tenant_id if sale.tenant_id is not None else effective_tenant_id,
        )
        latest_payment_adjustment = payment_adjustments.get(sale.id)
        adjusted_payments = (
            _parse_adjustment_payments(latest_payment_adjustment.payload)
            if latest_payment_adjustment
            else []
        )
        if adjusted_payments:
            source_payments = adjusted_payments
        elif sale.payments and len(sale.payments) > 0:
            source_payments = [
                (payment.method, float(payment.amount or 0.0))
                for payment in sale.payments
            ]
        else:
            fallback_method = sale.main_payment_method or sale.payment_method or "cash"
            fallback_amount = float(sale.paid_amount or sale.total or total_refund)
            source_payments = [(fallback_method, fallback_amount)]

        proportional = _build_proportional_payments(total_refund, source_payments)
        payments_payload = [
            schemas.ReturnPaymentCreate(method=method, amount=amount)
            for method, amount in proportional
        ]

    payments_total = sum(float(p.amount) for p in payments_payload)
    if abs(payments_total - total_refund) > 0.01:
        raise ValueError(
            "La suma de los pagos de reembolso debe coincidir con el total a devolver"
        )

    status = return_in.status or "confirmed"

    sale_return = models.SaleReturn(
        tenant_id=sale.tenant_id or get_default_tenant_id(db),
        sale_id=sale.id,
        status=status,
        notes=return_in.notes,
        created_by=return_in.created_by,
        total_refund=total_refund,
    )
    db.add(sale_return)
    db.flush()

    if not sale_return.document_number:
        sale_return.document_number = f"DV-{sale_return.id:06d}"

    for item_data in items_data:
        sale_item = item_data["sale_item"]
        db_return_item = models.SaleReturnItem(
            tenant_id=sale_return.tenant_id,
            return_id=sale_return.id,
            sale_item_id=sale_item.id,
            product_id=sale_item.product_id,
            product_name=sale_item.product_name,
            product_sku=sale_item.product_sku,
            product_barcode=sale_item.product_barcode,
            reason=item_data["reason"],
            quantity=item_data["quantity"],
            unit_price_original=item_data["unit_price_original"],
            unit_price_net=item_data["unit_price_net"],
            line_discount_value=item_data["line_discount_value"],
            cart_discount_share=item_data["cart_discount_share"],
            total_refund=item_data["total_refund"],
        )
        db.add(db_return_item)

    for idx, payment in enumerate(payments_payload):
        db_payment = models.SaleReturnPayment(
            tenant_id=sale_return.tenant_id,
            return_id=sale_return.id,
            method=payment.method,
            amount=payment.amount,
        )
        db.add(db_payment)

    if status == "confirmed":
        sale.refunded_total = float(sale.refunded_total or 0.0) + total_refund
        sale.refund_count = int(sale.refund_count or 0) + 1

    db.commit()
    db.refresh(sale_return)
    return sale_return


def create_change(
    db: Session,
    change_in: schemas.SaleChangeCreate,
    tenant_id: Optional[int] = None,
) -> models.SaleChange:
    if not change_in.return_items or len(change_in.return_items) == 0:
        raise ValueError("El cambio debe incluir al menos un ítem devuelto")
    if not change_in.new_items or len(change_in.new_items) == 0:
        raise ValueError("El cambio debe incluir al menos un ítem nuevo")

    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    sale: Optional[models.Sale] = None
    if change_in.sale_id is not None:
        sale = get_sale(db, change_in.sale_id, tenant_id=effective_tenant_id)
    elif change_in.sale_document_number:
        sale = get_sale_by_document(
            db,
            change_in.sale_document_number,
            tenant_id=effective_tenant_id,
        )

    if not sale:
        raise ValueError(
            "No encontramos la venta asociada (usa sale_id o sale_document_number)"
        )

    sale_items = {item.id: item for item in sale.items}
    if not sale_items:
        raise ValueError("La venta seleccionada no tiene ítems registrados")

    confirmed_statuses = {"confirmed"}
    refunded_qty = defaultdict(float)
    for previous_return in sale.returns:
        if previous_return.status not in confirmed_statuses:
            continue
        for previous_item in previous_return.items:
            refunded_qty[previous_item.sale_item_id] += float(
                previous_item.quantity or 0.0
            )
    for previous_change in sale.changes:
        if previous_change.status not in confirmed_statuses:
            continue
        for previous_item in previous_change.items_returned:
            refunded_qty[previous_item.sale_item_id] += float(
                previous_item.quantity or 0.0
            )

    subtotal_after_lines = sum(float(item.total or 0.0) for item in sale.items)
    cart_discount_value = float(sale.cart_discount_value or 0.0)
    cart_share_per_unit = {}

    for item in sale.items:
        if float(item.quantity or 0) == 0:
            cart_share_per_unit[item.id] = 0.0
            continue

        if subtotal_after_lines > 0 and cart_discount_value > 0:
            share_total = (float(item.total or 0.0) / subtotal_after_lines) * cart_discount_value
            cart_share_per_unit[item.id] = share_total / float(item.quantity)
        else:
            cart_share_per_unit[item.id] = 0.0

    returned_items_data = []
    total_credit = 0.0

    for item_in in change_in.return_items:
        sale_item = sale_items.get(item_in.sale_item_id)
        if not sale_item:
            raise ValueError(
                f"El ítem {item_in.sale_item_id} no pertenece a la venta especificada"
            )

        requested_qty = float(item_in.quantity or 0.0)
        if requested_qty <= 0:
            raise ValueError("La cantidad devuelta debe ser mayor a cero")

        already_refunded = refunded_qty[sale_item.id]
        available_qty = float(sale_item.quantity or 0.0) - already_refunded
        if requested_qty - available_qty > 0.0001:
            raise ValueError(
                f"La cantidad disponible para el ítem {sale_item.id} es {available_qty},"
                " no se puede devolver más de lo vendido"
            )

        line_quantity = float(sale_item.quantity or 0.0)
        unit_net_after_line = (
            float(sale_item.total or 0.0) / line_quantity if line_quantity else 0.0
        )
        unit_cart_share = cart_share_per_unit.get(sale_item.id, 0.0)
        unit_credit_value = max(0.0, unit_net_after_line - unit_cart_share)
        line_total_credit = unit_credit_value * requested_qty

        line_discount_per_unit = (
            float(sale_item.line_discount_value or 0.0) / line_quantity
            if line_quantity
            else 0.0
        )
        line_discount_value = line_discount_per_unit * requested_qty
        cart_discount_share_value = unit_cart_share * requested_qty

        returned_items_data.append(
            {
                "sale_item": sale_item,
                "quantity": requested_qty,
                "reason": item_in.reason,
                "unit_price_original": float(sale_item.unit_price_original or 0.0),
                "unit_price_net": unit_net_after_line,
                "line_discount_value": line_discount_value,
                "cart_discount_share": cart_discount_share_value,
                "total_credit": line_total_credit,
            }
        )

        total_credit += line_total_credit
        refunded_qty[sale_item.id] += requested_qty

    new_items_data = []
    total_new = 0.0
    for item_in in change_in.new_items:
        requested_qty = float(item_in.quantity or 0.0)
        if requested_qty <= 0:
            raise ValueError("La cantidad del nuevo producto debe ser mayor a cero")
        product = (
            db.query(models.Product)
            .filter(
                models.Product.id == item_in.product_id,
                (
                    models.Product.tenant_id == sale.tenant_id
                    if sale.tenant_id is not None
                    else true()
                ),
            )
            .first()
        )
        if not product:
            raise ValueError(
                f"No encontramos el producto {item_in.product_id} para el cambio"
            )
        unit_price = float(product.price or 0.0)
        line_total = unit_price * requested_qty
        new_items_data.append(
            {
                "product": product,
                "quantity": requested_qty,
                "unit_price": unit_price,
                "total": line_total,
            }
        )
        total_new += line_total

    if total_credit <= 0:
        raise ValueError("El total de crédito debe ser mayor a cero")
    if total_new <= 0:
        raise ValueError("El total del nuevo producto debe ser mayor a cero")

    net_total = total_new - total_credit
    extra_payment = max(0.0, net_total)
    refund_due = max(0.0, -net_total)

    payments_payload = []
    if extra_payment > 0:
        payments_payload = (
            list(change_in.payments)
            if change_in.payments and len(change_in.payments) > 0
            else [schemas.SaleChangePaymentCreate(method="cash", amount=extra_payment)]
        )
        payments_total = sum(float(p.amount) for p in payments_payload)
        if abs(payments_total - extra_payment) > 0.01:
            raise ValueError(
                "La suma de los pagos debe coincidir con el excedente a cobrar"
            )
    elif change_in.payments:
        raise ValueError("No debes registrar pagos cuando no hay excedente")

    status = change_in.status or "confirmed"

    sale_change = models.SaleChange(
        tenant_id=sale.tenant_id or get_default_tenant_id(db),
        sale_id=sale.id,
        status=status,
        notes=change_in.notes,
        created_by=change_in.created_by,
        total_credit=total_credit,
        total_new=total_new,
        net_total=net_total,
        extra_payment=extra_payment,
        refund_due=refund_due,
        pos_name=sale.pos_name,
        seller_name=change_in.created_by or sale.vendor_name,
        station_id=sale.station_id,
    )
    db.add(sale_change)
    db.flush()

    if not sale_change.document_number:
        sale_change.document_number = f"CB-{sale_change.id:06d}"

    for item_data in returned_items_data:
        sale_item = item_data["sale_item"]
        db_return_item = models.SaleChangeReturnItem(
            tenant_id=sale_change.tenant_id,
            change_id=sale_change.id,
            sale_item_id=sale_item.id,
            product_id=sale_item.product_id,
            product_name=sale_item.product_name,
            product_sku=sale_item.product_sku,
            product_barcode=sale_item.product_barcode,
            reason=item_data["reason"],
            quantity=item_data["quantity"],
            unit_price_original=item_data["unit_price_original"],
            unit_price_net=item_data["unit_price_net"],
            line_discount_value=item_data["line_discount_value"],
            cart_discount_share=item_data["cart_discount_share"],
            total_credit=item_data["total_credit"],
        )
        db.add(db_return_item)

    for item_data in new_items_data:
        product = item_data["product"]
        db_new_item = models.SaleChangeNewItem(
            tenant_id=sale_change.tenant_id,
            change_id=sale_change.id,
            product_id=product.id,
            product_name=product.name,
            product_sku=product.sku,
            product_barcode=product.barcode,
            quantity=item_data["quantity"],
            unit_price=item_data["unit_price"],
            total=item_data["total"],
        )
        db.add(db_new_item)

    for payment in payments_payload:
        db_payment = models.SaleChangePayment(
            tenant_id=sale_change.tenant_id,
            change_id=sale_change.id,
            method=payment.method,
            amount=payment.amount,
        )
        db.add(db_payment)

    db.commit()
    db.refresh(sale_change)
    return sale_change


# ===================== VOID / ADJUSTMENTS =====================


def void_sale(
    db: Session,
    sale: models.Sale,
    user: models.PosUser,
    reason: Optional[str] = None,
) -> models.Sale:
    if sale.status == "voided":
        raise ValueError("La venta ya está anulada")
    if sale.closure_id is not None:
        raise ValueError(
            "No se puede anular una venta cerrada; registra una devolución"
        )

    sale.status = "voided"
    sale.voided_at = datetime.utcnow()
    sale.voided_by_user_id = user.id
    sale.void_reason = reason

    sale_items = list(sale.items or [])
    product_ids = [int(item.product_id) for item in sale_items if item.product_id is not None]
    service_flags: dict[int, bool] = {}
    if product_ids:
        product_rows = (
            db.query(models.Product.id, models.Product.service)
            .filter(models.Product.id.in_(product_ids))
            .all()
        )
        service_flags = {int(row.id): bool(row.service) for row in product_rows}

    note_parts = ["Reposición automática por anulación de venta"]
    if sale.document_number:
        note_parts.append(f"({sale.document_number})")
    if reason and reason.strip():
        note_parts.append(f"- {reason.strip()}")
    movement_note = " ".join(note_parts)

    for item in sale_items:
        if item.product_id is None:
            continue
        product_id = int(item.product_id)
        if service_flags.get(product_id, False):
            continue
        qty = float(item.quantity or 0.0)
        if qty <= 0:
            continue
        db.add(
            models.InventoryMovement(
                tenant_id=sale.tenant_id,
                product_id=product_id,
                qty_delta=abs(qty),
                reason="adjustment",
                notes=movement_note,
                reference_type="sale",
                reference_id=sale.id,
                created_by_user_id=user.id,
            )
        )

    db.commit()
    db.refresh(sale)
    return sale


def void_return(
    db: Session,
    sale_return: models.SaleReturn,
    user: models.PosUser,
    reason: Optional[str] = None,
) -> models.SaleReturn:
    if sale_return.status != "confirmed":
        raise ValueError("Solo se pueden anular devoluciones confirmadas")
    if sale_return.closure_id is not None:
        raise ValueError(
            "No se puede anular una devolución cerrada; registra un ajuste nuevo"
        )

    sale = sale_return.sale
    if sale:
        sale.refunded_total = max(
            0.0, float(sale.refunded_total or 0.0) - float(sale_return.total_refund or 0.0)
        )
        sale.refund_count = max(0, int(sale.refund_count or 0) - 1)

    sale_return.status = "voided"
    sale_return.voided_at = datetime.utcnow()
    sale_return.voided_by_user_id = user.id
    sale_return.void_reason = reason
    sale_return.adjustment_reference = sale.document_number if sale else None

    db.commit()
    db.refresh(sale_return)
    return sale_return


def void_change(
    db: Session,
    sale_change: models.SaleChange,
    user: models.PosUser,
    reason: Optional[str] = None,
) -> models.SaleChange:
    if sale_change.status != "confirmed":
        raise ValueError("Solo se pueden anular cambios confirmados")
    if sale_change.closure_id is not None:
        raise ValueError(
            "No se puede anular un cambio cerrado; registra un ajuste nuevo"
        )

    sale_change.status = "voided"
    sale_change.voided_at = datetime.utcnow()
    sale_change.voided_by_user_id = user.id
    sale_change.void_reason = reason
    sale_change.adjustment_reference = (
        sale_change.sale.document_number if sale_change.sale else None
    )

    db.commit()
    db.refresh(sale_change)
    return sale_change


def create_document_adjustment(
    db: Session,
    doc_type: str,
    doc_id: int,
    adjustment_type: str,
    reason: Optional[str],
    payload: dict,
    total_delta: float,
    payment_delta: float,
    is_post_closure: bool,
    original_closure_id: Optional[int],
    user: models.PosUser,
    tenant_id: Optional[int] = None,
) -> models.DocumentAdjustment:
    effective_tenant_id = (
        tenant_id
        if tenant_id is not None
        else (user.tenant_id if user and user.tenant_id is not None else get_default_tenant_id(db))
    )
    adjustment = models.DocumentAdjustment(
        tenant_id=effective_tenant_id,
        doc_type=doc_type,
        doc_id=doc_id,
        adjustment_type=adjustment_type,
        reason=reason,
        payload=payload,
        total_delta=total_delta,
        payment_delta=payment_delta,
        is_post_closure=is_post_closure,
        original_closure_id=original_closure_id,
        created_by_user_id=user.id,
        created_by_user_name=user.name,
    )
    db.add(adjustment)
    db.commit()
    db.refresh(adjustment)
    return adjustment


def list_document_adjustments(
    db: Session,
    doc_type: str,
    doc_id: int,
    tenant_id: Optional[int] = None,
) -> List[models.DocumentAdjustment]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.DocumentAdjustment)
        .filter(
            models.DocumentAdjustment.doc_type == doc_type,
            models.DocumentAdjustment.doc_id == doc_id,
            (
                models.DocumentAdjustment.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .order_by(models.DocumentAdjustment.created_at.desc())
        .all()
    )


def list_document_adjustments_for_docs(
    db: Session,
    doc_type: str,
    doc_ids: List[int],
    tenant_id: Optional[int] = None,
) -> List[models.DocumentAdjustment]:
    if not doc_ids:
        return []
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.DocumentAdjustment)
        .filter(
            models.DocumentAdjustment.doc_type == doc_type,
            models.DocumentAdjustment.doc_id.in_(doc_ids),
            (
                models.DocumentAdjustment.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .order_by(models.DocumentAdjustment.created_at.desc())
        .all()
    )


def _parse_adjustment_payments(payload: object) -> list[tuple[str, float]]:
    if not isinstance(payload, dict):
        return []
    payments = payload.get("payments")
    if not isinstance(payments, list):
        return []
    results: list[tuple[str, float]] = []
    for entry in payments:
        if not isinstance(entry, dict):
            continue
        method = entry.get("method")
        amount = entry.get("amount")
        if not isinstance(method, str) or not method:
            continue
        try:
            numeric = float(amount or 0.0)
        except (TypeError, ValueError):
            continue
        results.append((method, numeric))
    return results


def _collect_sale_adjustments(
    db: Session,
    sale_ids: list[int],
    range_end: datetime | None = None,
    tenant_id: Optional[int] = None,
) -> tuple[dict[int, models.DocumentAdjustment], dict[int, float]]:
    if not sale_ids:
        return {}, {}
    query = (
        db.query(models.DocumentAdjustment)
        .filter(models.DocumentAdjustment.doc_type == "sale")
        .filter(models.DocumentAdjustment.doc_id.in_(sale_ids))
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.DocumentAdjustment.tenant_id == effective_tenant_id)
    if range_end is not None:
        query = query.filter(models.DocumentAdjustment.created_at <= range_end)
    adjustments = query.order_by(models.DocumentAdjustment.created_at.desc()).all()

    latest_payment_adjustment: dict[int, models.DocumentAdjustment] = {}
    total_delta: dict[int, float] = defaultdict(float)
    for adjustment in adjustments:
        doc_id = adjustment.doc_id
        total_delta[doc_id] += float(adjustment.total_delta or 0.0)
        if doc_id in latest_payment_adjustment:
            continue
        payload_payments = _parse_adjustment_payments(adjustment.payload)
        if payload_payments:
            latest_payment_adjustment[doc_id] = adjustment
    return latest_payment_adjustment, total_delta


def get_separated_order_payment(
    db: Session,
    payment_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.SeparatedOrderPayment]:
    query = db.query(models.SeparatedOrderPayment).filter(models.SeparatedOrderPayment.id == payment_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.SeparatedOrderPayment.tenant_id == effective_tenant_id)
    return query.first()


def void_separated_order_payment(
    db: Session,
    payment: models.SeparatedOrderPayment,
    user: models.PosUser,
    reason: Optional[str] = None,
    note: Optional[str] = None,
) -> models.SeparatedOrder:
    if payment.status == "voided":
        raise ValueError("El abono ya está anulado")
    order = payment.separated_order
    if not order:
        raise ValueError("No se encontró el separado asociado")

    if payment.closure_id is None:
        payment.status = "voided"
        payment.voided_at = datetime.utcnow()
        payment.voided_by_user_id = user.id
        payment.void_reason = reason
        payment.adjustment_reference = None
    else:
        adjustment_note = note or f"Ajuste por anulación del abono #{payment.id}"
        station_id = payment.station_id or order.sale.station_id
        adjustment = models.SeparatedOrderPayment(
            separated_order_id=order.id,
            method=payment.method,
            amount=-float(payment.amount or 0.0),
            reference=payment.reference,
            note=adjustment_note,
            station_id=station_id,
            status="adjustment",
        )
        db.add(adjustment)
        db.flush()
        payment.adjustment_reference = f"SEP-ADJ-{adjustment.id}"

    order.balance = float(order.balance or 0.0) + float(payment.amount or 0.0)
    if order.balance > 0.01:
        order.status = "reservado"

    db.commit()
    db.refresh(order)
    return order
# ===================== POS SETTINGS =====================


def get_pos_settings(
    db: Session,
    tenant_id: Optional[int] = None,
) -> models.PosSettings:
    if tenant_id is None:
        tenant_id = get_default_tenant_id(db)
    query = db.query(models.PosSettings)
    if tenant_id is not None:
        query = query.filter(models.PosSettings.tenant_id == tenant_id)
    settings = query.order_by(models.PosSettings.id.asc()).first()
    if not settings:
        settings = models.PosSettings(tenant_id=tenant_id)
        db.add(settings)
        db.commit()
        db.refresh(settings)

    settings.closure_email_recipients = settings.closure_email_recipients or []
    settings.ticket_email_cc = settings.ticket_email_cc or []
    settings.smtp_use_tls = bool(settings.smtp_use_tls) if settings.smtp_use_tls is not None else False
    settings.smtp_host = settings.smtp_host or ""
    settings.smtp_port = settings.smtp_port or 0
    settings.smtp_user = settings.smtp_user or ""
    settings.smtp_password = settings.smtp_password or ""
    settings.email_from = settings.email_from or None
    if settings.web_pos_send_closure_email is None:
        settings.web_pos_send_closure_email = True
    settings.station_closure_email_overrides = (
        settings.station_closure_email_overrides or {}
    )
    settings.web_personalization_bindings = (
        settings.web_personalization_bindings or {}
    )
    normalized_permissions = permissions.ensure_permissions(settings.role_permissions)
    if settings.role_permissions != normalized_permissions:
        settings.role_permissions = normalized_permissions
        db.commit()
        db.refresh(settings)
    return settings


def update_pos_settings(
    db: Session,
    settings: models.PosSettings,
    settings_in: schemas.PosSettingsUpdate,
) -> models.PosSettings:
    data = settings_in.model_dump(exclude_unset=True)
    notifications = data.pop("notifications", None)
    for field, value in data.items():
        setattr(settings, field, value)
    if notifications is not None:
        settings.notifications = notifications
    db.commit()
    db.refresh(settings)
    return settings


def get_role_permissions(
    db: Session,
    tenant_id: Optional[int] = None,
):
    settings = get_pos_settings(db, tenant_id=tenant_id)
    return permissions.ensure_permissions(settings.role_permissions)


def update_role_permissions(
    db: Session,
    modules: List[Dict[str, Any]],
    tenant_id: Optional[int] = None,
):
    settings = get_pos_settings(db, tenant_id=tenant_id)
    cleaned = permissions.ensure_permissions(modules)
    settings.role_permissions = cleaned
    db.commit()
    db.refresh(settings)
    return cleaned


# ===================== POS USERS =====================


def list_pos_users(
    db: Session,
    status: Optional[str] = None,
    role: Optional[str] = None,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.PosUser)
    if effective_tenant_id is not None:
        query = query.filter(models.PosUser.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.PosUser.status == status)
    if role:
        query = query.filter(models.PosUser.role == role)
    return query.order_by(models.PosUser.created_at.desc()).all()


def get_pos_user(
    db: Session,
    user_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosUser]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.PosUser).filter(models.PosUser.id == user_id)
    if effective_tenant_id is not None:
        query = query.filter(models.PosUser.tenant_id == effective_tenant_id)
    return query.first()


def get_pos_user_by_email(
    db: Session,
    email: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosUser]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.PosUser).filter(func.lower(models.PosUser.email) == email.lower())
    if effective_tenant_id is not None:
        query = query.filter(models.PosUser.tenant_id == effective_tenant_id)
    return query.first()


def get_pos_user_by_email_global(
    db: Session,
    email: str,
) -> Optional[models.PosUser]:
    return (
        db.query(models.PosUser)
        .filter(func.lower(models.PosUser.email) == email.lower())
        .order_by(models.PosUser.id.asc())
        .first()
    )


def get_pos_user_by_pin(
    db: Session,
    pin: str,
    exclude_user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosUser]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.PosUser).filter(models.PosUser.pin_hash.isnot(None))
    if effective_tenant_id is not None:
        query = query.filter(models.PosUser.tenant_id == effective_tenant_id)
    users = query.all()
    matches: list[models.PosUser] = []
    for user in users:
        if exclude_user_id and user.id == exclude_user_id:
            continue
        if user.pin_hash and verify_password(pin, user.pin_hash):
            matches.append(user)
            if len(matches) > 1:
                raise ValueError("PIN duplicado entre usuarios")
    return matches[0] if matches else None


def _count_active_admins(db: Session, tenant_id: Optional[int] = None) -> int:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = (
        db.query(models.PosUser)
        .filter(models.PosUser.role == "Administrador")
        .filter(models.PosUser.status == "Activo")
    )
    if effective_tenant_id is not None:
        query = query.filter(models.PosUser.tenant_id == effective_tenant_id)
    return query.count()


def create_pos_user(
    db: Session,
    user_in: schemas.PosUserCreate,
    tenant_id: Optional[int] = None,
) -> models.PosUser:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    existing = get_pos_user_by_email(db, user_in.email, tenant_id=effective_tenant_id)
    if existing:
        raise ValueError("Ya existe un usuario con ese email")

    raw_password = user_in.password or secrets.token_urlsafe(16)
    pin_plain = user_in.pin_plain
    if pin_plain:
        existing_pin = get_pos_user_by_pin(db, pin_plain, tenant_id=effective_tenant_id)
        if existing_pin:
            raise ValueError("Ya existe un usuario con ese PIN")
    linked_employee_id = user_in.employee_id
    if linked_employee_id:
        employee = get_hr_employee(db, user_in.employee_id, tenant_id=effective_tenant_id)
        if not employee:
            raise ValueError("Empleado HR no encontrado")
        linked = (
            db.query(models.PosUser)
            .filter(models.PosUser.employee_id == user_in.employee_id)
            .filter(
                models.PosUser.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            )
            .first()
        )
        if linked:
            raise ValueError("Ese empleado HR ya tiene un usuario vinculado")
    elif user_in.create_hr_profile:
        employee = models.HREmployee(
            tenant_id=effective_tenant_id,
            name=user_in.name,
            email=user_in.email,
            status="Activo",
            phone=user_in.phone,
            position=user_in.position,
            notes=user_in.notes,
            avatar_url=user_in.avatar_url,
            birth_date=user_in.birth_date,
            location=user_in.location,
            bio=user_in.bio,
        )
        db.add(employee)
        db.flush()
        linked_employee_id = employee.id

    user = models.PosUser(
        tenant_id=effective_tenant_id,
        name=user_in.name,
        email=user_in.email,
        role=user_in.role,
        status="Activo",
        is_active=True,
        password_hash=hash_password(raw_password),
        pin_hash=hash_password(pin_plain) if pin_plain else None,
        phone=user_in.phone,
        position=user_in.position,
        notes=user_in.notes,
        employee_id=linked_employee_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if user.employee_id:
        employee = get_hr_employee(db, user.employee_id, tenant_id=effective_tenant_id)
        if employee:
            employee.name = user.name
            employee.email = user.email
            employee.phone = user.phone
            employee.position = user.position
            employee.notes = user.notes
            employee.avatar_url = user.avatar_url
            employee.birth_date = user.birth_date
            employee.location = user.location
            employee.bio = user.bio
            db.commit()
    # Aquí podríamos disparar invitación / pin
    return user


def update_pos_user(
    db: Session,
    user: models.PosUser,
    user_in: schemas.PosUserUpdate,
) -> models.PosUser:
    data = user_in.dict(exclude_unset=True)

    if "email" in data:
        new_email = data["email"]
        existing = get_pos_user_by_email(db, new_email, tenant_id=user.tenant_id)
        if existing and existing.id != user.id:
            raise ValueError("Ya existe un usuario con ese email")
    if "employee_id" in data and data["employee_id"] is not None:
        employee = get_hr_employee(db, data["employee_id"], tenant_id=user.tenant_id)
        if not employee:
            raise ValueError("Empleado HR no encontrado")
        linked = (
            db.query(models.PosUser)
            .filter(models.PosUser.employee_id == data["employee_id"])
            .filter(models.PosUser.id != user.id)
            .filter(
                models.PosUser.tenant_id == user.tenant_id
                if user.tenant_id is not None
                else true()
            )
            .first()
        )
        if linked:
            raise ValueError("Ese empleado HR ya tiene un usuario vinculado")

    new_role = data.get("role", user.role)
    new_status = data.get("status", user.status)

    was_active_admin = user.role == "Administrador" and user.status == "Activo"
    will_be_active_admin = new_role == "Administrador" and new_status == "Activo"

    if was_active_admin and not will_be_active_admin:
        if _count_active_admins(db, tenant_id=user.tenant_id) <= 2:
            raise ValueError("No se puede desactivar o cambiar al último Administrador activo")

    for field, value in data.items():
        if field == "password":
            user.password_hash = hash_password(value)
            continue
        if field == "pin_plain":
            if value:
                existing_pin = get_pos_user_by_pin(
                    db,
                    value,
                    exclude_user_id=user.id,
                    tenant_id=user.tenant_id,
                )
                if existing_pin:
                    raise ValueError("Ya existe un usuario con ese PIN")
                user.pin_hash = hash_password(value)
            else:
                user.pin_hash = None
            continue
        if field == "status":
            user.is_active = value == "Activo"
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


def list_user_documents(
    db: Session,
    user_id: int,
    tenant_id: Optional[int] = None,
) -> list[models.PosUserDocument]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.PosUserDocument)
        .filter(
            models.PosUserDocument.user_id == user_id,
            (
                models.PosUserDocument.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .order_by(models.PosUserDocument.created_at.desc())
        .all()
    )


def create_user_document(
    db: Session,
    user_id: int,
    file_name: str,
    file_url: str,
    file_size: int,
    note: str | None,
    tenant_id: Optional[int] = None,
) -> models.PosUserDocument:
    user = db.query(models.PosUser).filter(models.PosUser.id == user_id).first()
    effective_tenant_id = (
        tenant_id
        if tenant_id is not None
        else (user.tenant_id if user and user.tenant_id is not None else get_default_tenant_id(db))
    )
    doc = models.PosUserDocument(
        tenant_id=effective_tenant_id,
        user_id=user_id,
        file_name=file_name,
        file_url=file_url,
        file_size=file_size,
        note=note,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def delete_user_document(
    db: Session,
    user_id: int,
    doc_id: int,
    tenant_id: Optional[int] = None,
) -> bool:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    doc = (
        db.query(models.PosUserDocument)
        .filter(models.PosUserDocument.user_id == user_id)
        .filter(models.PosUserDocument.id == doc_id)
        .filter(
            models.PosUserDocument.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .first()
    )
    if not doc:
        return False
    db.delete(doc)
    db.commit()
    return True


def list_hr_employees(
    db: Session,
    status: str | None = None,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.HREmployee)
    if effective_tenant_id is not None:
        query = query.filter(models.HREmployee.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.HREmployee.status == status)
    return (
        query.options(joinedload(models.HREmployee.system_user))
        .order_by(
            models.HREmployee.order_index.asc(),
            models.HREmployee.created_at.desc(),
            models.HREmployee.id.desc(),
        )
        .all()
    )


def get_hr_employee(
    db: Session,
    employee_id: int,
    tenant_id: Optional[int] = None,
) -> models.HREmployee | None:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.HREmployee)
        .options(joinedload(models.HREmployee.system_user))
        .filter(
            models.HREmployee.id == employee_id,
            (
                models.HREmployee.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )


def create_hr_employee(
    db: Session,
    payload: schemas.HREmployeeCreate,
    tenant_id: Optional[int] = None,
) -> models.HREmployee:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    payload_data = payload.model_dump()
    if payload_data.get("status") == "Activo":
        payload_data["active_from"] = payload_data.get("active_from") or date.today()
        payload_data["active_until"] = None
    elif payload_data.get("active_until") is None:
        payload_data["active_until"] = date.today()
    max_order_index = (
        db.query(func.max(models.HREmployee.order_index))
        .filter(
            models.HREmployee.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .scalar()
    )
    employee = models.HREmployee(
        tenant_id=effective_tenant_id,
        order_index=int(max_order_index or 0) + 10,
        **payload_data,
    )
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee


def update_hr_employee(
    db: Session,
    employee: models.HREmployee,
    payload: schemas.HREmployeeUpdate,
) -> models.HREmployee:
    data = payload.model_dump(exclude_unset=True)
    previous_status = employee.status
    next_status = data.get("status", previous_status)

    if next_status == "Inactivo" and previous_status != "Inactivo":
        data["active_until"] = data.get("active_until") or date.today()
    if next_status == "Activo":
        data["active_until"] = None
        if previous_status != "Activo":
            data["active_from"] = data.get("active_from") or date.today()

    for field, value in data.items():
        setattr(employee, field, value)
    db.commit()
    db.refresh(employee)
    return employee


def reorder_hr_employees(
    db: Session,
    reorder_items: List[schemas.HREmployeeReorderItem],
    tenant_id: Optional[int] = None,
) -> List[models.HREmployee]:
    ids = [item.id for item in reorder_items]
    query = db.query(models.HREmployee).filter(models.HREmployee.id.in_(ids))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.HREmployee.tenant_id == effective_tenant_id)
    employees = query.all()
    employees_map = {employee.id: employee for employee in employees}
    if len(employees_map) != len(ids):
        raise ValueError("Algún empleado no existe")
    for item in reorder_items:
        employees_map[item.id].order_index = int(item.order_index)
    db.commit()
    return list_hr_employees(db, tenant_id=effective_tenant_id)


def list_hr_employee_documents(
    db: Session,
    employee_id: int,
    tenant_id: Optional[int] = None,
) -> list[models.HREmployeeDocument]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.HREmployeeDocument)
        .filter(
            models.HREmployeeDocument.employee_id == employee_id,
            (
                models.HREmployeeDocument.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .order_by(models.HREmployeeDocument.created_at.desc())
        .all()
    )


def create_hr_employee_document(
    db: Session,
    employee_id: int,
    file_name: str,
    file_url: str,
    file_size: int,
    note: str | None,
    tenant_id: Optional[int] = None,
) -> models.HREmployeeDocument:
    employee = db.query(models.HREmployee).filter(models.HREmployee.id == employee_id).first()
    effective_tenant_id = (
        tenant_id
        if tenant_id is not None
        else (employee.tenant_id if employee and employee.tenant_id is not None else get_default_tenant_id(db))
    )
    doc = models.HREmployeeDocument(
        tenant_id=effective_tenant_id,
        employee_id=employee_id,
        file_name=file_name,
        file_url=file_url,
        file_size=file_size,
        note=note,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def delete_hr_employee_document(
    db: Session,
    employee_id: int,
    doc_id: int,
    tenant_id: Optional[int] = None,
) -> bool:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    doc = (
        db.query(models.HREmployeeDocument)
        .filter(models.HREmployeeDocument.id == doc_id)
        .filter(models.HREmployeeDocument.employee_id == employee_id)
        .filter(
            models.HREmployeeDocument.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .first()
    )
    if not doc:
        return False
    db.delete(doc)
    db.commit()
    return True


# ===================== HR SCHEDULE =====================


def _start_of_week(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _parse_minutes(value: str | None) -> int:
    if not value:
        return 0
    try:
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return 0


def _calculate_schedule_shift_hours(shift: models.ScheduleShift) -> float:
    if shift.is_time_off:
        return 0.0
    start_minutes = _parse_minutes(shift.start_time)
    end_minutes = _parse_minutes(shift.end_time)
    if start_minutes <= 0 and end_minutes <= 0:
        return 0.0
    total = max(0, end_minutes - start_minutes - int(shift.break_minutes or 0))
    return round(total / 60.0, 2)


def schedule_shift_total_hours(shift: models.ScheduleShift) -> float:
    return _calculate_schedule_shift_hours(shift)


def _validate_schedule_shift_fields(
    start_time: str | None,
    end_time: str | None,
    break_minutes: int,
    is_time_off: bool,
) -> None:
    if break_minutes < 0:
        raise ValueError("El descanso no puede ser negativo")
    if is_time_off:
        return
    if not start_time or not end_time:
        raise ValueError("Debes indicar hora de inicio y fin")
    start_minutes = _parse_minutes(start_time)
    end_minutes = _parse_minutes(end_time)
    if end_minutes <= start_minutes:
        raise ValueError("La hora fin debe ser mayor a la hora inicio")
    if break_minutes >= (end_minutes - start_minutes):
        raise ValueError("El descanso no puede ser mayor o igual a la duración del turno")


def get_schedule_week_by_start(
    db: Session,
    week_start: date,
    tenant_id: Optional[int] = None,
) -> models.ScheduleWeek | None:
    normalized_start = _start_of_week(week_start)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.ScheduleWeek)
        .filter(
            models.ScheduleWeek.week_start == normalized_start,
            (
                models.ScheduleWeek.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )


def get_schedule_week(
    db: Session,
    week_id: int,
    tenant_id: Optional[int] = None,
) -> models.ScheduleWeek | None:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.ScheduleWeek)
        .filter(
            models.ScheduleWeek.id == week_id,
            (
                models.ScheduleWeek.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )


def get_or_create_schedule_week(
    db: Session,
    week_start: date,
    notes: str | None = None,
) -> models.ScheduleWeek:
    normalized_start = _start_of_week(week_start)
    effective_tenant_id = get_default_tenant_id(db)
    week = get_schedule_week_by_start(db, normalized_start, tenant_id=effective_tenant_id)
    if week:
        if notes is not None:
            week.notes = notes
            db.commit()
            db.refresh(week)
        return week

    week = models.ScheduleWeek(
        tenant_id=effective_tenant_id,
        week_start=normalized_start,
        status="draft",
        notes=notes,
    )
    db.add(week)
    db.commit()
    db.refresh(week)
    return week


def publish_schedule_week(
    db: Session,
    week: models.ScheduleWeek,
    published_by_user_id: int | None = None,
    notes: str | None = None,
) -> models.ScheduleWeek:
    week.status = "published"
    week.published_at = datetime.utcnow()
    week.published_by_user_id = published_by_user_id
    if notes is not None:
        week.notes = notes
    db.commit()
    db.refresh(week)
    return week


def list_schedule_templates(
    db: Session,
    include_inactive: bool = True,
    tenant_id: Optional[int] = None,
) -> List[models.ScheduleTemplate]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.ScheduleTemplate)
    if effective_tenant_id is not None:
        query = query.filter(models.ScheduleTemplate.tenant_id == effective_tenant_id)
    if not include_inactive:
        query = query.filter(models.ScheduleTemplate.is_active.is_(True))
    return (
        query.order_by(
            models.ScheduleTemplate.order_index.asc(),
            models.ScheduleTemplate.id.asc(),
        )
        .all()
    )


def get_schedule_template(
    db: Session,
    template_id: int,
    tenant_id: Optional[int] = None,
) -> models.ScheduleTemplate | None:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.ScheduleTemplate)
        .filter(
            models.ScheduleTemplate.id == template_id,
            (
                models.ScheduleTemplate.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )


def create_schedule_template(
    db: Session,
    payload: schemas.ScheduleTemplateCreate,
) -> models.ScheduleTemplate:
    effective_tenant_id = get_default_tenant_id(db)
    template = models.ScheduleTemplate(
        tenant_id=effective_tenant_id,
        **payload.model_dump(),
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def update_schedule_template(
    db: Session,
    template: models.ScheduleTemplate,
    payload: schemas.ScheduleTemplateUpdate,
) -> models.ScheduleTemplate:
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


def list_schedule_shifts_for_week(
    db: Session,
    week_id: int,
    tenant_id: Optional[int] = None,
) -> List[models.ScheduleShift]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.ScheduleShift)
        .options(selectinload(models.ScheduleShift.employee))
        .filter(
            models.ScheduleShift.week_id == week_id,
            (
                models.ScheduleShift.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .order_by(
            models.ScheduleShift.shift_date.asc(),
            models.ScheduleShift.employee_id.asc(),
        )
        .all()
    )


def upsert_schedule_shift(
    db: Session,
    payload: schemas.ScheduleShiftUpsertRequest,
) -> models.ScheduleShift:
    effective_tenant_id = get_default_tenant_id(db)
    week: models.ScheduleWeek | None = None
    if payload.week_id is not None:
        week = get_schedule_week(db, payload.week_id, tenant_id=effective_tenant_id)
        if not week:
            raise ValueError("Semana no encontrada")
    else:
        if payload.week_start is None:
            raise ValueError("Debes indicar week_id o week_start")
        week = get_or_create_schedule_week(db, payload.week_start)

    employee = get_hr_employee(db, payload.employee_id, tenant_id=effective_tenant_id)
    if not employee:
        raise ValueError("Empleado no encontrado")

    normalized_shift_date = payload.shift_date
    if _start_of_week(normalized_shift_date) != week.week_start:
        raise ValueError("La fecha del turno debe pertenecer a la semana seleccionada")

    _validate_schedule_shift_fields(
        payload.start_time,
        payload.end_time,
        payload.break_minutes,
        payload.is_time_off,
    )

    shift = (
        db.query(models.ScheduleShift)
        .filter(
            models.ScheduleShift.week_id == week.id,
            (
                models.ScheduleShift.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .filter(models.ScheduleShift.employee_id == payload.employee_id)
        .filter(models.ScheduleShift.shift_date == normalized_shift_date)
        .first()
    )
    data = payload.model_dump(
        exclude={"week_id", "week_start"},
        exclude_unset=True,
    )
    if shift:
        for field, value in data.items():
            setattr(shift, field, value)
    else:
        shift = models.ScheduleShift(
            tenant_id=week.tenant_id or employee.tenant_id or get_default_tenant_id(db),
            week_id=week.id,
            **data,
        )
        db.add(shift)
    db.commit()
    db.refresh(shift)
    return shift


def get_schedule_shift(
    db: Session,
    shift_id: int,
    tenant_id: Optional[int] = None,
) -> models.ScheduleShift | None:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.ScheduleShift)
        .options(selectinload(models.ScheduleShift.employee))
        .filter(
            models.ScheduleShift.id == shift_id,
            (
                models.ScheduleShift.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )


def update_schedule_shift(
    db: Session,
    shift: models.ScheduleShift,
    payload: schemas.ScheduleShiftUpdate,
) -> models.ScheduleShift:
    data = payload.model_dump(exclude_unset=True)
    start_time = data.get("start_time", shift.start_time)
    end_time = data.get("end_time", shift.end_time)
    break_minutes = data.get("break_minutes", shift.break_minutes or 0)
    is_time_off = data.get("is_time_off", shift.is_time_off)
    _validate_schedule_shift_fields(start_time, end_time, break_minutes, is_time_off)

    for field, value in data.items():
        setattr(shift, field, value)
    db.commit()
    db.refresh(shift)
    return shift


def delete_schedule_shift(
    db: Session,
    shift: models.ScheduleShift,
) -> None:
    db.delete(shift)
    db.commit()


def _easter_sunday(year: int) -> date:
    # Gregorian algorithm (Meeus/Jones/Butcher)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _move_to_monday(value: date) -> date:
    if value.weekday() == 0:
        return value
    return value + timedelta(days=(7 - value.weekday()))


def _colombia_holidays(year: int) -> Dict[date, str]:
    easter = _easter_sunday(year)
    holidays: Dict[date, str] = {}

    def add(day: date, label: str, *, emiliani: bool = False) -> None:
        normalized = _move_to_monday(day) if emiliani else day
        holidays[normalized] = label

    # Fixed holidays
    add(date(year, 1, 1), "Año Nuevo")
    add(date(year, 5, 1), "Día del Trabajo")
    add(date(year, 7, 20), "Día de la Independencia")
    add(date(year, 8, 7), "Batalla de Boyacá")
    add(date(year, 12, 8), "Inmaculada Concepción")
    add(date(year, 12, 25), "Navidad")

    # Emiliani holidays (move to monday)
    add(date(year, 1, 6), "Reyes Magos", emiliani=True)
    add(date(year, 3, 19), "San José", emiliani=True)
    add(date(year, 6, 29), "San Pedro y San Pablo", emiliani=True)
    add(date(year, 8, 15), "Asunción de la Virgen", emiliani=True)
    add(date(year, 10, 12), "Día de la Raza", emiliani=True)
    add(date(year, 11, 1), "Todos los Santos", emiliani=True)
    add(date(year, 11, 11), "Independencia de Cartagena", emiliani=True)

    # Relative to Easter
    add(easter - timedelta(days=3), "Jueves Santo")
    add(easter - timedelta(days=2), "Viernes Santo")
    add(easter + timedelta(days=43), "Ascensión del Señor", emiliani=True)
    add(easter + timedelta(days=64), "Corpus Christi", emiliani=True)
    add(easter + timedelta(days=71), "Sagrado Corazón", emiliani=True)
    return holidays


def get_schedule_week_view(
    db: Session,
    week_start: date,
) -> schemas.ScheduleWeekView:
    week = get_or_create_schedule_week(db, week_start)
    shifts = list_schedule_shifts_for_week(db, week.id)
    employees = list_hr_employees(db)

    week_days = [week.week_start + timedelta(days=offset) for offset in range(7)]
    day_totals_map: Dict[date, float] = {day: 0.0 for day in week_days}
    shift_payload: List[schemas.ScheduleShiftRead] = []
    for shift in shifts:
        total_hours = _calculate_schedule_shift_hours(shift)
        day_totals_map[shift.shift_date] = round(
            day_totals_map.get(shift.shift_date, 0.0) + total_hours,
            2,
        )
        shift_payload.append(
            schemas.ScheduleShiftRead(
                id=shift.id,
                week_id=shift.week_id,
                employee_id=shift.employee_id,
                shift_date=shift.shift_date,
                start_time=shift.start_time,
                end_time=shift.end_time,
                break_minutes=shift.break_minutes or 0,
                position=shift.position,
                color=shift.color,
                note=shift.note,
                is_time_off=bool(shift.is_time_off),
                source_template_id=shift.source_template_id,
                created_at=shift.created_at,
                updated_at=shift.updated_at,
                total_hours=total_hours,
            )
        )

    week_total_hours = round(sum(day_totals_map.values()), 2)
    day_totals = [
        schemas.ScheduleDayTotal(shift_date=day, total_hours=day_totals_map.get(day, 0.0))
        for day in week_days
    ]
    week_end = week.week_start + timedelta(days=6)
    employees_with_shifts = {shift.employee_id for shift in shifts}

    def _employee_in_week(employee: models.HREmployee) -> bool:
        if not bool(getattr(employee, "show_in_schedule", True)):
            return False
        if employee.id in employees_with_shifts:
            return True
        start = employee.active_from or (employee.created_at.date() if employee.created_at else None)
        end = employee.active_until
        if end is None and employee.status == "Inactivo":
            # Backward-compatible fallback for historical rows that became inactive
            # before active_until existed in schema.
            end = (
                employee.updated_at.date()
                if employee.updated_at
                else (employee.created_at.date() if employee.created_at else None)
            )
        if start and start > week_end:
            return False
        if end and end < week.week_start:
            return False
        return True

    employee_rows = [
        schemas.ScheduleEmployeeRow(
            id=employee.id,
            name=employee.name,
            status="Activo" if employee.status == "Activo" else "Inactivo",
            position=employee.position,
            avatar_url=employee.avatar_url,
            row_color=employee.row_color,
            birth_date=employee.birth_date,
        )
        for employee in employees
        if _employee_in_week(employee)
    ]

    day_events: List[schemas.ScheduleDayEvent] = []
    holiday_map = _colombia_holidays(week.week_start.year)
    for day in week_days:
        holiday_label = holiday_map.get(day)
        if holiday_label:
            day_events.append(
                schemas.ScheduleDayEvent(
                    shift_date=day,
                    kind="holiday",
                    label=holiday_label,
                )
            )

    for employee in employee_rows:
        birth = employee.birth_date
        if not birth:
            continue
        for day in week_days:
            if day.month == birth.month and day.day == birth.day:
                day_events.append(
                    schemas.ScheduleDayEvent(
                        shift_date=day,
                        kind="birthday",
                        label=f"Cumpleaños: {employee.name}",
                        employee_id=employee.id,
                        employee_name=employee.name,
                    )
                )

    return schemas.ScheduleWeekView(
        week=schemas.ScheduleWeekRead.model_validate(week),
        employees=employee_rows,
        shifts=shift_payload,
        day_totals=day_totals,
        day_events=day_events,
        week_total_hours=week_total_hours,
    )


# ===================== POS STATIONS =====================


_station_logger = logging.getLogger("kensar.pos_station")
_closure_logger = logging.getLogger("kensar.pos_closure")


def _is_pos_web_name(pos_name: Optional[str]) -> bool:
    if not pos_name:
        return False
    return "pos web" in pos_name.strip().lower()


def _filter_pos_name(query, column, pos_name: Optional[str]):
    if not pos_name:
        return query
    if _is_pos_web_name(pos_name):
        return query.filter(func.lower(column).contains("pos web"))
    return query.filter(column == pos_name)


def _station_label_from_pos_name(pos_name: Optional[str]) -> Optional[str]:
    if not pos_name:
        return None
    normalized = re.sub(r"^(pos\s+)+", "", pos_name.strip(), flags=re.IGNORECASE)
    return normalized or None


def _resolve_station_id_from_pos_name(
    db: Session,
    pos_name: Optional[str],
    tenant_id: Optional[int] = None,
) -> Optional[str]:
    label = _station_label_from_pos_name(pos_name)
    if not label:
        return None
    stations = (
        db.query(models.PosStation)
        .filter(
            func.lower(models.PosStation.label) == label.lower(),
            models.PosStation.is_active.is_(True),
            (
                models.PosStation.tenant_id == (tenant_id if tenant_id is not None else get_default_tenant_id(db))
                if (tenant_id if tenant_id is not None else get_default_tenant_id(db)) is not None
                else true()
            ),
        )
        .all()
    )
    if len(stations) == 1:
        return stations[0].id
    return None


def _tenant_requires_station(
    db: Session,
    tenant_id: Optional[int] = None,
) -> bool:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.PosStation).filter(models.PosStation.is_active.is_(True))
    if effective_tenant_id is not None:
        query = query.filter(models.PosStation.tenant_id == effective_tenant_id)
    return query.count() > 0


def _resolve_station_id(
    db: Session,
    station_id: Optional[str],
    tenant_id: Optional[int] = None,
) -> Optional[str]:
    if not station_id:
        return None
    station = (
        db.query(models.PosStation)
        .filter(
            models.PosStation.id == station_id,
            (
                models.PosStation.tenant_id == (tenant_id if tenant_id is not None else get_default_tenant_id(db))
                if (tenant_id if tenant_id is not None else get_default_tenant_id(db)) is not None
                else true()
            ),
        )
        .first()
    )
    if not station or not station.is_active:
        raise ValueError("Estación inválida o inactiva")
    return station.id


def _generate_station_pin(length: int = 6) -> str:
    digits = string.digits
    return "".join(secrets.choice(digits) for _ in range(length))


def list_pos_stations(
    db: Session,
    tenant_id: Optional[int] = None,
) -> List[models.PosStation]:
    query = db.query(models.PosStation)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosStation.tenant_id == effective_tenant_id)
    return (
        query
        .options(
            selectinload(models.PosStation.user),
            selectinload(models.PosStation.parent_station),
        )
        .order_by(models.PosStation.created_at.desc())
        .all()
    )


def get_pos_station(
    db: Session,
    station_id: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosStation]:
    query = (
        db.query(models.PosStation)
        .options(
            selectinload(models.PosStation.user),
            selectinload(models.PosStation.parent_station),
        )
        .filter(models.PosStation.id == station_id)
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosStation.tenant_id == effective_tenant_id)
    return query.first()


def get_pos_station_any(
    db: Session,
    station_id: str,
) -> Optional[models.PosStation]:
    return (
        db.query(models.PosStation)
        .options(
            selectinload(models.PosStation.user),
            selectinload(models.PosStation.parent_station),
        )
        .filter(models.PosStation.id == station_id)
        .first()
    )


def get_pos_station_by_label(
    db: Session,
    label: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosStation]:
    query = (
        db.query(models.PosStation)
        .options(
            selectinload(models.PosStation.user),
            selectinload(models.PosStation.parent_station),
        )
        .filter(func.lower(models.PosStation.label) == label.lower())
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosStation.tenant_id == effective_tenant_id)
    return query.first()


def list_stock_devices(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    active_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> List[models.StockDevice]:
    query = (
        db.query(models.StockDevice)
        .options(selectinload(models.StockDevice.created_by))
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.StockDevice.tenant_id == effective_tenant_id)
    if active_only:
        query = query.filter(models.StockDevice.is_active.is_(True))
    return (
        query
        .order_by(models.StockDevice.updated_at.desc(), models.StockDevice.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def count_stock_devices(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    active_only: bool = False,
) -> int:
    query = db.query(func.count(models.StockDevice.id))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.StockDevice.tenant_id == effective_tenant_id)
    if active_only:
        query = query.filter(models.StockDevice.is_active.is_(True))
    return int(query.scalar() or 0)


def get_stock_device(
    db: Session,
    stock_device_id: str,
    *,
    tenant_id: Optional[int] = None,
) -> Optional[models.StockDevice]:
    query = (
        db.query(models.StockDevice)
        .options(selectinload(models.StockDevice.created_by))
        .filter(models.StockDevice.id == stock_device_id)
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.StockDevice.tenant_id == effective_tenant_id)
    return query.first()


def get_stock_device_by_name(
    db: Session,
    name: str,
    *,
    tenant_id: Optional[int] = None,
) -> Optional[models.StockDevice]:
    query = (
        db.query(models.StockDevice)
        .options(selectinload(models.StockDevice.created_by))
        .filter(func.lower(models.StockDevice.name) == name.strip().lower())
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.StockDevice.tenant_id == effective_tenant_id)
    return query.first()


def create_stock_device(
    db: Session,
    payload: schemas.StockDeviceCreate,
    *,
    tenant_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None,
) -> models.StockDevice:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    normalized_name = payload.name.strip()
    if get_stock_device_by_name(db, normalized_name, tenant_id=effective_tenant_id):
        raise ValueError("Ya existe un dispositivo con ese nombre")

    device = models.StockDevice(
        id=str(uuid4()),
        tenant_id=effective_tenant_id,
        name=normalized_name,
        is_active=True,
        bound_device_id=_clean_field(payload.bound_device_id),
        bound_device_label=_clean_field(payload.bound_device_label),
        created_by_user_id=created_by_user_id,
        last_seen_at=datetime.utcnow(),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def update_stock_device(
    db: Session,
    device: models.StockDevice,
    payload: schemas.StockDeviceUpdate,
) -> models.StockDevice:
    if payload.name is not None:
        next_name = payload.name.strip()
        if not next_name:
            raise ValueError("El nombre del dispositivo es obligatorio")
        existing = get_stock_device_by_name(db, next_name, tenant_id=device.tenant_id)
        if existing and existing.id != device.id:
            raise ValueError("Ya existe un dispositivo con ese nombre")
        device.name = next_name

    if payload.is_active is not None:
        device.is_active = bool(payload.is_active)

    if payload.bound_device_id is not None:
        device.bound_device_id = _clean_field(payload.bound_device_id)
    if payload.bound_device_label is not None:
        device.bound_device_label = _clean_field(payload.bound_device_label)
    if payload.touch_seen:
        device.last_seen_at = datetime.utcnow()

    db.commit()
    db.refresh(device)
    return device


def get_pos_station_by_email(
    db: Session,
    station_email: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosStation]:
    query = db.query(models.PosStation).filter(
        func.lower(models.PosStation.station_email) == station_email.lower()
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosStation.tenant_id == effective_tenant_id)
    return query.first()


def get_pos_station_by_email_any(
    db: Session,
    station_email: str,
) -> Optional[models.PosStation]:
    return (
        db.query(models.PosStation)
        .filter(func.lower(models.PosStation.station_email) == station_email.lower())
        .first()
    )


def create_pos_station(
    db: Session,
    payload: schemas.PosStationCreate,
    tenant_id: Optional[int] = None,
) -> tuple[models.PosStation, str]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    existing = get_pos_station_by_email(
        db,
        payload.station_email,
        tenant_id=effective_tenant_id,
    )
    if existing:
        raise ValueError("Ya existe una estación con ese correo")

    parent_station_id = payload.parent_station_id
    if payload.station_type != "tablet":
        parent_station_id = None
    if payload.station_type == "tablet":
        if not parent_station_id:
            raise ValueError("Las estaciones tablet deben vincularse a una estación desktop activa")
        parent_station = get_pos_station(db, parent_station_id, tenant_id=effective_tenant_id)
        if not parent_station or not parent_station.is_active:
            raise ValueError("La estación principal vinculada no existe o está inactiva")
        if (parent_station.station_type or "desktop") != "desktop":
            raise ValueError("Solo puedes vincular tablets a estaciones desktop")
    if payload.station_type == "desktop" and payload.parent_station_id:
        raise ValueError("Solo las estaciones tablet se pueden vincular a una estación principal")

    station = models.PosStation(
        id=str(uuid4()),
        tenant_id=effective_tenant_id,
        label=payload.label,
        station_type=payload.station_type,
        parent_station_id=parent_station_id,
        station_email=payload.station_email,
        station_password_hash=hash_password(payload.station_password),
        pin_hash=None,
        is_active=True,
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return station, ""


def update_pos_station(
    db: Session,
    station: models.PosStation,
    payload: schemas.PosStationUpdate,
    tenant_id: Optional[int] = None,
) -> tuple[models.PosStation, Optional[str]]:
    effective_tenant_id = tenant_id if tenant_id is not None else station.tenant_id
    data = payload.model_dump(exclude_unset=True)
    pin_plain: Optional[str] = None
    if "label" in data and data["label"] is not None:
        station.label = data["label"]
    if "station_type" in data and data["station_type"] is not None:
        station.station_type = data["station_type"]
        if station.station_type != "tablet":
            station.parent_station_id = None
    if "is_active" in data and data["is_active"] is not None:
        station.is_active = data["is_active"]
    if "station_email" in data and data["station_email"]:
        existing = get_pos_station_by_email(
            db,
            data["station_email"],
            tenant_id=effective_tenant_id,
        )
        if existing and existing.id != station.id:
            raise ValueError("Ya existe una estación con ese correo")
        station.station_email = data["station_email"]
    if "parent_station_id" in data:
        next_parent_id = data["parent_station_id"]
        if station.station_type != "tablet" and next_parent_id:
            raise ValueError("Solo las estaciones tablet se pueden vincular a una estación principal")
        if station.station_type != "tablet":
            station.parent_station_id = None
        elif not next_parent_id:
            station.parent_station_id = None
        else:
            parent_station = get_pos_station(db, next_parent_id, tenant_id=effective_tenant_id)
            if not parent_station or not parent_station.is_active:
                raise ValueError("La estación principal vinculada no existe o está inactiva")
            if parent_station.id == station.id:
                raise ValueError("Una estación no se puede vincular a sí misma")
            if (parent_station.station_type or "desktop") != "desktop":
                raise ValueError("Solo puedes vincular tablets a estaciones desktop")
            station.parent_station_id = parent_station.id
    if station.station_type == "tablet":
        if not station.parent_station_id:
            raise ValueError("Las estaciones tablet deben vincularse a una estación desktop activa")
        parent_station = get_pos_station(
            db,
            station.parent_station_id,
            tenant_id=effective_tenant_id,
        )
        if not parent_station or not parent_station.is_active:
            raise ValueError("La estación principal vinculada no existe o está inactiva")
        if parent_station.id == station.id:
            raise ValueError("Una estación no se puede vincular a sí misma")
        if (parent_station.station_type or "desktop") != "desktop":
            raise ValueError("Solo puedes vincular tablets a estaciones desktop")
    if "station_password" in data and data["station_password"]:
        station.station_password_hash = hash_password(data["station_password"])
    if payload.pin_plain:
        pin_plain = payload.pin_plain
        station.pin_hash = hash_password(pin_plain)
        station.failed_attempts = 0
    elif payload.reset_pin:
        pin_plain = _generate_station_pin()
        station.pin_hash = hash_password(pin_plain)
        station.failed_attempts = 0
    db.commit()
    db.refresh(station)
    return station, pin_plain


def update_pos_station_printer_config(
    db: Session,
    station: models.PosStation,
    payload: schemas.PosStationPrinterConfigUpdate,
) -> models.PosStation:
    data = payload.model_dump(exclude_unset=True)
    if "printer_mode" in data:
        station.printer_mode = data["printer_mode"]
    if "printer_name" in data:
        station.printer_name = data["printer_name"]
    if "printer_width" in data:
        station.printer_width = data["printer_width"]
    if "printer_auto_open_drawer" in data:
        station.printer_auto_open_drawer = data["printer_auto_open_drawer"]
    if "printer_show_drawer_button" in data:
        station.printer_show_drawer_button = data["printer_show_drawer_button"]
    db.commit()
    db.refresh(station)
    return station


def deactivate_pos_station(db: Session, station: models.PosStation):
    station.is_active = False
    db.commit()
    db.refresh(station)
    return station


def register_pos_station_login_success(db: Session, station: models.PosStation):
    station.last_login_at = datetime.utcnow()
    station.failed_attempts = 0
    db.commit()


def register_pos_station_login_failure(db: Session, station: models.PosStation):
    station.failed_attempts = int(station.failed_attempts or 0) + 1
    station.last_failed_at = datetime.utcnow()
    db.commit()


def get_active_pos_station_notice(
    db: Session,
    station_id: str,
) -> Optional[models.PosStationNotice]:
    return (
        db.query(models.PosStationNotice)
        .filter(
            models.PosStationNotice.station_id == station_id,
            models.PosStationNotice.dismissed_at.is_(None),
        )
        .order_by(models.PosStationNotice.created_at.desc())
        .first()
    )


def get_pos_station_notice(
    db: Session,
    station_id: str,
    notice_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosStationNotice]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    return (
        db.query(models.PosStationNotice)
        .filter(
            models.PosStationNotice.station_id == station_id,
            models.PosStationNotice.id == notice_id,
            (
                models.PosStationNotice.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .first()
    )


def create_pos_station_notice(
    db: Session,
    station: models.PosStation,
    message: str,
    user: models.PosUser,
) -> models.PosStationNotice:
    now = datetime.utcnow()
    (
        db.query(models.PosStationNotice)
        .filter(
            models.PosStationNotice.station_id == station.id,
            models.PosStationNotice.dismissed_at.is_(None),
        )
        .update(
            {
                models.PosStationNotice.dismissed_at: now,
                models.PosStationNotice.dismissed_by_user_id: user.id,
            },
            synchronize_session=False,
        )
    )
    notice = models.PosStationNotice(
        tenant_id=station.tenant_id or user.tenant_id or get_default_tenant_id(db),
        station_id=station.id,
        message=message,
        created_by_user_id=user.id,
    )
    db.add(notice)
    db.commit()
    db.refresh(notice)
    return notice


def dismiss_pos_station_notice(
    db: Session,
    notice: models.PosStationNotice,
    user: models.PosUser,
) -> None:
    notice.dismissed_at = datetime.utcnow()
    notice.dismissed_by_user_id = user.id
    db.commit()


# ===================== LOGIN 2FA =====================


def _login_2fa_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _trusted_device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invalidate_platform_login_2fa_challenges(db: Session, platform_user_id: int) -> None:
    now = datetime.utcnow()
    (
        db.query(models.PlatformLogin2FAChallenge)
        .filter(
            models.PlatformLogin2FAChallenge.platform_user_id == platform_user_id,
            models.PlatformLogin2FAChallenge.consumed_at.is_(None),
        )
        .update(
            {models.PlatformLogin2FAChallenge.consumed_at: now},
            synchronize_session=False,
        )
    )
    db.commit()


def create_platform_login_2fa_challenge(
    db: Session,
    user: models.PlatformUser,
    *,
    code: str,
    expires_at: datetime,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> models.PlatformLogin2FAChallenge:
    invalidate_platform_login_2fa_challenges(db, user.id)
    challenge = models.PlatformLogin2FAChallenge(
        platform_user_id=user.id,
        code_hash=_login_2fa_code_hash(code),
        expires_at=expires_at,
        attempts=0,
        user_agent=_clean_field(user_agent),
        ip_address=_clean_field(ip_address),
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return challenge


def get_platform_login_2fa_challenge(
    db: Session,
    challenge_id: int,
) -> Optional[models.PlatformLogin2FAChallenge]:
    return (
        db.query(models.PlatformLogin2FAChallenge)
        .filter(models.PlatformLogin2FAChallenge.id == challenge_id)
        .first()
    )


def verify_platform_login_2fa_code(
    db: Session,
    challenge: models.PlatformLogin2FAChallenge,
    code: str,
    *,
    max_attempts: int = 5,
) -> bool:
    now = datetime.utcnow()
    if challenge.consumed_at is not None:
        return False
    if challenge.expires_at < now:
        return False
    if int(challenge.attempts or 0) >= max_attempts:
        challenge.consumed_at = now
        db.commit()
        return False
    if challenge.code_hash != _login_2fa_code_hash(code):
        challenge.attempts = int(challenge.attempts or 0) + 1
        if int(challenge.attempts or 0) >= max_attempts:
            challenge.consumed_at = now
        db.commit()
        return False
    challenge.consumed_at = now
    db.commit()
    return True


def get_platform_trusted_device(
    db: Session,
    platform_user_id: int,
    token: str,
) -> Optional[models.PlatformTrustedDevice]:
    token_hash = _trusted_device_token_hash(token)
    now = datetime.utcnow()
    device = (
        db.query(models.PlatformTrustedDevice)
        .filter(
            models.PlatformTrustedDevice.platform_user_id == platform_user_id,
            models.PlatformTrustedDevice.token_hash == token_hash,
            models.PlatformTrustedDevice.revoked_at.is_(None),
        )
        .first()
    )
    if not device:
        return None
    if device.expires_at < now:
        device.revoked_at = now
        db.commit()
        return None
    device.last_used_at = now
    db.commit()
    db.refresh(device)
    return device


def trust_platform_device(
    db: Session,
    user: models.PlatformUser,
    token: str,
    *,
    expires_at: datetime,
    device_label: Optional[str] = None,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> models.PlatformTrustedDevice:
    token_hash = _trusted_device_token_hash(token)
    now = datetime.utcnow()
    device = (
        db.query(models.PlatformTrustedDevice)
        .filter(
            models.PlatformTrustedDevice.platform_user_id == user.id,
            models.PlatformTrustedDevice.token_hash == token_hash,
        )
        .first()
    )
    if not device:
        device = models.PlatformTrustedDevice(
            platform_user_id=user.id,
            token_hash=token_hash,
            device_label=_clean_field(device_label),
            user_agent=_clean_field(user_agent),
            last_ip=_clean_field(ip_address),
            expires_at=expires_at,
            last_used_at=now,
        )
        db.add(device)
    else:
        device.revoked_at = None
        device.expires_at = expires_at
        device.last_used_at = now
        if device_label:
            device.device_label = device_label.strip()
        if user_agent:
            device.user_agent = user_agent.strip()
        if ip_address:
            device.last_ip = ip_address.strip()
    db.commit()
    db.refresh(device)
    return device


# ===================== PASSWORD RESET TOKENS =====================


def _password_reset_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invalidate_password_reset_tokens(db: Session, user_id: int) -> None:
    now = datetime.utcnow()
    (
        db.query(models.PasswordReset)
        .filter(
            models.PasswordReset.user_id == user_id,
            models.PasswordReset.used_at.is_(None),
        )
        .update({models.PasswordReset.used_at: now}, synchronize_session=False)
    )
    db.commit()


def create_password_reset_token(
    db: Session,
    user: models.PosUser,
    token: str,
    expires_at: datetime,
) -> models.PasswordReset:
    reset = models.PasswordReset(
        tenant_id=user.tenant_id or get_default_tenant_id(db),
        user_id=user.id,
        token_hash=_password_reset_token_hash(token),
        expires_at=expires_at,
    )
    db.add(reset)
    db.commit()
    db.refresh(reset)
    return reset


def get_password_reset_by_token(
    db: Session,
    token: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.PasswordReset]:
    token_hash = _password_reset_token_hash(token)
    query = db.query(models.PasswordReset).filter(models.PasswordReset.token_hash == token_hash)
    if tenant_id is not None:
        query = query.filter(models.PasswordReset.tenant_id == tenant_id)
    return query.first()


def complete_password_reset(
    db: Session,
    reset: models.PasswordReset,
    new_password: str,
) -> models.PosUser:
    reset.used_at = datetime.utcnow()
    user = reset.user
    user.password_hash = hash_password(new_password)
    user.accepted_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    db.refresh(reset)
    return user


# ===================== POS CUSTOMERS =====================


def list_pos_customers(
    db: Session,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    include_inactive: bool = False,
    include_web_customers: bool = True,
    tenant_id: Optional[int] = None,
):
    # Backward-compatibility repair: older guest checkout used a placeholder email
    # with `.local`, which fails EmailStr validation in API responses.
    legacy_guest_email = "__guest_checkout__@kensar.local"
    canonical_guest_email = "__guest_checkout__@kensar.example.com"
    repair_query = db.query(models.PosCustomer).filter(
        func.lower(models.PosCustomer.email) == legacy_guest_email
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        repair_query = repair_query.filter(models.PosCustomer.tenant_id == effective_tenant_id)
    legacy_rows = repair_query.all()
    if legacy_rows:
        for row in legacy_rows:
            row.email = canonical_guest_email
            db.add(row)
        db.commit()

    query = db.query(models.PosCustomer)
    if effective_tenant_id is not None:
        query = query.filter(models.PosCustomer.tenant_id == effective_tenant_id)
    if not include_inactive:
        query = query.filter(models.PosCustomer.is_active.is_(True))
    if not include_web_customers:
        query = query.filter(~models.PosCustomer.web_accounts.any())

    if search:
        pattern = f"%{search.lower()}%"
        query = query.filter(
            or_(
                func.lower(models.PosCustomer.name).like(pattern),
                func.lower(models.PosCustomer.phone).like(pattern),
                func.lower(models.PosCustomer.email).like(pattern),
                func.lower(models.PosCustomer.tax_id).like(pattern),
            )
        )

    return (
        query.order_by(models.PosCustomer.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def list_pos_frequent_customers(
    db: Session,
    min_sales: int = 5,
    limit: int = 10,
    include_web_customers: bool = True,
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    count_expr = func.count(models.Sale.id)
    query = (
        db.query(models.PosCustomer, count_expr.label("sales_count"))
        .join(models.Sale, models.Sale.customer_id == models.PosCustomer.id)
        .filter(models.PosCustomer.is_active.is_(True))
        .filter(
            ~models.PosCustomer.web_accounts.any()
            if not include_web_customers
            else true()
        )
        .filter(
            models.PosCustomer.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .filter(
            models.Sale.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true()
        )
        .group_by(models.PosCustomer.id)
        .having(count_expr >= min_sales)
        .order_by(count_expr.desc(), func.lower(models.PosCustomer.name).asc())
        .limit(limit)
    )
    results = []
    for customer, sales_count in query.all():
        results.append(
            {
                "id": customer.id,
                "name": customer.name,
                "phone": customer.phone,
                "email": customer.email,
                "tax_id": customer.tax_id,
                "address": customer.address,
                "is_active": customer.is_active,
                "created_at": customer.created_at,
                "updated_at": customer.updated_at,
                "sales_count": int(sales_count or 0),
            }
        )
    return results


def get_pos_customer(
    db: Session,
    customer_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosCustomer]:
    query = db.query(models.PosCustomer).filter(models.PosCustomer.id == customer_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosCustomer.tenant_id == effective_tenant_id)
    return query.first()


def create_pos_customer(
    db: Session,
    customer_in: schemas.PosCustomerCreate,
    tenant_id: Optional[int] = None,
) -> models.PosCustomer:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    customer = models.PosCustomer(
        tenant_id=effective_tenant_id,
        name=customer_in.name,
        phone=customer_in.phone,
        email=customer_in.email,
        tax_id=customer_in.tax_id,
        address=customer_in.address,
        is_active=True,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def update_pos_customer(
    db: Session,
    customer: models.PosCustomer,
    customer_in: schemas.PosCustomerUpdate,
) -> models.PosCustomer:
    data = customer_in.model_dump(exclude_unset=True)

    for field, value in data.items():
        if field == "is_active":
            customer.is_active = bool(value)
            continue
        setattr(customer, field, value)

    db.commit()
    db.refresh(customer)
    return customer


def soft_delete_pos_customer(db: Session, customer: models.PosCustomer):
    customer.is_active = False
    db.commit()
    db.refresh(customer)
    return customer


def get_web_customer_account(
    db: Session,
    account_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.WebCustomerAccount]:
    query = (
        db.query(models.WebCustomerAccount)
        .options(joinedload(models.WebCustomerAccount.customer))
        .filter(models.WebCustomerAccount.id == account_id)
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.WebCustomerAccount.tenant_id == effective_tenant_id)
    return query.first()


def get_web_customer_account_by_email(
    db: Session,
    email: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.WebCustomerAccount]:
    query = (
        db.query(models.WebCustomerAccount)
        .options(joinedload(models.WebCustomerAccount.customer))
        .filter(func.lower(models.WebCustomerAccount.email) == email.strip().lower())
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.WebCustomerAccount.tenant_id == effective_tenant_id)
    return query.first()


def _find_pos_customer_by_email(
    db: Session,
    email: str,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosCustomer]:
    query = db.query(models.PosCustomer).filter(func.lower(models.PosCustomer.email) == email.strip().lower())
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosCustomer.tenant_id == effective_tenant_id)
    return query.first()


def create_web_customer_account(
    db: Session,
    payload: schemas.WebCustomerRegisterRequest,
    tenant_id: Optional[int] = None,
) -> models.WebCustomerAccount:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    normalized_name = payload.name.strip()
    if not normalized_name:
        raise ValueError("El nombre es obligatorio")
    normalized_email = payload.email.strip().lower()
    normalized_phone = payload.phone.strip() if payload.phone and payload.phone.strip() else None
    normalized_tax_id = payload.tax_id.strip() if payload.tax_id and payload.tax_id.strip() else None
    normalized_address = payload.address.strip() if payload.address and payload.address.strip() else None
    existing = get_web_customer_account_by_email(db, normalized_email, tenant_id=effective_tenant_id)
    if existing:
        raise ValueError("Ya existe una cuenta registrada con ese correo")

    customer = _find_pos_customer_by_email(db, normalized_email, tenant_id=effective_tenant_id)
    if customer:
        customer.name = normalized_name
        if normalized_phone is not None:
            customer.phone = normalized_phone
        if normalized_tax_id is not None:
            customer.tax_id = normalized_tax_id
        if normalized_address is not None:
            customer.address = normalized_address
        customer.is_active = True
    else:
        customer = models.PosCustomer(
            tenant_id=effective_tenant_id,
            name=normalized_name,
            phone=normalized_phone,
            email=normalized_email,
            tax_id=normalized_tax_id,
            address=normalized_address,
            is_active=True,
        )
        db.add(customer)
        db.flush()

    account = models.WebCustomerAccount(
        tenant_id=effective_tenant_id,
        pos_customer_id=customer.id,
        email=normalized_email,
        password_hash=hash_password(payload.password),
        is_active=True,
        email_verified=False,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return get_web_customer_account(db, account.id, tenant_id=effective_tenant_id)


def get_or_create_guest_web_customer_account(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
) -> models.WebCustomerAccount:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    guest_email = "__guest_checkout__@kensar.example.com"
    legacy_guest_email = "__guest_checkout__@kensar.local"

    existing = get_web_customer_account_by_email(db, guest_email, tenant_id=effective_tenant_id)
    if not existing:
        existing = get_web_customer_account_by_email(db, legacy_guest_email, tenant_id=effective_tenant_id)
    if existing:
        if (existing.email or "").strip().lower() != guest_email:
            existing.email = guest_email
            if existing.customer and (existing.customer.email or "").strip().lower() != guest_email:
                existing.customer.email = guest_email
            db.add(existing)
            db.commit()
            db.refresh(existing)
        return existing

    guest_customer = (
        db.query(models.PosCustomer)
        .filter(
            models.PosCustomer.tenant_id == effective_tenant_id
            if effective_tenant_id is not None
            else true(),
            or_(
                func.lower(models.PosCustomer.email) == guest_email,
                func.lower(models.PosCustomer.email) == legacy_guest_email,
            ),
        )
        .first()
    )
    if not guest_customer:
        guest_customer = models.PosCustomer(
            tenant_id=effective_tenant_id,
            name="Cliente invitado web",
            phone=None,
            email=guest_email,
            tax_id=None,
            address=None,
            is_active=True,
        )
        db.add(guest_customer)
        db.flush()
    elif (guest_customer.email or "").strip().lower() != guest_email:
        guest_customer.email = guest_email
        db.add(guest_customer)
        db.flush()

    account = models.WebCustomerAccount(
        tenant_id=effective_tenant_id,
        pos_customer_id=guest_customer.id,
        email=guest_email,
        password_hash=hash_password(secrets.token_urlsafe(24)),
        is_active=True,
        email_verified=False,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return get_web_customer_account(db, account.id, tenant_id=effective_tenant_id)


def revoke_web_customer_sessions(
    db: Session,
    account_id: int,
    reason: str = "replaced",
) -> None:
    now = datetime.utcnow()
    (
        db.query(models.WebCustomerSession)
        .filter(
            models.WebCustomerSession.account_id == account_id,
            models.WebCustomerSession.revoked_at.is_(None),
        )
        .update(
            {
                models.WebCustomerSession.revoked_at: now,
                models.WebCustomerSession.revoked_reason: reason,
            },
            synchronize_session=False,
        )
    )
    db.commit()


def create_web_customer_session(
    db: Session,
    account_id: int,
    token: str,
    expires_at: datetime,
) -> models.WebCustomerSession:
    account = get_web_customer_account(db, account_id)
    if not account:
        raise ValueError("Cuenta web no encontrada")
    session = models.WebCustomerSession(
        tenant_id=account.tenant_id,
        account_id=account.id,
        token_hash=_session_token_hash(token),
        created_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_web_customer_session_by_token(
    db: Session,
    token: str,
) -> Optional[models.WebCustomerSession]:
    return (
        db.query(models.WebCustomerSession)
        .options(
            joinedload(models.WebCustomerSession.account).joinedload(models.WebCustomerAccount.customer)
        )
        .filter(models.WebCustomerSession.token_hash == _session_token_hash(token))
        .first()
    )


def get_active_web_cart(
    db: Session,
    account_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.WebCart]:
    query = (
        db.query(models.WebCart)
        .options(
            joinedload(models.WebCart.items).joinedload(models.WebCartItem.product)
        )
        .filter(
            models.WebCart.account_id == account_id,
            models.WebCart.status == "active",
        )
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.WebCart.tenant_id == effective_tenant_id)
    return query.order_by(models.WebCart.updated_at.desc()).first()


def get_or_create_active_web_cart(
    db: Session,
    account: models.WebCustomerAccount,
) -> models.WebCart:
    cart = get_active_web_cart(db, account.id, tenant_id=account.tenant_id)
    if cart:
        return cart
    cart = models.WebCart(
        tenant_id=account.tenant_id,
        account_id=account.id,
        status="active",
        currency="COP",
    )
    db.add(cart)
    db.commit()
    db.refresh(cart)
    return get_active_web_cart(db, account.id, tenant_id=account.tenant_id)


def _get_cart_item(
    db: Session,
    cart_id: int,
    product_id: int,
) -> Optional[models.WebCartItem]:
    return (
        db.query(models.WebCartItem)
        .filter(
            models.WebCartItem.cart_id == cart_id,
            models.WebCartItem.product_id == product_id,
        )
        .first()
    )


def _get_web_cart_stock_snapshot(
    db: Session,
    tenant_id: Optional[int],
    product_ids: list[int],
) -> dict[int, float]:
    if not product_ids:
        return {}
    rows = (
        db.query(
            models.InventoryMovement.product_id,
            func.coalesce(func.sum(models.InventoryMovement.qty_delta), 0).label("qty_on_hand"),
        )
        .filter(models.InventoryMovement.product_id.in_(product_ids))
        .filter(
            models.InventoryMovement.tenant_id == tenant_id
            if tenant_id is not None
            else true()
        )
        .group_by(models.InventoryMovement.product_id)
        .all()
    )
    return {int(product_id): float(qty_on_hand or 0.0) for product_id, qty_on_hand in rows}


def _resolve_valid_discount_code(
    db: Session,
    *,
    tenant_id: Optional[int],
    code: Optional[str] = None,
    discount_code_id: Optional[int] = None,
) -> Optional[models.WebDiscountCode]:
    query = db.query(models.WebDiscountCode).filter(
        models.WebDiscountCode.tenant_id == tenant_id,
    )
    if discount_code_id is not None:
        query = query.filter(models.WebDiscountCode.id == int(discount_code_id))
    elif code:
        query = query.filter(models.WebDiscountCode.code == _normalize_discount_code(code))
    else:
        return None

    row = query.first()
    if not row:
        return None

    now = datetime.utcnow()
    if not bool(row.is_active):
        return None
    if row.starts_at and row.starts_at > now:
        return None
    if row.ends_at and row.ends_at < now:
        return None
    if row.max_uses is not None and int(row.uses_count or 0) >= int(row.max_uses):
        return None
    return row


def _resolve_cart_coupon_snapshot(
    db: Session,
    cart: models.WebCart,
) -> tuple[Optional[str], str, float, float, Optional[models.WebDiscountCode]]:
    saved_code = _normalize_discount_code(getattr(cart, "coupon_code", None) or "")
    saved_code_id = getattr(cart, "coupon_discount_code_id", None)
    if not saved_code:
        return None, "percent", 0.0, 0.0, None

    valid_row = _resolve_valid_discount_code(
        db,
        tenant_id=cart.tenant_id,
        code=saved_code,
        discount_code_id=saved_code_id,
    )
    if not valid_row:
        return None, "percent", 0.0, 0.0, None
    discount_type, discount_value, discount_percent = _resolve_discount_code_snapshot_values(
        discount_type=getattr(valid_row, "discount_type", None),
        discount_value=getattr(valid_row, "discount_value", None),
        discount_percent=getattr(valid_row, "discount_percent", None),
    )
    if discount_value <= 0:
        return None, "percent", 0.0, 0.0, None
    return saved_code, discount_type, discount_value, discount_percent, valid_row


def _serialize_web_cart(
    db: Session,
    cart: models.WebCart,
) -> schemas.WebCartRead:
    items = list(cart.items or [])
    product_ids = [item.product_id for item in items]
    qty_by_product = _get_web_cart_stock_snapshot(db, cart.tenant_id, product_ids)
    serialized_items: list[schemas.WebCartItemRead] = []
    subtotal_base = 0.0

    for item in items:
        product = item.product
        if not product:
            continue
        unit_price = float(item.unit_price_snapshot or resolve_web_product_sale_price(product) or 0.0)
        quantity = float(item.quantity or 0.0)
        line_total = unit_price * quantity
        subtotal_base += line_total
        serialized_items.append(
            schemas.WebCartItemRead(
                id=item.id,
                product_id=product.id,
                product_name=product.name,
                product_slug=resolve_product_web_slug(product),
                product_sku=product.sku,
                image_url=product.image_url,
                brand=product.brand,
                stock_status=resolve_web_product_stock_status(product, qty_by_product.get(product.id, 0.0)),
                quantity=quantity,
                unit_price=unit_price,
                compare_price=resolve_web_compare_price(product, sale_price=unit_price),
                line_total=line_total,
            )
        )

    coupon_code, coupon_discount_type, coupon_discount_value, coupon_discount_percent, _ = _resolve_cart_coupon_snapshot(db, cart)
    discount_amount = 0.0
    if coupon_code:
        discount_amount = _compute_coupon_discount_amount(
            subtotal_base,
            discount_type=coupon_discount_type,
            discount_value=coupon_discount_value,
            discount_percent=coupon_discount_percent,
        )
    total = max(0.0, subtotal_base - discount_amount)

    return schemas.WebCartRead(
        id=cart.id,
        status=cart.status,
        currency=cart.currency,
        items=serialized_items,
        items_count=len(serialized_items),
        subtotal_base=subtotal_base,
        discount_amount=discount_amount,
        subtotal=total,
        total=total,
        coupon_code=coupon_code,
        coupon_discount_percent=coupon_discount_percent,
        coupon_discount_type=(coupon_discount_type if coupon_code else None),
        coupon_discount_value=(coupon_discount_value if coupon_code else 0.0),
        updated_at=cart.updated_at,
    )


def get_web_cart(
    db: Session,
    account: models.WebCustomerAccount,
) -> schemas.WebCartRead:
    cart = get_or_create_active_web_cart(db, account)
    return _serialize_web_cart(db, cart)


def add_item_to_web_cart(
    db: Session,
    account: models.WebCustomerAccount,
    payload: schemas.WebCartItemMutationRequest,
) -> schemas.WebCartRead:
    cart = get_or_create_active_web_cart(db, account)
    product = get_product(db, payload.product_id, tenant_id=account.tenant_id)
    if not product or not product.active or not product.web_published:
        raise ValueError("Producto no disponible para web")

    existing = _get_cart_item(db, cart.id, product.id)
    web_unit_price = resolve_web_product_sale_price(product)
    if existing:
        existing.quantity = float(existing.quantity or 0.0) + float(payload.quantity)
        existing.unit_price_snapshot = float(web_unit_price or 0.0)
    else:
        existing = models.WebCartItem(
            tenant_id=cart.tenant_id,
            cart_id=cart.id,
            product_id=product.id,
            quantity=float(payload.quantity),
            unit_price_snapshot=float(web_unit_price or 0.0),
        )
        db.add(existing)

    cart.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(cart)
    return get_web_cart(db, account)


def update_web_cart_item_quantity(
    db: Session,
    account: models.WebCustomerAccount,
    product_id: int,
    quantity: float,
) -> schemas.WebCartRead:
    cart = get_or_create_active_web_cart(db, account)
    item = _get_cart_item(db, cart.id, product_id)
    if not item:
        raise ValueError("Producto no existe en el carrito")

    if quantity <= 0:
        db.delete(item)
    else:
        item.quantity = float(quantity)

    cart.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(cart)
    return get_web_cart(db, account)


def clear_web_cart(
    db: Session,
    account: models.WebCustomerAccount,
) -> schemas.WebCartRead:
    cart = get_or_create_active_web_cart(db, account)
    for item in list(cart.items or []):
        db.delete(item)
    cart.coupon_code = None
    cart.coupon_discount_percent = 0.0
    cart.coupon_discount_code_id = None
    cart.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(cart)
    return get_web_cart(db, account)


def clear_active_web_cart_if_exists(
    db: Session,
    *,
    account_id: int,
    tenant_id: Optional[int] = None,
) -> None:
    cart = get_active_web_cart(db, account_id, tenant_id=tenant_id)
    if not cart:
        return
    for item in list(cart.items or []):
        db.delete(item)
    cart.coupon_code = None
    cart.coupon_discount_percent = 0.0
    cart.coupon_discount_code_id = None
    cart.updated_at = datetime.utcnow()
    db.commit()


def apply_coupon_to_web_cart(
    db: Session,
    account: models.WebCustomerAccount,
    code: str,
) -> schemas.WebCartRead:
    cart = get_or_create_active_web_cart(db, account)
    if not list(cart.items or []):
        raise ValueError("El carrito está vacío")
    normalized = _normalize_discount_code(code)
    if not normalized:
        raise ValueError("Ingresa un código válido")
    row = _resolve_valid_discount_code(
        db,
        tenant_id=account.tenant_id,
        code=normalized,
    )
    if not row:
        raise ValueError("El código no está disponible o ya venció")

    cart.coupon_code = normalized
    cart.coupon_discount_percent = float(row.discount_percent or 0.0)
    cart.coupon_discount_code_id = int(row.id)
    cart.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(cart)
    return get_web_cart(db, account)


def clear_coupon_from_web_cart(
    db: Session,
    account: models.WebCustomerAccount,
) -> schemas.WebCartRead:
    cart = get_or_create_active_web_cart(db, account)
    cart.coupon_code = None
    cart.coupon_discount_percent = 0.0
    cart.coupon_discount_code_id = None
    cart.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(cart)
    return get_web_cart(db, account)


def _consume_discount_code_use(
    db: Session,
    *,
    discount_code_id: int,
    tenant_id: Optional[int],
) -> bool:
    updated = (
        db.query(models.WebDiscountCode)
        .filter(
            models.WebDiscountCode.id == discount_code_id,
            models.WebDiscountCode.tenant_id == tenant_id,
            models.WebDiscountCode.is_active.is_(True),
            or_(
                models.WebDiscountCode.max_uses.is_(None),
                models.WebDiscountCode.uses_count < models.WebDiscountCode.max_uses,
            ),
        )
        .update(
            {
                models.WebDiscountCode.uses_count: models.WebDiscountCode.uses_count + 1,
                models.WebDiscountCode.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    return bool(updated)


def _consume_web_order_coupon_if_needed(
    db: Session,
    *,
    order: models.WebOrder,
) -> None:
    if order.coupon_consumed_at is not None:
        return
    if int(order.coupon_discount_code_id or 0) <= 0:
        return

    consumed = _consume_discount_code_use(
        db,
        discount_code_id=int(order.coupon_discount_code_id),
        tenant_id=order.tenant_id,
    )
    if not consumed:
        # Si el cupón cambió de estado luego de crear la orden, no bloqueamos
        # la confirmación del pago ya recibido. De todas formas incrementamos
        # el contador para mantener trazabilidad de uso real.
        fallback = (
            db.query(models.WebDiscountCode)
            .filter(
                models.WebDiscountCode.id == int(order.coupon_discount_code_id),
                models.WebDiscountCode.tenant_id == order.tenant_id,
            )
            .update(
                {
                    models.WebDiscountCode.uses_count: models.WebDiscountCode.uses_count + 1,
                    models.WebDiscountCode.updated_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        if not fallback:
            return

    order.coupon_consumed_at = datetime.utcnow()
    order.updated_at = datetime.utcnow()
    db.add(order)


def get_next_web_order_number(
    db: Session,
    tenant_id: Optional[int] = None,
) -> int:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    orders_query = db.query(models.WebOrder)
    if effective_tenant_id is not None:
        orders_query = orders_query.filter(models.WebOrder.tenant_id == effective_tenant_id)
    max_number = orders_query.with_entities(func.max(models.WebOrder.web_order_number)).scalar()
    max_id = orders_query.with_entities(func.max(models.WebOrder.id)).scalar()
    candidates = [value for value in [max_number, max_id] if value is not None]
    current = int(max(candidates)) if candidates else 0
    return current + 1


def _create_web_order_status_log(
    db: Session,
    order: models.WebOrder,
    *,
    from_status: str | None,
    to_status: str,
    note: str | None = None,
    actor_type: str = "system",
    actor_user_id: int | None = None,
) -> models.WebOrderStatusLog:
    log = models.WebOrderStatusLog(
        tenant_id=order.tenant_id,
        web_order_id=order.id,
        from_status=from_status,
        to_status=to_status,
        note=note,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
    )
    db.add(log)
    return log


def get_web_order(
    db: Session,
    order_id: int,
    account_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.WebOrder]:
    query = (
        db.query(models.WebOrder)
        .options(
            joinedload(models.WebOrder.items).joinedload(models.WebOrderItem.product),
            joinedload(models.WebOrder.payments),
            joinedload(models.WebOrder.status_logs),
        )
        .filter(
            models.WebOrder.id == order_id,
            models.WebOrder.account_id == account_id,
        )
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.WebOrder.tenant_id == effective_tenant_id)
    return query.first()


def list_web_orders(
    db: Session,
    account: models.WebCustomerAccount,
    limit: int = 50,
) -> list[models.WebOrder]:
    expire_stale_web_orders(db, tenant_id=account.tenant_id)
    return (
        db.query(models.WebOrder)
        .options(
            joinedload(models.WebOrder.items).joinedload(models.WebOrderItem.product),
            joinedload(models.WebOrder.payments),
            joinedload(models.WebOrder.status_logs),
        )
        .filter(
            models.WebOrder.account_id == account.id,
            models.WebOrder.tenant_id == account.tenant_id
            if account.tenant_id is not None
            else true(),
        )
        .order_by(models.WebOrder.created_at.desc())
        .limit(limit)
        .all()
    )


def expire_stale_web_orders(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    minutes = _web_order_pending_expiry_minutes()
    if minutes <= 0:
        return 0
    instant = now or datetime.utcnow()
    cutoff = instant - timedelta(minutes=minutes)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)

    query = (
        db.query(models.WebOrder)
        .filter(models.WebOrder.sale_id.is_(None))
        .filter(models.WebOrder.status.in_(["pending_payment"]))
        .filter(models.WebOrder.payment_status.in_(["pending", "failed", "cancelled"]))
        .filter(func.coalesce(models.WebOrder.updated_at, models.WebOrder.created_at) <= cutoff)
    )
    if effective_tenant_id is not None:
        query = query.filter(models.WebOrder.tenant_id == effective_tenant_id)

    stale_orders = query.all()
    if not stale_orders:
        return 0

    expired = 0
    for order in stale_orders:
        try:
            _transition_web_order_status(
                db,
                order,
                to_status="payment_failed",
                note=f"Pago no confirmado en ventana esperada (>{minutes} min). Orden pendiente de reintento/sincronización.",
                actor_type="system",
            )
            expired += 1
        except ValueError:
            continue
    if expired > 0:
        db.commit()
    return expired


def expire_stale_web_orders_all_tenants(
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Expire stale web orders across every tenant with pending web payments."""
    tenant_rows = (
        db.query(models.WebOrder.tenant_id)
        .filter(models.WebOrder.sale_id.is_(None))
        .filter(models.WebOrder.status.in_(["pending_payment"]))
        .filter(models.WebOrder.payment_status.in_(["pending", "failed", "cancelled"]))
        .distinct()
        .all()
    )
    if not tenant_rows:
        return 0

    expired_total = 0
    processed_tenants: set[Optional[int]] = set()
    for (row_tenant_id,) in tenant_rows:
        if row_tenant_id in processed_tenants:
            continue
        processed_tenants.add(row_tenant_id)
        expired_total += expire_stale_web_orders(
            db,
            tenant_id=row_tenant_id,
            now=now,
        )
    return expired_total


def find_reusable_pending_web_order(
    db: Session,
    *,
    tenant_id: Optional[int],
    account_id: int,
    customer_email: Optional[str],
    currency: Optional[str],
    subtotal: float,
    discount_amount: float,
    total: float,
    item_signature: tuple[tuple[int, float, float, float], ...],
    reuse_window_minutes: Optional[int] = None,
) -> Optional[models.WebOrder]:
    if not item_signature:
        return None
    window_minutes = reuse_window_minutes
    if window_minutes is None:
        window_minutes = _web_order_reuse_window_minutes()
    if window_minutes <= 0:
        return None

    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    normalized_currency = (currency or "COP").strip().upper()
    normalized_email = _clean_field(customer_email)
    if isinstance(normalized_email, str):
        normalized_email = normalized_email.lower()

    query = (
        db.query(models.WebOrder)
        .options(joinedload(models.WebOrder.items))
        .filter(models.WebOrder.account_id == account_id)
        .filter(models.WebOrder.sale_id.is_(None))
        .filter(models.WebOrder.status.in_(["pending_payment", "payment_failed"]))
        .filter(models.WebOrder.payment_status.in_(["pending", "failed", "cancelled"]))
        .filter(models.WebOrder.created_at >= cutoff)
    )
    if tenant_id is not None:
        query = query.filter(models.WebOrder.tenant_id == tenant_id)
    if normalized_email:
        query = query.filter(func.lower(models.WebOrder.customer_email) == normalized_email)

    candidates = query.order_by(models.WebOrder.created_at.desc()).limit(20).all()
    for candidate in candidates:
        if (candidate.currency or "COP").strip().upper() != normalized_currency:
            continue
        if not _money_eq(candidate.subtotal, subtotal):
            continue
        if not _money_eq(candidate.discount_amount, discount_amount):
            continue
        if not _money_eq(candidate.total, total):
            continue
        if build_web_order_item_signature(candidate.items or []) != item_signature:
            continue
        return candidate
    return None


def _serialize_web_order(
    order: models.WebOrder,
) -> schemas.WebOrderRead:
    items = [
        schemas.WebOrderItemRead(
            id=item.id,
            product_id=item.product_id,
            product_name=item.product_name_snapshot,
            product_slug=(resolve_product_web_slug(item.product) if item.product else build_product_web_slug(item.product_name_snapshot, item.product_sku_snapshot)),
            product_sku=item.product_sku_snapshot,
            image_url=(item.product.image_url if item.product else None),
            quantity=float(item.quantity or 0.0),
            unit_price=float(item.unit_price_snapshot or 0.0),
            line_discount_value=float(item.line_discount_value or 0.0),
            line_total=float(item.line_total or 0.0),
        )
        for item in (order.items or [])
    ]
    payments = [
        schemas.WebOrderPaymentRead(
            id=payment.id,
            provider=payment.provider,
            provider_reference=payment.provider_reference,
            method=payment.method,
            status=payment.status,
            provider_status=(payment.raw_payload or {}).get("status"),
            status_detail=(payment.raw_payload or {}).get("status_detail"),
            amount=float(payment.amount or 0.0),
            currency=payment.currency,
            approved_at=payment.approved_at,
            failed_at=payment.failed_at,
            cancelled_at=payment.cancelled_at,
            created_at=payment.created_at,
        )
        for payment in (order.payments or [])
    ]
    status_logs = [
        schemas.WebOrderStatusLogRead(
            id=log.id,
            from_status=log.from_status,
            to_status=log.to_status,
            note=log.note,
            actor_type=log.actor_type,
            actor_user_id=log.actor_user_id,
            created_at=log.created_at,
        )
        for log in sorted(order.status_logs or [], key=lambda row: row.created_at)
    ]
    return schemas.WebOrderRead(
        id=order.id,
        account_id=order.account_id,
        pos_customer_id=order.pos_customer_id,
        web_order_number=order.web_order_number,
        document_number=order.document_number,
        status=order.status,
        payment_status=order.payment_status,
        fulfillment_status=order.fulfillment_status,
        customer_name=order.customer_name,
        customer_email=order.customer_email,
        customer_phone=order.customer_phone,
        customer_tax_id=order.customer_tax_id,
        customer_address=order.customer_address,
        subtotal=float(order.subtotal or 0.0),
        discount_amount=float(order.discount_amount or 0.0),
        shipping_amount=float(order.shipping_amount or 0.0),
        total=float(order.total or 0.0),
        currency=order.currency,
        notes=_sanitize_checkout_context_in_notes_for_backoffice(order.notes),
        submitted_at=order.submitted_at,
        paid_at=order.paid_at,
        cancelled_at=order.cancelled_at,
        converted_to_sale_at=order.converted_to_sale_at,
        sale_id=order.sale_id,
        sale_document_number=order.sale_document_number,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=items,
        payments=payments,
        status_logs=status_logs,
    )


def get_backoffice_web_order(
    db: Session,
    order_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.WebOrder]:
    query = (
        db.query(models.WebOrder)
        .options(
            joinedload(models.WebOrder.items).joinedload(models.WebOrderItem.product),
            joinedload(models.WebOrder.payments),
            joinedload(models.WebOrder.status_logs),
        )
        .filter(models.WebOrder.id == order_id)
    )
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.WebOrder.tenant_id == effective_tenant_id)
    return query.first()


def list_backoffice_web_orders(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    limit: int = 100,
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    search: Optional[str] = None,
) -> list[models.WebOrder]:
    expire_stale_web_orders(db, tenant_id=tenant_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    query = db.query(models.WebOrder).options(
        joinedload(models.WebOrder.items).joinedload(models.WebOrderItem.product),
        joinedload(models.WebOrder.payments),
        joinedload(models.WebOrder.status_logs),
    )
    if effective_tenant_id is not None:
        query = query.filter(models.WebOrder.tenant_id == effective_tenant_id)
    if status:
        query = query.filter(models.WebOrder.status == status)
    if payment_status:
        query = query.filter(models.WebOrder.payment_status == payment_status)
    term = _clean_field(search)
    if term:
        like_term = f"%{term}%"
        query = query.filter(
            or_(
                models.WebOrder.document_number.ilike(like_term),
                models.WebOrder.customer_name.ilike(like_term),
                models.WebOrder.customer_email.ilike(like_term),
                models.WebOrder.customer_phone.ilike(like_term),
            )
        )
    return query.order_by(models.WebOrder.created_at.desc()).limit(limit).all()


def _transition_web_order_status(
    db: Session,
    order: models.WebOrder,
    *,
    to_status: str,
    note: str | None = None,
    actor_type: str = "system",
    actor_user_id: int | None = None,
) -> models.WebOrder:
    to_status = (to_status or "").strip()
    if not to_status:
        raise ValueError("Debe indicar un estado para la orden web")

    current_status = order.status
    if current_status == to_status:
        return order

    paid_statuses = {"paid", "processing", "ready", "fulfilled"}
    if to_status in paid_statuses and order.payment_status != "approved":
        raise ValueError("La orden debe tener pago aprobado antes de avanzar en fulfillment")
    if to_status == "fulfilled" and order.sale_id is None:
        raise ValueError("La orden debe convertirse en venta antes de marcarse como entregada")
    if current_status in {"cancelled", "refunded"} and to_status not in {"refunded"}:
        raise ValueError("La orden ya está cerrada y no puede volver a un estado operativo")

    order.status = to_status
    if to_status == "pending_payment":
        order.fulfillment_status = "pending"
        order.payment_status = "pending"
        order.paid_at = None
    elif to_status == "payment_failed":
        order.payment_status = "failed"
        order.fulfillment_status = "pending"
    elif to_status == "paid":
        order.payment_status = "approved"
        order.fulfillment_status = "pending"
        order.paid_at = order.paid_at or datetime.utcnow()
    elif to_status == "processing":
        order.payment_status = "approved"
        order.fulfillment_status = "processing"
        order.paid_at = order.paid_at or datetime.utcnow()
    elif to_status == "ready":
        order.payment_status = "approved"
        order.fulfillment_status = "ready"
        order.paid_at = order.paid_at or datetime.utcnow()
    elif to_status == "fulfilled":
        order.payment_status = "approved"
        order.fulfillment_status = "fulfilled"
        order.paid_at = order.paid_at or datetime.utcnow()
    elif to_status == "cancelled":
        order.fulfillment_status = "cancelled"
        order.cancelled_at = order.cancelled_at or datetime.utcnow()
        if order.payment_status == "pending":
            order.payment_status = "cancelled"
    elif to_status == "refunded":
        order.payment_status = "refunded"
        order.fulfillment_status = "cancelled"
        order.cancelled_at = order.cancelled_at or datetime.utcnow()

    order.updated_at = datetime.utcnow()
    _create_web_order_status_log(
        db,
        order,
        from_status=current_status,
        to_status=to_status,
        note=_clean_field(note),
        actor_type=actor_type,
        actor_user_id=actor_user_id,
    )
    return order


def record_web_order_payment(
    db: Session,
    order: models.WebOrder,
    payload: schemas.WebOrderPaymentRecordRequest,
    *,
    actor_user_id: int | None = None,
) -> schemas.WebOrderRead:
    if order.status in {"cancelled", "refunded"}:
        raise ValueError("La orden no admite pagos en su estado actual")

    payment_status = payload.status or "approved"
    amount = float(payload.amount or 0.0)
    if payment_status == "approved":
        if amount <= 0:
            raise ValueError("El monto del pago aprobado debe ser mayor que cero")
    elif amount < 0:
        raise ValueError("El monto del pago no puede ser negativo")

    provider = _clean_field(payload.provider)
    provider_reference = _clean_field(payload.provider_reference)
    method = _clean_field(payload.method)

    existing_payment: models.WebOrderPayment | None = None
    if provider and provider_reference:
        duplicate_query = db.query(models.WebOrderPayment).filter(
            models.WebOrderPayment.provider == provider,
            models.WebOrderPayment.provider_reference == provider_reference,
        )
        if order.tenant_id is None:
            duplicate_query = duplicate_query.filter(models.WebOrderPayment.tenant_id.is_(None))
        else:
            duplicate_query = duplicate_query.filter(models.WebOrderPayment.tenant_id == order.tenant_id)

        duplicate = duplicate_query.first()
        if duplicate:
            if duplicate.web_order_id != order.id:
                raise ValueError(
                    "La referencia del proveedor ya está asociada a otra orden web"
                )
            existing_payment = duplicate

    # Idempotencia: si ya existe exactamente el mismo estado para la misma referencia
    # del proveedor, evitamos reprocesar y generar cambios redundantes.
    if existing_payment is not None:
        existing_method = _clean_field(existing_payment.method)
        same_method = (method or None) in {existing_method, None}
        same_status = (existing_payment.status or "") == payment_status
        same_amount = abs(float(existing_payment.amount or 0.0) - amount) <= 0.0001
        if same_method and same_status and same_amount:
            stored = get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
            if not stored:
                raise ValueError("No se pudo recuperar la orden actualizada")
            return _serialize_web_order(stored)

    if payment_status == "approved":
        approved_total = (
            db.query(func.coalesce(func.sum(models.WebOrderPayment.amount), 0.0))
            .filter(
                models.WebOrderPayment.web_order_id == order.id,
                models.WebOrderPayment.status == "approved",
            )
            .scalar()
        )
        next_total = round(float(approved_total or 0.0) + amount, 2)
        order_total = round(float(order.total or 0.0), 2)
        if next_total - order_total > 0.01:
            raise ValueError("El pago supera el total de la orden")

    if existing_payment is not None:
        payment = existing_payment
        payment.method = method or payment.method
        payment.status = payment_status
        payment.amount = amount
        payment.currency = order.currency
        payment.raw_payload = payload.raw_payload or payment.raw_payload or {}
        if payment_status == "approved":
            payment.approved_at = payment.approved_at or datetime.utcnow()
            payment.failed_at = None
            payment.cancelled_at = None
        elif payment_status == "failed":
            payment.failed_at = datetime.utcnow()
            payment.approved_at = None
            payment.cancelled_at = None
        elif payment_status == "cancelled":
            payment.cancelled_at = datetime.utcnow()
            payment.approved_at = None
            payment.failed_at = None
        db.add(payment)
    else:
        payment = models.WebOrderPayment(
            tenant_id=order.tenant_id,
            web_order_id=order.id,
            provider=provider,
            provider_reference=provider_reference,
            method=method,
            status=payment_status,
            amount=amount,
            currency=order.currency,
            raw_payload=payload.raw_payload or {},
            approved_at=datetime.utcnow() if payment_status == "approved" else None,
            failed_at=datetime.utcnow() if payment_status == "failed" else None,
            cancelled_at=datetime.utcnow() if payment_status == "cancelled" else None,
        )
        db.add(payment)

    transition_note = payload.note
    if payment_status == "approved":
        order.payment_status = "approved"
        order.paid_at = order.paid_at or datetime.utcnow()
        _consume_web_order_coupon_if_needed(db, order=order)
        if order.status in {"pending_payment", "payment_failed"}:
            _transition_web_order_status(
                db,
                order,
                to_status="paid",
                note=transition_note or "Pago aprobado registrado en Comercio Web",
                actor_type="pos_user",
                actor_user_id=actor_user_id,
            )
    elif payment_status == "failed":
        order.payment_status = "failed"
        _transition_web_order_status(
            db,
            order,
            to_status="payment_failed",
            note=transition_note or "Pago fallido registrado en Comercio Web",
            actor_type="pos_user",
            actor_user_id=actor_user_id,
        )
    elif payment_status == "cancelled":
        order.payment_status = "cancelled"
        order.updated_at = datetime.utcnow()
    elif payment_status == "refunded":
        _transition_web_order_status(
            db,
            order,
            to_status="refunded",
            note=transition_note or "Pago reembolsado registrado en Comercio Web",
            actor_type="pos_user",
            actor_user_id=actor_user_id,
        )
    else:
        order.updated_at = datetime.utcnow()

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if provider and provider_reference:
            duplicate_query = db.query(models.WebOrderPayment).filter(
                models.WebOrderPayment.provider == provider,
                models.WebOrderPayment.provider_reference == provider_reference,
            )
            if order.tenant_id is None:
                duplicate_query = duplicate_query.filter(models.WebOrderPayment.tenant_id.is_(None))
            else:
                duplicate_query = duplicate_query.filter(models.WebOrderPayment.tenant_id == order.tenant_id)

            duplicate = duplicate_query.first()
            if duplicate:
                if duplicate.web_order_id != order.id:
                    raise ValueError(
                        "La referencia del proveedor ya está asociada a otra orden web"
                    ) from exc
                stored = get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
                if not stored:
                    raise ValueError("No se pudo recuperar la orden actualizada") from exc
                return _serialize_web_order(stored)
        raise
    stored = get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
    if not stored:
        raise ValueError("No se pudo recuperar la orden actualizada")
    return _serialize_web_order(stored)


def submit_customer_web_order_payment(
    db: Session,
    order: models.WebOrder,
    payload: schemas.WebOrderCustomerPaymentSubmissionRequest,
) -> schemas.WebOrderRead:
    if order.status in {"cancelled", "refunded", "fulfilled"}:
        raise ValueError("La orden no admite nuevos pagos en su estado actual")
    if order.payment_status == "approved":
        raise ValueError("La orden ya tiene un pago aprobado")

    amount = float(payload.amount or 0.0)
    if amount <= 0:
        raise ValueError("El monto reportado debe ser mayor que cero")

    payment = models.WebOrderPayment(
        tenant_id=order.tenant_id,
        web_order_id=order.id,
        provider=_clean_field(payload.provider) or "manual_transfer",
        provider_reference=_clean_field(payload.provider_reference),
        method=_clean_field(payload.method) or "transferencia",
        status="pending",
        amount=amount,
        currency=order.currency,
        raw_payload={},
    )
    db.add(payment)

    if order.status == "payment_failed":
        _transition_web_order_status(
            db,
            order,
            to_status="pending_payment",
            note="Cliente registró un nuevo comprobante de pago",
            actor_type="customer",
        )

    order.updated_at = datetime.utcnow()
    _create_web_order_status_log(
        db,
        order,
        from_status=order.status,
        to_status=order.status,
        note=_clean_field(payload.note)
        or f"Cliente reportó pago manual por {amount:.2f} {order.currency}",
        actor_type="customer",
    )

    db.commit()
    stored = get_web_order(db, order.id, order.account_id, tenant_id=order.tenant_id)
    if not stored:
        raise ValueError("No se pudo recuperar la orden actualizada")
    return _serialize_web_order(stored)


def update_backoffice_web_order_status(
    db: Session,
    order: models.WebOrder,
    payload: schemas.WebOrderStatusUpdateRequest,
    *,
    actor_user_id: int | None = None,
) -> schemas.WebOrderRead:
    _transition_web_order_status(
        db,
        order,
        to_status=payload.status,
        note=payload.note,
        actor_type="pos_user",
        actor_user_id=actor_user_id,
    )
    db.commit()
    stored = get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
    if not stored:
        raise ValueError("No se pudo recuperar la orden actualizada")
    return _serialize_web_order(stored)


def convert_web_order_to_sale(
    db: Session,
    order: models.WebOrder,
    payload: schemas.WebOrderConvertToSaleRequest,
    *,
    actor_user_id: int | None = None,
) -> schemas.WebOrderRead:
    if order.sale_id is not None:
        stored = get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
        if not stored:
            raise ValueError("No se pudo recuperar la orden convertida")
        return _serialize_web_order(stored)
    if order.payment_status != "approved":
        raise ValueError("La orden debe tener pago aprobado antes de convertirse en venta")
    if not order.items:
        raise ValueError("La orden no tiene items para convertir")

    approved_payments = [payment for payment in (order.payments or []) if payment.status == "approved"]
    if not approved_payments:
        raise ValueError("No hay pagos aprobados para convertir la orden en venta")

    paid_amount = round(sum(float(payment.amount or 0.0) for payment in approved_payments), 2)
    total_amount = round(float(order.total or 0.0), 2)
    if paid_amount + 0.01 < total_amount:
        raise ValueError("Los pagos aprobados no cubren el total de la orden")

    sale_notes = [f"Generada desde Comercio Web: {order.document_number}"]
    clean_order_notes = _strip_checkout_context_note_segment(order.notes)
    if clean_order_notes:
        sale_notes.append(clean_order_notes)
    if payload.note:
        sale_notes.append(payload.note)

    sale_payload = schemas.SaleCreate(
        payment_method=_clean_field(approved_payments[0].method) or "online",
        total=total_amount,
        paid_amount=paid_amount,
        change_amount=max(0.0, paid_amount - total_amount),
        cart_discount_value=float(order.discount_amount or 0.0),
        cart_discount_percent=0.0,
        surcharge_amount=float(order.shipping_amount or 0.0),
        surcharge_label="Envío" if float(order.shipping_amount or 0.0) > 0 else None,
        customer_name=order.customer_name,
        customer_id=order.pos_customer_id,
        customer_phone=order.customer_phone,
        customer_email=order.customer_email,
        customer_tax_id=order.customer_tax_id,
        customer_address=order.customer_address,
        notes=" | ".join(part for part in sale_notes if part),
        pos_name="POS Web",
        station_id=None,
        vendor_name="Comercio Web",
        items=[
            schemas.SaleItemCreate(
                product_id=item.product_id,
                quantity=float(item.quantity or 0.0),
                unit_price=float(item.unit_price_snapshot or 0.0),
                unit_price_original=float(item.unit_price_snapshot or 0.0),
                product_sku=item.product_sku_snapshot,
                product_name=item.product_name_snapshot,
                product_barcode=item.product_barcode_snapshot,
                discount=float(item.line_discount_value or 0.0),
                line_discount_value=float(item.line_discount_value or 0.0),
            )
            for item in order.items
        ],
        payments=[
            schemas.SalePaymentCreate(
                method=_clean_field(payment.method) or "online",
                amount=float(payment.amount or 0.0),
            )
            for payment in approved_payments
        ],
    )
    sale = create_sale(
        db,
        sale_payload,
        created_by_user_id=actor_user_id,
        tenant_id=order.tenant_id,
    )

    order.sale_id = sale.id
    order.sale_document_number = sale.document_number
    order.converted_to_sale_at = datetime.utcnow()
    if order.status in {"paid", "pending_payment", "payment_failed"}:
        _transition_web_order_status(
            db,
            order,
            to_status="processing",
            note=f"Orden convertida a venta {sale.document_number}",
            actor_type="pos_user",
            actor_user_id=actor_user_id,
        )
    else:
        order.updated_at = datetime.utcnow()
        _create_web_order_status_log(
            db,
            order,
            from_status=order.status,
            to_status=order.status,
            note=f"Orden vinculada a venta {sale.document_number}",
            actor_type="pos_user",
            actor_user_id=actor_user_id,
        )

    db.commit()
    stored = get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
    if not stored:
        raise ValueError("No se pudo recuperar la orden convertida")
    return _serialize_web_order(stored)


def create_web_order_from_cart(
    db: Session,
    account: models.WebCustomerAccount,
    payload: schemas.WebOrderCreateFromCartRequest,
) -> schemas.WebOrderRead:
    cart = get_active_web_cart(db, account.id, tenant_id=account.tenant_id)
    if not cart or not cart.items:
        raise ValueError("El carrito está vacío")

    line_items_payload: list[dict[str, Any]] = []
    subtotal_base = 0.0
    for cart_item in cart.items:
        product = cart_item.product
        if not product or not product.active or not product.web_published:
            raise ValueError("El carrito contiene productos no disponibles para web")
        unit_price = float(cart_item.unit_price_snapshot or resolve_web_product_sale_price(product) or 0.0)
        quantity = float(cart_item.quantity or 0.0)
        if quantity <= 0:
            continue
        line_total = unit_price * quantity
        subtotal_base += line_total
        line_items_payload.append(
            {
                "product_id": product.id,
                "product_name_snapshot": product.name,
                "product_sku_snapshot": product.sku,
                "product_barcode_snapshot": product.barcode,
                "unit_price_snapshot": unit_price,
                "quantity": quantity,
                "line_discount_value": 0.0,
                "line_total": line_total,
            }
        )
    if subtotal_base <= 0 or not line_items_payload:
        raise ValueError("El carrito no tiene productos válidos para crear la orden")

    coupon_code, coupon_discount_type, coupon_discount_value, coupon_discount_percent, valid_coupon = _resolve_cart_coupon_snapshot(db, cart)
    discount_amount = 0.0
    if coupon_code:
        discount_amount = _compute_coupon_discount_amount(
            subtotal_base,
            discount_type=coupon_discount_type,
            discount_value=coupon_discount_value,
            discount_percent=coupon_discount_percent,
        )
    total_amount = max(0.0, subtotal_base - discount_amount)
    currency = (cart.currency or "COP").strip().upper()

    expire_stale_web_orders(db, tenant_id=account.tenant_id)
    reusable = find_reusable_pending_web_order(
        db,
        tenant_id=account.tenant_id,
        account_id=account.id,
        customer_email=account.email,
        currency=currency,
        subtotal=subtotal_base,
        discount_amount=discount_amount,
        total=total_amount,
        item_signature=build_web_order_item_signature(line_items_payload),
    )
    if reusable:
        if reusable.status == "payment_failed":
            _transition_web_order_status(
                db,
                reusable,
                to_status="pending_payment",
                note="Reintento de pago desde checkout web",
                actor_type="customer",
            )
        reusable.pos_customer_id = account.pos_customer_id
        reusable.customer_name = account.customer.name if account.customer else reusable.customer_name
        reusable.customer_email = account.email or reusable.customer_email
        reusable.customer_phone = account.customer.phone if account.customer else reusable.customer_phone
        reusable.customer_tax_id = account.customer.tax_id if account.customer else reusable.customer_tax_id
        reusable.customer_address = account.customer.address if account.customer else reusable.customer_address
        reusable.notes = _clean_field(payload.notes) or reusable.notes
        reusable.updated_at = datetime.utcnow()
        _create_web_order_status_log(
            db,
            reusable,
            from_status=reusable.status,
            to_status=reusable.status,
            note="Orden reutilizada para reintento de pago desde checkout web",
            actor_type="customer",
        )

        cart.status = "converted"
        cart.coupon_code = None
        cart.coupon_discount_percent = 0.0
        cart.coupon_discount_code_id = None
        cart.converted_at = datetime.utcnow()
        cart.updated_at = datetime.utcnow()
        db.commit()
        existing = get_web_order(db, reusable.id, account.id, tenant_id=account.tenant_id)
        if not existing:
            raise ValueError("No se pudo recuperar la orden web reutilizada")
        return _serialize_web_order(existing)

    number = get_next_web_order_number(db, tenant_id=account.tenant_id)
    document_number = f"OW-{number:06d}"
    order = models.WebOrder(
        tenant_id=account.tenant_id,
        web_order_number=number,
        document_number=document_number,
        account_id=account.id,
        pos_customer_id=account.pos_customer_id,
        status="pending_payment",
        payment_status="pending",
        fulfillment_status="pending",
        customer_name=(account.customer.name if account.customer else None),
        customer_email=account.email,
        customer_phone=(account.customer.phone if account.customer else None),
        customer_tax_id=(account.customer.tax_id if account.customer else None),
        customer_address=(account.customer.address if account.customer else None),
        subtotal=0.0,
        discount_amount=0.0,
        coupon_code=coupon_code,
        coupon_discount_percent=coupon_discount_percent,
        coupon_discount_code_id=(int(valid_coupon.id) if valid_coupon is not None else None),
        coupon_consumed_at=None,
        shipping_amount=0.0,
        total=0.0,
        currency=currency,
        notes=_clean_field(payload.notes),
        submitted_at=datetime.utcnow(),
    )
    db.add(order)
    db.flush()
    for line in line_items_payload:
        db.add(
            models.WebOrderItem(
                tenant_id=order.tenant_id,
                web_order_id=order.id,
                product_id=int(line["product_id"]),
                product_name_snapshot=str(line["product_name_snapshot"]),
                product_sku_snapshot=line.get("product_sku_snapshot"),
                product_barcode_snapshot=line.get("product_barcode_snapshot"),
                unit_price_snapshot=float(line["unit_price_snapshot"]),
                quantity=float(line["quantity"]),
                line_discount_value=float(line["line_discount_value"]),
                line_total=float(line["line_total"]),
            )
        )

    order.subtotal = subtotal_base
    order.discount_amount = discount_amount
    order.total = total_amount
    _create_web_order_status_log(
        db,
        order,
        from_status=None,
        to_status=order.status,
        note="Orden creada desde carrito web",
        actor_type="customer",
    )

    cart.status = "converted"
    cart.coupon_code = None
    cart.coupon_discount_percent = 0.0
    cart.coupon_discount_code_id = None
    cart.converted_at = datetime.utcnow()
    cart.updated_at = datetime.utcnow()

    db.commit()
    created = get_web_order(db, order.id, account.id, tenant_id=account.tenant_id)
    if not created:
        raise ValueError("No se pudo recuperar la orden web creada")
    return _serialize_web_order(created)


# ===================== POS CLOSURES =====================


def _resolve_closure_station_scope(
    db: Session,
    station_id: Optional[str],
    tenant_id: Optional[int] = None,
) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not station_id:
        return [], {}
    _, scoped = get_closure_station_scope(db, station_id, tenant_id=tenant_id)
    ids = [item["station_id"] for item in scoped]
    meta = {
        item["station_id"]: {
            "label": item["station_label"],
            "type": item["station_type"],
        }
        for item in scoped
    }
    return ids, meta


def get_closure_station_scope(
    db: Session,
    station_id: str,
    tenant_id: Optional[int] = None,
) -> tuple[str, list[dict[str, Any]]]:
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    resolved_station_id = _resolve_station_id(db, station_id, tenant_id=effective_tenant_id)
    station = get_pos_station(db, resolved_station_id, tenant_id=effective_tenant_id)
    if not station:
        raise ValueError("Estación inválida o inactiva")

    primary_id = station.id
    if (station.station_type or "desktop") == "tablet" and station.parent_station_id:
        parent = get_pos_station(db, station.parent_station_id, tenant_id=effective_tenant_id)
        if parent and parent.is_active and (parent.station_type or "desktop") == "desktop":
            primary_id = parent.id

    scoped = (
        db.query(models.PosStation.id, models.PosStation.label, models.PosStation.station_type)
        .filter(
            or_(
                models.PosStation.id == primary_id,
                models.PosStation.parent_station_id == primary_id,
            ),
            models.PosStation.is_active.is_(True),
            (
                models.PosStation.tenant_id == effective_tenant_id
                if effective_tenant_id is not None
                else true()
            ),
        )
        .order_by(
            case((models.PosStation.id == primary_id, 0), else_=1),
            models.PosStation.created_at.asc(),
        )
        .all()
    )
    if not scoped:
        primary_station = get_pos_station(db, primary_id, tenant_id=effective_tenant_id)
        label = primary_station.label if primary_station else primary_id
        station_type = (primary_station.station_type if primary_station else "desktop") or "desktop"
        return primary_id, [
            {
                "station_id": primary_id,
                "station_label": label,
                "station_type": station_type,
                "is_primary": True,
            }
        ]

    return primary_id, [
        {
            "station_id": item[0],
            "station_label": item[1] or item[0],
            "station_type": item[2] or "desktop",
            "is_primary": item[0] == primary_id,
        }
        for item in scoped
    ]


def create_pos_closure(
    db: Session,
    closure_in: schemas.PosClosureCreate,
    user: models.PosUser,
) -> models.PosClosure:
    _acquire_pos_closure_lock(db, user)
    snapshot = _build_pos_closure_snapshot(
        db,
        closure_in,
        user,
        apply_admin_fallback=True,
        lock_pending_rows=True,
    )

    closure = models.PosClosure(
        tenant_id=snapshot["tenant_id"],
        pos_name=snapshot["pos_name"],
        pos_identifier=closure_in.pos_identifier,
        station_id=snapshot["station_id"],
        closed_by_user_id=user.id,
        closed_by_user_name=user.name,
        opened_at=snapshot["opened_at"],
        closed_at=snapshot["closed_at"],
        total_amount=snapshot["total_amount"],
        total_cash=snapshot["total_cash"],
        total_card=snapshot["total_card"],
        total_qr=snapshot["total_qr"],
        total_nequi=snapshot["total_nequi"],
        total_daviplata=snapshot["total_daviplata"],
        total_credit=snapshot["total_credit"],
        total_refunds=snapshot["total_refunds"],
        net_amount=snapshot["net_amount"],
        counted_cash=float(closure_in.counted_cash or 0.0),
        difference=snapshot["difference"],
        notes=closure_in.notes,
        sales_count=snapshot["sales_count"],
        change_extra_total=snapshot["change_extra_total"],
        change_refund_total=snapshot["change_refund_total"],
        change_count=snapshot["change_count"],
        total_surcharge=snapshot["total_surcharge"],
        station_breakdown=snapshot["station_breakdown"],
        methods_breakdown=snapshot["methods_breakdown"],
        separated_summary=snapshot["separated_summary"],
        user_breakdown=snapshot["user_breakdown"],
    )
    db.add(closure)
    db.flush()
    if not closure.consecutive:
        closure.consecutive = f"CL-{closure.id:06d}"

    for sale in snapshot["pending_sales"]:
        sale.closure_id = closure.id

    pending_returns = snapshot["pending_returns"]
    if pending_returns:
        (
            db.query(models.SaleReturn)
            .filter(models.SaleReturn.id.in_([ret.id for ret in pending_returns]))
            .update({"closure_id": closure.id}, synchronize_session=False)
        )

    sep_payment_ids = snapshot["sep_payment_ids"]
    if sep_payment_ids:
        (
            db.query(models.SeparatedOrderPayment)
            .filter(models.SeparatedOrderPayment.id.in_(sep_payment_ids))
            .update({"closure_id": closure.id}, synchronize_session=False)
        )

    pending_changes = snapshot["pending_changes"]
    if pending_changes:
        (
            db.query(models.SaleChange)
            .filter(models.SaleChange.id.in_([change.id for change in pending_changes]))
            .update({"closure_id": closure.id}, synchronize_session=False)
        )
        if snapshot["admin_fallback_used"] and snapshot["station_id"]:
            (
                db.query(models.SeparatedOrderPayment)
                .filter(models.SeparatedOrderPayment.id.in_(sep_payment_ids))
                .filter(models.SeparatedOrderPayment.station_id.is_(None))
                .update({"station_id": snapshot["station_id"]}, synchronize_session=False)
            )

    db.commit()
    db.refresh(closure)
    return closure


def preview_pos_closure(
    db: Session,
    closure_in: schemas.PosClosureCreate,
    user: models.PosUser,
) -> dict[str, Any]:
    snapshot = _build_pos_closure_snapshot(
        db,
        closure_in,
        user,
        apply_admin_fallback=False,
    )
    return {
        "pos_name": snapshot["pos_name"],
        "pos_identifier": closure_in.pos_identifier,
        "station_id": snapshot["station_id"],
        "opened_at": snapshot["opened_at"],
        "closed_at": snapshot["closed_at"],
        "total_amount": snapshot["total_amount"],
        "total_cash": snapshot["total_cash"],
        "total_card": snapshot["total_card"],
        "total_qr": snapshot["total_qr"],
        "total_nequi": snapshot["total_nequi"],
        "total_daviplata": snapshot["total_daviplata"],
        "total_credit": snapshot["total_credit"],
        "total_refunds": snapshot["total_refunds"],
        "net_amount": snapshot["net_amount"],
        "counted_cash": float(closure_in.counted_cash or 0.0),
        "difference": snapshot["difference"],
        "change_extra_total": snapshot["change_extra_total"],
        "change_refund_total": snapshot["change_refund_total"],
        "change_count": snapshot["change_count"],
        "notes": closure_in.notes,
        "total_surcharge": snapshot["total_surcharge"],
        "sales_count": snapshot["sales_count"],
        "station_breakdown": snapshot["station_breakdown"],
        "methods_breakdown": snapshot["methods_breakdown"],
        "separated_summary": snapshot["separated_summary"],
        "user_breakdown": snapshot["user_breakdown"],
    }


def _build_pos_closure_snapshot(
    db: Session,
    closure_in: schemas.PosClosureCreate,
    user: models.PosUser,
    *,
    apply_admin_fallback: bool,
    lock_pending_rows: bool = False,
) -> dict[str, Any]:
    effective_tenant_id = resolve_user_tenant_id(db, user)
    pos_name = closure_in.pos_name.strip() if closure_in.pos_name else None
    station_id = _resolve_station_id(db, closure_in.station_id, tenant_id=effective_tenant_id)
    is_pos_web = _is_pos_web_name(pos_name)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede cerrar con estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(db, pos_name, tenant_id=effective_tenant_id)
    if not station_id and not is_pos_web and _tenant_requires_station(
        db, tenant_id=effective_tenant_id
    ):
        raise ValueError("Debe seleccionar una estación para cerrar caja")
    scoped_station_ids, scoped_station_meta = _resolve_closure_station_scope(
        db, station_id, tenant_id=effective_tenant_id
    )

    pending_sales_query = db.query(models.Sale).filter(
        models.Sale.tenant_id == effective_tenant_id,
        models.Sale.closure_id.is_(None),
        or_(models.Sale.status.is_(None), models.Sale.status != "voided"),
    )
    if scoped_station_ids:
        pending_sales_query = pending_sales_query.filter(
            models.Sale.station_id.in_(scoped_station_ids)
        )
    elif pos_name:
        pending_sales_query = _filter_pos_name(
            pending_sales_query,
            models.Sale.pos_name,
            pos_name,
        )

    if lock_pending_rows:
        pending_sales_query = pending_sales_query.with_for_update()
    pending_sales = pending_sales_query.order_by(models.Sale.created_at.asc()).all()
    admin_fallback_used = False

    if (
        apply_admin_fallback
        and not pending_sales
        and station_id
        and user.role == "Administrador"
    ):
        fallback_query = db.query(models.Sale).filter(
            models.Sale.tenant_id == effective_tenant_id,
            models.Sale.closure_id.is_(None),
            models.Sale.station_id.is_(None),
            or_(models.Sale.status.is_(None), models.Sale.status != "voided"),
        )
        if pos_name:
            fallback_query = fallback_query.filter(
                models.Sale.pos_name == pos_name
            )
        pending_sales = fallback_query.order_by(models.Sale.created_at.asc()).all()
        if pending_sales:
            for sale in pending_sales:
                sale.station_id = station_id
            db.flush()
            admin_fallback_used = True

    closed_at = closure_in.closed_at or datetime.utcnow()
    range_end = closed_at

    pending_returns_query = (
        db.query(models.SaleReturn)
        .join(models.Sale, models.SaleReturn.sale_id == models.Sale.id)
        .filter(
            models.SaleReturn.tenant_id == effective_tenant_id,
            models.SaleReturn.closure_id.is_(None),
            models.SaleReturn.status == "confirmed",
            models.SaleReturn.adjustment_reference.is_(None),
        )
    )
    if scoped_station_ids:
        pending_returns_query = pending_returns_query.filter(
            models.Sale.station_id.in_(scoped_station_ids)
        )
    elif pos_name:
        pending_returns_query = _filter_pos_name(
            pending_returns_query,
            models.Sale.pos_name,
            pos_name,
        )

    if lock_pending_rows:
        pending_returns_query = pending_returns_query.with_for_update()
    pending_returns = pending_returns_query.order_by(models.SaleReturn.created_at.asc()).all()

    pending_changes_base = (
        db.query(models.SaleChange)
        .filter(
            models.SaleChange.tenant_id == effective_tenant_id,
            models.SaleChange.closure_id.is_(None),
            models.SaleChange.status == "confirmed",
        )
    )
    if scoped_station_ids:
        pending_changes_base = pending_changes_base.filter(
            models.SaleChange.station_id.in_(scoped_station_ids)
        )
    elif pos_name:
        pending_changes_base = _filter_pos_name(
            pending_changes_base,
            models.SaleChange.pos_name,
            pos_name,
        )

    if lock_pending_rows:
        pending_changes_base = pending_changes_base.with_for_update()
    pending_changes_all = pending_changes_base.order_by(models.SaleChange.created_at.asc()).all()

    sep_paid_at = (
        db.query(func.min(models.SeparatedOrderPayment.paid_at))
        .join(models.SeparatedOrder, models.SeparatedOrderPayment.separated_order_id == models.SeparatedOrder.id)
        .join(models.Sale, models.SeparatedOrder.sale_id == models.Sale.id)
        .filter(
            models.SeparatedOrderPayment.tenant_id == effective_tenant_id,
            models.SeparatedOrderPayment.closure_id.is_(None),
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            ),
        )
    )
    if scoped_station_ids:
        sep_paid_at = sep_paid_at.filter(
            or_(
                models.SeparatedOrderPayment.station_id.in_(scoped_station_ids),
                models.Sale.station_id.in_(scoped_station_ids),
            )
        )
    elif pos_name:
        sep_paid_at = _filter_pos_name(
            sep_paid_at,
            models.Sale.pos_name,
            pos_name,
        )
    sep_paid_at = sep_paid_at.scalar()

    date_candidates = []
    if pending_sales:
        date_candidates.append(pending_sales[0].created_at)
    if pending_returns:
        date_candidates.append(pending_returns[0].created_at)
    if pending_changes_all:
        date_candidates.append(pending_changes_all[0].created_at)
    if sep_paid_at:
        date_candidates.append(sep_paid_at)

    if not date_candidates:
        raise ValueError("No hay movimientos pendientes por cerrar")

    range_start = min(date_candidates)

    if pending_returns:
        pending_returns = [
            ret
            for ret in pending_returns
            if ret.created_at <= range_end
        ]

    pending_changes = [
        change
        for change in pending_changes_all
        if change.created_at <= range_end
    ]

    _closure_logger.info(
        "POS closure debug -> aggregated range_start=%s, range_end=%s",
        range_start,
        range_end,
    )
    _closure_logger.info(
        "POS closure debug -> ventas en rango: %s",
        len(pending_sales),
    )

    sale_ids = [sale.id for sale in pending_sales]
    payment_adjustments, total_delta_by_sale = _collect_sale_adjustments(
        db,
        sale_ids,
        range_end=range_end,
        tenant_id=effective_tenant_id,
    )
    total_amount = sum(
        float(sale.total or 0.0) + float(total_delta_by_sale.get(sale.id, 0.0))
        for sale in pending_sales
    )
    total_refunds = sum(float(ret.total_refund or 0.0) for ret in pending_returns)
    sales_count = len(pending_sales)

    payment_totals = {
        "cash": 0.0,
        "card": 0.0,
        "qr": 0.0,
        "nequi": 0.0,
        "daviplata": 0.0,
        "credit": 0.0,
    }
    method_map = {
        "cash": "cash",
        "card": "card",
        "qr": "qr",
        "nequi": "nequi",
        "daviplata": "daviplata",
        "credit": "credit",
    }
    method_labels = {
        "cash": "Efectivo",
        "card": "Tarjeta Datáfono",
        "qr": "Transferencias / QR",
        "nequi": "Nequi",
        "daviplata": "Daviplata",
        "credit": "Crédito / separado",
    }
    standard_method_keys = set(method_map.values())
    methods_breakdown_map: dict[str, dict[str, Any]] = {}
    user_totals: dict[str, float] = defaultdict(float)

    def _method_key(raw: Optional[str]) -> str:
        normalized = re.sub(r"\s+", " ", (raw or "").strip().lower())
        return normalized or "other"

    def _method_label(raw: Optional[str], key: str) -> str:
        if key in method_labels:
            return method_labels[key]
        value = (raw or "").strip()
        return value if value else "Otro método"

    def _add_method_amount(raw_method: Optional[str], amount: float, *, refund: bool = False) -> None:
        if abs(float(amount or 0.0)) <= 0.0001:
            return
        key = _method_key(raw_method)
        entry = methods_breakdown_map.get(key)
        if entry is None:
            entry = {
                "key": key,
                "label": _method_label(raw_method, key),
                "gross": 0.0,
                "refunds": 0.0,
                "net": 0.0,
                "is_standard": key in standard_method_keys,
            }
            methods_breakdown_map[key] = entry
        if refund:
            entry["refunds"] += float(abs(amount))
        else:
            entry["gross"] += float(amount)
        entry["net"] = float(entry["gross"] or 0.0) - float(entry["refunds"] or 0.0)
    station_totals: dict[str, dict[str, Any]] = {}

    def _station_bucket(station_ref: Optional[str]) -> dict[str, Any]:
        key = station_ref or "__unassigned__"
        if key not in station_totals:
            meta = scoped_station_meta.get(station_ref or "")
            station_totals[key] = {
                "station_id": station_ref,
                "station_label": (
                    meta.get("label")
                    if meta
                    else ("Sin estación" if not station_ref else station_ref)
                ),
                "station_type": meta.get("type") if meta else None,
                "sales_count": 0,
                "total_amount": 0.0,
                "total_refunds": 0.0,
                "total_cash": 0.0,
                "total_card": 0.0,
                "total_qr": 0.0,
                "total_nequi": 0.0,
                "total_daviplata": 0.0,
                "total_credit": 0.0,
                "change_extra_total": 0.0,
                "change_refund_total": 0.0,
                "net_amount": 0.0,
            }
        return station_totals[key]
    for sale in pending_sales:
        station_bucket = _station_bucket(sale.station_id)
        station_bucket["sales_count"] += 1
        station_bucket["total_amount"] += float(sale.total or 0.0) + float(
            total_delta_by_sale.get(sale.id, 0.0)
        )
        adjustment = payment_adjustments.get(sale.id)
        adjusted_payments = (
            _parse_adjustment_payments(adjustment.payload) if adjustment else []
        )
        payment_entries: list[tuple[str, float]] = (
            adjusted_payments
            if adjusted_payments
            else [
                (payment.method, float(payment.amount or 0.0))
                for payment in (sale.payments or [])
            ]
        )
        if not payment_entries:
            fallback_method = sale.main_payment_method or sale.payment_method
            fallback_amount = float(sale.paid_amount or sale.total or 0.0)
            if fallback_method and fallback_amount > 0:
                payment_entries = [(fallback_method, fallback_amount)]
        for method, amount in payment_entries:
            key = method_map.get((method or "").lower())
            amount_float = float(amount or 0.0)
            if key:
                payment_totals[key] += amount_float
                station_bucket[f"total_{key}"] += amount_float
            _add_method_amount(method, amount_float, refund=False)

        # El cambio de la venta (vuelto) siempre sale de caja en efectivo.
        # Se descuenta del total de efectivo para no inflar cifras en cierre.
        sale_change_amount = max(float(sale.change_amount or 0.0), 0.0)
        if sale_change_amount > 0:
            payment_totals["cash"] -= sale_change_amount
            station_bucket["total_cash"] -= sale_change_amount
            _add_method_amount("cash", sale_change_amount, refund=True)

        vendor_name = (sale.vendor_name or "").strip()
        if vendor_name and not bool(sale.is_separated):
            user_totals[vendor_name] += float(sale.total or 0.0) + float(
                total_delta_by_sale.get(sale.id, 0.0)
            )

    if pending_returns:
        for ret in pending_returns:
            station_bucket = _station_bucket(ret.sale.station_id if ret.sale else None)
            station_bucket["total_refunds"] += float(ret.total_refund or 0.0)
        return_ids = [ret.id for ret in pending_returns]
        return_rows = (
            db.query(
                models.Sale.station_id,
                models.SaleReturnPayment.method,
                func.sum(models.SaleReturnPayment.amount),
            )
            .join(models.SaleReturn, models.SaleReturnPayment.return_id == models.SaleReturn.id)
            .join(models.Sale, models.SaleReturn.sale_id == models.Sale.id)
            .filter(models.SaleReturnPayment.return_id.in_(return_ids))
            .group_by(models.Sale.station_id, models.SaleReturnPayment.method)
            .all()
        )
        for ret_station_id, method, amount in return_rows:
            key = method_map.get((method or "").lower())
            amount_float = float(amount or 0.0)
            if key:
                payment_totals[key] -= amount_float
                station_bucket = _station_bucket(ret_station_id)
                station_bucket[f"total_{key}"] -= amount_float
            _add_method_amount(method, amount_float, refund=True)

    sep_payment_filter = (
        db.query(
            func.coalesce(
                models.SeparatedOrderPayment.station_id,
                models.Sale.station_id,
            ).label("resolved_station_id"),
            models.SeparatedOrderPayment.method,
            func.sum(models.SeparatedOrderPayment.amount),
        )
        .join(models.SeparatedOrder, models.SeparatedOrderPayment.separated_order_id == models.SeparatedOrder.id)
        .join(models.Sale, models.SeparatedOrder.sale_id == models.Sale.id)
        .filter(
            models.SeparatedOrderPayment.tenant_id == effective_tenant_id,
            models.SeparatedOrderPayment.closure_id.is_(None),
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            ),
        )
    )
    sep_ids_query = (
        db.query(models.SeparatedOrderPayment.id)
        .join(models.SeparatedOrder, models.SeparatedOrderPayment.separated_order_id == models.SeparatedOrder.id)
        .join(models.Sale, models.SeparatedOrder.sale_id == models.Sale.id)
        .filter(
            models.SeparatedOrderPayment.tenant_id == effective_tenant_id,
            models.SeparatedOrderPayment.closure_id.is_(None),
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            ),
        )
    )
    if scoped_station_ids:
        sep_payment_filter = sep_payment_filter.filter(
            or_(
                models.SeparatedOrderPayment.station_id.in_(scoped_station_ids),
                models.Sale.station_id.in_(scoped_station_ids),
            )
        )
        sep_ids_query = sep_ids_query.filter(
            or_(
                models.SeparatedOrderPayment.station_id.in_(scoped_station_ids),
                models.Sale.station_id.in_(scoped_station_ids),
            )
        )
    elif pos_name:
        sep_payment_filter = _filter_pos_name(
            sep_payment_filter,
            models.Sale.pos_name,
            pos_name,
        )
        sep_ids_query = _filter_pos_name(
            sep_ids_query,
            models.Sale.pos_name,
            pos_name,
        )

    if lock_pending_rows:
        sep_ids_query = sep_ids_query.with_for_update()

    sep_rows = sep_payment_filter.group_by(
        "resolved_station_id",
        models.SeparatedOrderPayment.method,
    ).all()
    sep_payment_ids = [row[0] for row in sep_ids_query.all()]

    for sep_station_id, method, amount in sep_rows:
        key = method_map.get((method or "").lower())
        amount_float = float(amount or 0.0)
        if key:
            payment_totals[key] += amount_float
            station_bucket = _station_bucket(sep_station_id)
            station_bucket[f"total_{key}"] += amount_float
        _add_method_amount(method, amount_float, refund=False)

    pending_changes = [
        change
        for change in pending_changes
        if change.created_at >= range_start
    ]
    change_extra_total = sum(float(change.extra_payment or 0.0) for change in pending_changes)
    change_refund_total = sum(float(change.refund_due or 0.0) for change in pending_changes)
    change_count = len(pending_changes)

    for change in pending_changes:
        station_bucket = _station_bucket(change.station_id)
        station_bucket["change_extra_total"] += float(change.extra_payment or 0.0)
        station_bucket["change_refund_total"] += float(change.refund_due or 0.0)
        for payment in change.payments:
            key = method_map.get((payment.method or "").lower())
            payment_amount = float(payment.amount or 0.0)
            if key:
                payment_totals[key] += payment_amount
                station_bucket[f"total_{key}"] += payment_amount
            _add_method_amount(payment.method, payment_amount, refund=False)
        if float(change.refund_due or 0.0) > 0:
            payment_totals["cash"] -= float(change.refund_due or 0.0)
            station_bucket["total_cash"] -= float(change.refund_due or 0.0)
            _add_method_amount("cash", float(change.refund_due or 0.0), refund=True)

    net_amount = total_amount - total_refunds + change_extra_total - change_refund_total

    separated_orders = (
        db.query(models.SeparatedOrder)
        .filter(
            models.SeparatedOrder.tenant_id == effective_tenant_id,
            models.SeparatedOrder.sale_id.in_(sale_ids) if sale_ids else false(),
        )
        .all()
    )
    separated_summary: Optional[dict[str, Any]] = None
    if separated_orders:
        sale_map = {sale.id: sale for sale in pending_sales}
        sep_by_sale = {row.sale_id: row for row in separated_orders}
        tickets = 0
        reserved_total = 0.0
        pending_total = 0.0
        payments_total = 0.0
        for sale in pending_sales:
            separated = sep_by_sale.get(sale.id)
            if not separated and not bool(sale.is_separated):
                continue
            tickets += 1
            reserved_total += float(
                (separated.total_amount if separated else None) or sale.total or 0.0
            )
            pending_total += max(
                float((separated.balance if separated else None) or sale.balance or 0.0),
                0.0,
            )
            payments_total += max(
                float(
                    (sale.initial_payment_amount or 0.0)
                    or ((separated.initial_payment if separated else None) or 0.0)
                ),
                0.0,
            )
            vendor_name = (sale.vendor_name or "").strip()
            if vendor_name:
                user_totals[vendor_name] += max(
                    float(
                        (sale.initial_payment_amount or 0.0)
                        or ((separated.initial_payment if separated else None) or 0.0)
                    ),
                    0.0,
                )
        sep_user_rows = (
            db.query(
                models.Sale.vendor_name,
                func.sum(models.SeparatedOrderPayment.amount),
            )
            .join(
                models.SeparatedOrder,
                models.SeparatedOrderPayment.separated_order_id == models.SeparatedOrder.id,
            )
            .join(models.Sale, models.SeparatedOrder.sale_id == models.Sale.id)
            .filter(
                models.SeparatedOrderPayment.tenant_id == effective_tenant_id,
                models.SeparatedOrderPayment.closure_id.is_(None),
                or_(
                    models.SeparatedOrderPayment.status.is_(None),
                    models.SeparatedOrderPayment.status != "voided",
                ),
                models.Sale.id.in_(sale_ids) if sale_ids else false(),
            )
            .group_by(models.Sale.vendor_name)
            .all()
        )
        for vendor_name_raw, vendor_amount in sep_user_rows:
            vendor_name = (vendor_name_raw or "").strip()
            if not vendor_name:
                continue
            user_totals[vendor_name] += float(vendor_amount or 0.0)
        payments_total += sum(float(amount or 0.0) for _, _, amount in sep_rows)
        separated_summary = {
            "tickets": tickets,
            "payments_total": round(payments_total, 2),
            "reserved_total": round(reserved_total, 2),
            "pending_total": round(max(pending_total, 0.0), 2),
            "day_collected_total": round(max(net_amount - max(pending_total, 0.0), 0.0), 2),
            "day_with_pending_total": round(net_amount, 2),
        }

    station_breakdown: list[dict[str, Any]] = []
    for row in station_totals.values():
        row["net_amount"] = (
            float(row["total_amount"] or 0.0)
            - float(row["total_refunds"] or 0.0)
            + float(row["change_extra_total"] or 0.0)
            - float(row["change_refund_total"] or 0.0)
        )
        has_movement = (
            int(row["sales_count"] or 0) > 0
            or abs(float(row["total_amount"] or 0.0)) > 0.009
            or abs(float(row["total_refunds"] or 0.0)) > 0.009
            or abs(float(row["change_extra_total"] or 0.0)) > 0.009
            or abs(float(row["change_refund_total"] or 0.0)) > 0.009
            or abs(float(row["total_cash"] or 0.0)) > 0.009
            or abs(float(row["total_card"] or 0.0)) > 0.009
            or abs(float(row["total_qr"] or 0.0)) > 0.009
            or abs(float(row["total_nequi"] or 0.0)) > 0.009
            or abs(float(row["total_daviplata"] or 0.0)) > 0.009
            or abs(float(row["total_credit"] or 0.0)) > 0.009
        )
        if has_movement:
            station_breakdown.append(row)

    if station_breakdown:
        station_breakdown.sort(
            key=lambda value: (
                0 if station_id and value.get("station_id") == station_id else 1,
                str(value.get("station_label") or ""),
            )
        )

    difference = float(closure_in.counted_cash or 0.0) - payment_totals["cash"]
    total_surcharge = sum(float(sale.surcharge_amount or 0.0) for sale in pending_sales)
    standard_rows = []
    for std_key, std_label in method_labels.items():
        std_net = round(float(payment_totals.get(std_key, 0.0) or 0.0), 2)
        if abs(std_net) <= 0.0001:
            continue
        standard_rows.append(
            {
                "key": std_key,
                "label": std_label,
                "gross": std_net,
                "refunds": 0.0,
                "net": std_net,
                "is_standard": True,
            }
        )

    extra_rows = [
        {
            "key": key,
            "label": str(value.get("label") or key),
            "gross": round(float(value.get("net") or 0.0), 2),
            "refunds": 0.0,
            "net": round(float(value.get("net") or 0.0), 2),
            "is_standard": False,
        }
        for key, value in methods_breakdown_map.items()
        if key not in standard_method_keys
        and abs(float(value.get("net") or 0.0)) > 0.0001
    ]
    methods_breakdown = sorted(
        standard_rows + extra_rows,
        key=lambda row: (0 if row["is_standard"] else 1, row["label"].lower()),
    )
    user_breakdown = sorted(
        [
            {"name": name, "total": round(float(total), 2)}
            for name, total in user_totals.items()
            if name and abs(float(total or 0.0)) > 0.0001
        ],
        key=lambda row: row["total"],
        reverse=True,
    )
    return {
        "tenant_id": effective_tenant_id,
        "pos_name": pos_name,
        "station_id": station_id,
        "opened_at": range_start,
        "closed_at": closed_at,
        "total_amount": total_amount,
        "total_cash": payment_totals["cash"],
        "total_card": payment_totals["card"],
        "total_qr": payment_totals["qr"],
        "total_nequi": payment_totals["nequi"],
        "total_daviplata": payment_totals["daviplata"],
        "total_credit": payment_totals["credit"],
        "total_refunds": total_refunds,
        "net_amount": net_amount,
        "difference": difference,
        "sales_count": sales_count,
        "change_extra_total": change_extra_total,
        "change_refund_total": change_refund_total,
        "change_count": change_count,
        "total_surcharge": total_surcharge,
        "station_breakdown": station_breakdown,
        "methods_breakdown": methods_breakdown,
        "separated_summary": separated_summary,
        "user_breakdown": user_breakdown,
        "pending_sales": pending_sales,
        "pending_returns": pending_returns,
        "pending_changes": pending_changes,
        "sep_payment_ids": sep_payment_ids,
        "admin_fallback_used": admin_fallback_used,
    }


def _acquire_pos_closure_lock(db: Session, user: models.PosUser) -> None:
    tenant_id = resolve_user_tenant_id(db, user)
    if tenant_id is not None:
        (
            db.query(models.Tenant.id)
            .filter(models.Tenant.id == tenant_id)
            .with_for_update()
            .first()
        )


def get_pos_closure(
    db: Session,
    closure_id: int,
    tenant_id: Optional[int] = None,
) -> Optional[models.PosClosure]:
    query = db.query(models.PosClosure).filter(models.PosClosure.id == closure_id)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosClosure.tenant_id == effective_tenant_id)
    return query.first()


def list_pos_closures(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    pos_name: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
) -> List[models.PosClosure]:
    query = db.query(models.PosClosure)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosClosure.tenant_id == effective_tenant_id)
    if pos_name:
        query = query.filter(models.PosClosure.pos_name == pos_name)
    if date_from:
        query = query.filter(models.PosClosure.closed_at >= date_from)
    if date_to:
        query = query.filter(models.PosClosure.closed_at <= date_to)
    return (
        query.order_by(models.PosClosure.closed_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def delete_pos_closure(db: Session, closure: models.PosClosure):
    for sale in closure.sales:
        sale.closure_id = None
    (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.closure_id == closure.id)
        .update({"closure_id": None}, synchronize_session=False)
    )
    (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.closure_id == closure.id)
        .update({"closure_id": None}, synchronize_session=False)
    )
    db.delete(closure)
    db.commit()
