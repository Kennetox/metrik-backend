from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set
from io import BytesIO
from fastapi.responses import StreamingResponse, PlainTextResponse
import io
import csv
import re
import unicodedata
import pandas as pd
from pydantic import BaseModel

import schemas, crud, models
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/products",
    tags=["products"]
)


class ExportProductsRequest(BaseModel):
    scope: str = "all"
    search: Optional[str] = None
    show_only_active: bool = False
    show_only_inactive: bool = False
    group: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    price_min: Optional[str] = None
    price_max: Optional[str] = None
    columns: List[str] = []
    file_name: Optional[str] = None


def _parse_price(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _normalize_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _row_to_normalized_map(row: Any) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in row.items():
        normalized[_normalize_header(key)] = value
    return normalized


def _coalesce_row_value(row_map: Dict[str, Any], *aliases: str) -> Any:
    for alias in aliases:
        normalized_alias = _normalize_header(alias)
        if normalized_alias in row_map:
            return row_map[normalized_alias]
    return None


def _parse_floatish(value: Any, default: float = 0.0) -> float:
    if value is None or pd.isna(value):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return default
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


def _parse_intish(value: Any, default: int = 0) -> int:
    return int(round(_parse_floatish(value, float(default))))


def _parse_boolish(value: Any, default: bool = False) -> bool:
    if value is None or pd.isna(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    if text in {"1", "true", "t", "si", "yes", "y", "x"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return bool(text)


_DUPLICATE_STOPWORDS: Set[str] = {
    "de", "del", "la", "el", "los", "las", "para", "con", "sin", "por", "en", "y", "a"
}


def _normalize_text(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize_text(value: Optional[str]) -> List[str]:
    normalized = _normalize_text(value)
    if not normalized:
        return []
    return [
        token
        for token in normalized.split(" ")
        if token and token not in _DUPLICATE_STOPWORDS
    ]


def _normalize_identifier(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def _spec_tokens(tokens: List[str]) -> Set[str]:
    return {token for token in tokens if any(ch.isdigit() for ch in token)}


def _reference_tokens(tokens: List[str]) -> Set[str]:
    return {
        token
        for token in tokens
        if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)
    }


def _jaccard_similarity(left_tokens: List[str], right_tokens: List[str]) -> float:
    left = set(left_tokens)
    right = set(right_tokens)
    if not left or not right:
        return 0.0
    intersection = len(left.intersection(right))
    union = len(left.union(right))
    if union == 0:
        return 0.0
    return intersection / union


def _sequence_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _shared_token_preview(left_tokens: List[str], right_tokens: List[str]) -> List[str]:
    shared = sorted(set(left_tokens).intersection(right_tokens))
    return shared[:4]


def _search_terms_from_payload(payload: schemas.ProductDuplicateCandidatesRequest) -> List[str]:
    raw_values = [
        payload.name,
        payload.sku,
        payload.barcode,
        payload.group_name,
        payload.brand,
        payload.supplier,
    ]
    terms: List[str] = []
    seen: Set[str] = set()
    for raw in raw_values:
        normalized = _normalize_text(raw)
        if normalized and len(normalized) >= 2 and normalized not in seen:
            seen.add(normalized)
            terms.append(normalized)
        for token in _tokenize_text(raw):
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            terms.append(token)
        identifier = _normalize_identifier(raw)
        if len(identifier) >= 2 and identifier not in seen:
            seen.add(identifier)
            terms.append(identifier)
    return terms


def _build_duplicate_candidate(
    payload: schemas.ProductDuplicateCandidatesRequest,
    candidate: models.Product,
) -> Optional[schemas.ProductDuplicateCandidate]:
    reasons: List[str] = []
    score = 0.0

    input_name_norm = _normalize_text(payload.name)
    candidate_name_norm = _normalize_text(candidate.name)
    input_name_id = _normalize_identifier(payload.name)
    candidate_name_id = _normalize_identifier(candidate.name)
    input_name_tokens = _tokenize_text(payload.name)
    candidate_name_tokens = _tokenize_text(candidate.name)
    input_spec_tokens = _spec_tokens(input_name_tokens)
    candidate_spec_tokens = _spec_tokens(candidate_name_tokens)
    input_reference_tokens = _reference_tokens(input_name_tokens)
    candidate_reference_tokens = _reference_tokens(candidate_name_tokens)
    shared_reference_tokens = input_reference_tokens.intersection(candidate_reference_tokens)

    if input_name_norm and candidate_name_norm and input_name_norm == candidate_name_norm:
        score += 0.58
        reasons.append("Nombre prácticamente idéntico.")
    elif input_name_norm and candidate_name_norm:
        name_ratio = _sequence_similarity(input_name_norm, candidate_name_norm)
        token_similarity = _jaccard_similarity(input_name_tokens, candidate_name_tokens)
        overlap_score = max(name_ratio, token_similarity)
        if input_name_norm in candidate_name_norm or candidate_name_norm in input_name_norm:
            score += 0.22
            reasons.append("Nombre contenido casi completo (variación menor).")
        if overlap_score > 0:
            score += 0.38 * overlap_score
            if overlap_score >= 0.85:
                reasons.append("Nombre casi idéntico tras normalización.")
            elif overlap_score >= 0.65:
                reasons.append("Alta similitud textual en el nombre.")
            elif overlap_score >= 0.45:
                reasons.append("Similitud textual parcial en el nombre.")

    if input_spec_tokens and candidate_spec_tokens:
        shared_specs = input_spec_tokens.intersection(candidate_spec_tokens)
        spec_ratio = len(shared_specs) / max(len(input_spec_tokens), len(candidate_spec_tokens))
        if spec_ratio > 0:
            score += 0.24 * spec_ratio
            reasons.append("Coinciden especificaciones numéricas relevantes.")
        else:
            score -= 0.22
            reasons.append("Las especificaciones numéricas difieren.")

    if shared_reference_tokens:
        score += 0.22 + (0.02 * max(0, len(shared_reference_tokens) - 1))
        reasons.append("Coincide la referencia alfanumérica.")

    payload_brand = _normalize_text(payload.brand)
    payload_group = _normalize_text(payload.group_name)
    payload_supplier = _normalize_text(payload.supplier)
    candidate_brand = _normalize_text(candidate.brand)
    candidate_group = _normalize_text(candidate.group_name)
    candidate_supplier = _normalize_text(candidate.supplier)

    if payload_brand and payload_brand == candidate_brand:
        score += 0.16
        reasons.append("Misma marca.")
    if payload_group and payload_group == candidate_group:
        score += 0.2
        reasons.append("Mismo grupo.")
    if payload_supplier and payload_supplier == candidate_supplier:
        score += 0.09
        reasons.append("Mismo proveedor.")
    if payload_group and payload_brand and payload_group == candidate_group and payload_brand == candidate_brand:
        score += 0.08
        reasons.append("Grupo y marca coinciden simultáneamente.")
    if payload_group and payload_supplier and payload_group == candidate_group and payload_supplier == candidate_supplier:
        score += 0.05
        reasons.append("Grupo y proveedor coinciden simultáneamente.")

    payload_sku = (payload.sku or "").strip()
    payload_barcode = (payload.barcode or "").strip()
    candidate_sku = (candidate.sku or "").strip()
    candidate_barcode = (candidate.barcode or "").strip()
    payload_sku_id = _normalize_identifier(payload_sku)
    payload_barcode_id = _normalize_identifier(payload_barcode)
    candidate_sku_id = _normalize_identifier(candidate_sku)
    candidate_barcode_id = _normalize_identifier(candidate_barcode)

    if payload_sku and candidate_sku and payload_sku.strip().lower() == candidate_sku.strip().lower():
        score = max(score, 1.0)
        reasons.append("SKU idéntico.")
    elif payload_sku_id and candidate_sku_id:
        if payload_sku_id == candidate_sku_id:
            score = max(score, 0.98)
            reasons.append("SKU idéntico tras normalización.")
        elif payload_sku_id in candidate_sku_id or candidate_sku_id in payload_sku_id:
            score += 0.28
            reasons.append("SKU muy parecido.")
    if payload_barcode and candidate_barcode and payload_barcode.strip() == candidate_barcode.strip():
        score = max(score, 1.0)
        reasons.append("Código de barras idéntico.")
    elif payload_barcode_id and candidate_barcode_id:
        if payload_barcode_id == candidate_barcode_id:
            score = max(score, 0.99)
            reasons.append("Código de barras idéntico tras normalización.")
        elif payload_barcode_id in candidate_barcode_id or candidate_barcode_id in payload_barcode_id:
            score += 0.3
            reasons.append("Código de barras muy parecido.")

    if input_name_id and candidate_name_id:
        if input_name_id == candidate_name_id and input_name_norm != candidate_name_norm:
            score += 0.08
            reasons.append("Coinciden códigos en el nombre.")
        elif input_name_id in candidate_name_id or candidate_name_id in input_name_id:
            score += 0.14
            reasons.append("Coincidencia parcial de códigos en el nombre.")

    if (
        payload_group
        and payload_brand
        and payload_supplier
        and payload_group == candidate_group
        and payload_brand == candidate_brand
        and payload_supplier == candidate_supplier
        and (input_name_norm and candidate_name_norm)
    ):
        score += 0.08
        reasons.append("Coincidencia completa por catálogo.")

    score = max(0.0, min(1.0, score))
    min_score = 0.6
    if input_name_norm and candidate_name_norm and input_name_norm == candidate_name_norm:
        min_score = 0.4
    if shared_reference_tokens:
        min_score = min(min_score, 0.5)
    if score < min_score:
        return None

    risk_level: str
    if score >= 0.92:
        risk_level = "alto"
    elif score >= 0.76:
        risk_level = "medio"
    else:
        risk_level = "bajo"

    return schemas.ProductDuplicateCandidate(
        product_id=candidate.id,
        name=candidate.name,
        sku=candidate.sku,
        barcode=candidate.barcode,
        price=float(candidate.price or 0.0),
        group_name=candidate.group_name,
        brand=candidate.brand,
        supplier=candidate.supplier,
        similarity_score=round(score, 4),
        risk_level=risk_level,
        match_reasons=reasons[:4],
    )


def _build_family_duplicate_candidate(
    payload: schemas.ProductDuplicateCandidatesRequest,
    candidate: models.Product,
) -> Optional[schemas.ProductDuplicateCandidate]:
    reasons: List[str] = []
    score = 0.0

    input_name_norm = _normalize_text(payload.name)
    candidate_name_norm = _normalize_text(candidate.name)
    input_name_tokens = _tokenize_text(payload.name)
    candidate_name_tokens = _tokenize_text(candidate.name)
    input_specs = _spec_tokens(input_name_tokens)
    candidate_specs = _spec_tokens(candidate_name_tokens)
    input_reference_tokens = _reference_tokens(input_name_tokens)
    candidate_reference_tokens = _reference_tokens(candidate_name_tokens)
    shared_reference_tokens = input_reference_tokens.intersection(candidate_reference_tokens)

    payload_group = _normalize_text(payload.group_name)
    payload_brand = _normalize_text(payload.brand)
    payload_supplier = _normalize_text(payload.supplier)
    candidate_group = _normalize_text(candidate.group_name)
    candidate_brand = _normalize_text(candidate.brand)
    candidate_supplier = _normalize_text(candidate.supplier)

    family_hits = 0
    if payload_group and payload_group == candidate_group:
        family_hits += 1
        score += 0.3
        reasons.append("Mismo grupo.")
    if payload_brand and payload_brand == candidate_brand:
        family_hits += 1
        score += 0.22
        reasons.append("Misma marca.")
    if payload_supplier and payload_supplier == candidate_supplier:
        family_hits += 1
        score += 0.12
        reasons.append("Mismo proveedor.")

    if input_specs and candidate_specs:
        shared_specs = input_specs.intersection(candidate_specs)
        spec_ratio = len(shared_specs) / max(len(input_specs), len(candidate_specs))
        if spec_ratio > 0:
            score += 0.22 * spec_ratio
            reasons.append("Coinciden especificaciones numéricas.")

    input_name_id = _normalize_identifier(payload.name)
    candidate_name_id = _normalize_identifier(candidate.name)
    if input_name_id and candidate_name_id:
        if input_name_id == candidate_name_id:
            score += 0.28
            reasons.append("Nombre normalizado idéntico.")
        elif input_name_id in candidate_name_id or candidate_name_id in input_name_id:
            score += 0.18
            reasons.append("Nombre normalizado muy parecido.")

    if input_name_norm and candidate_name_norm:
        token_similarity = _jaccard_similarity(input_name_tokens, candidate_name_tokens)
        seq_similarity = _sequence_similarity(input_name_norm, candidate_name_norm)
        blended = max(token_similarity, seq_similarity * 0.85)
        if blended > 0:
            score += 0.18 * blended
            if blended >= 0.8:
                reasons.append("Nombre comercial muy cercano.")
            elif blended >= 0.55:
                reasons.append("Nombre comercial parcialmente similar.")

    if shared_reference_tokens:
        score += 0.2 + (0.02 * max(0, len(shared_reference_tokens) - 1))
        reasons.append("Coincide parte de la referencia alfanumérica.")

    payload_sku = _normalize_identifier(payload.sku)
    candidate_sku = _normalize_identifier(candidate.sku)
    payload_barcode = _normalize_identifier(payload.barcode)
    candidate_barcode = _normalize_identifier(candidate.barcode)
    if payload_sku and candidate_sku and payload_sku == candidate_sku:
        score = max(score, 0.98)
        reasons.append("SKU equivalente.")
    if payload_barcode and candidate_barcode and payload_barcode == candidate_barcode:
        score = max(score, 1.0)
        reasons.append("Código de barras equivalente.")

    if family_hits == 0:
        return None

    if family_hits >= 2:
        score += 0.12
        reasons.append("Coincidencia de familia comercial.")

    if family_hits == 1 and score < 0.6:
        return None

    score = max(0.0, min(1.0, score))
    min_score = 0.58
    if shared_reference_tokens:
        min_score = min(min_score, 0.48)
    if score < min_score:
        return None

    if score >= 0.9:
        risk_level = "alto"
    elif score >= 0.72:
        risk_level = "medio"
    else:
        risk_level = "bajo"

    return schemas.ProductDuplicateCandidate(
        product_id=candidate.id,
        name=candidate.name,
        sku=candidate.sku,
        barcode=candidate.barcode,
        price=float(candidate.price or 0.0),
        group_name=candidate.group_name,
        brand=candidate.brand,
        supplier=candidate.supplier,
        similarity_score=round(score, 4),
        risk_level=risk_level,
        match_reasons=reasons[:4],
    )


def _duplicate_candidate_rank(candidate: schemas.ProductDuplicateCandidate) -> tuple:
    exact_signal = 1 if any(
        reason in {
            "SKU idéntico.",
            "SKU idéntico tras normalización.",
            "Código de barras idéntico.",
            "Código de barras idéntico tras normalización.",
        }
        for reason in candidate.match_reasons
    ) else 0
    strong_signal = 1 if candidate.risk_level == "alto" else 0
    return (
        exact_signal,
        strong_signal,
        candidate.similarity_score,
        candidate.product_id,
    )


def _filter_products(products: List[models.Product], payload: ExportProductsRequest):
    if payload.scope != "filtered":
        return products
    term = (payload.search or "").strip().lower()
    min_price = _parse_price(payload.price_min)
    max_price = _parse_price(payload.price_max)
    group_filter = (payload.group or "").strip()
    brand_filter = (payload.brand or "").strip()
    supplier_filter = (payload.supplier or "").strip()

    filtered: List[models.Product] = []
    for p in products:
        if term:
            haystack = " ".join(
                [
                    p.name or "",
                    p.sku or "",
                    p.barcode or "",
                    p.group_name or "",
                    p.brand or "",
                    p.supplier or "",
                ]
            ).lower()
            if term not in haystack:
                continue

        if payload.show_only_active and not p.active:
            continue
        if payload.show_only_inactive and p.active:
            continue
        if group_filter and (p.group_name or "") != group_filter:
            continue
        if brand_filter and (p.brand or "") != brand_filter:
            continue
        if supplier_filter and (p.supplier or "") != supplier_filter:
            continue
        if min_price is not None and p.price < min_price:
            continue
        if max_price is not None and p.price > max_price:
            continue

        filtered.append(p)

    return filtered


def _build_export_rows(
    db: Session,
    products: List[models.Product],
    columns: List[str],
):
    history_map: Dict[int, Dict[str, Optional[str]]] = {}
    include_history = "history" in columns if columns else False
    if include_history and products:
        product_ids = [p.id for p in products]
        history_rows = (
            db.query(
                models.ProductAuditLog.product_id.label("product_id"),
                func.min(
                    case(
                        (
                            models.ProductAuditLog.action.in_(["create", "snapshot"]),
                            models.ProductAuditLog.created_at,
                        ),
                        else_=None,
                    )
                ).label("created_or_imported_at"),
                func.max(
                    case(
                        (models.ProductAuditLog.action == "update", models.ProductAuditLog.created_at),
                        else_=None,
                    )
                ).label("last_modified_at"),
            )
            .filter(models.ProductAuditLog.product_id.in_(product_ids))
            .group_by(models.ProductAuditLog.product_id)
            .all()
        )
        history_map = {
            int(row.product_id): {
                "created_or_imported_at": (
                    row.created_or_imported_at.isoformat(sep=" ", timespec="seconds")
                    if row.created_or_imported_at
                    else ""
                ),
                "last_modified_at": (
                    row.last_modified_at.isoformat(sep=" ", timespec="seconds")
                    if row.last_modified_at
                    else ""
                ),
            }
            for row in history_rows
        }

    column_map = {
        "id": ("ID", lambda p: p.id),
        "sku": ("SKU", lambda p: p.sku or ""),
        "name": ("Nombre", lambda p: p.name),
        "group_name": ("Grupo", lambda p: p.group_name or ""),
        "brand": ("Marca", lambda p: p.brand or ""),
        "supplier": ("Proveedor", lambda p: p.supplier or ""),
        "price": ("Precio", lambda p: p.price),
        "cost": ("Costo", lambda p: p.cost),
        "barcode": ("Código barras", lambda p: p.barcode or ""),
        "label_format": ("Formato etiquetas", lambda p: p.label_format or ""),
        "unit": ("Unidad", lambda p: p.unit or ""),
        "preferred_qty": ("Cant. preferida", lambda p: p.preferred_qty),
        "reorder_point": ("Punto pedido", lambda p: p.reorder_point),
        "stock_min": ("Stock mínimo", lambda p: p.stock_min),
        "low_stock_alert": ("Alerta stock", lambda p: 1 if p.low_stock_alert else 0),
        "allow_price_change": ("Cambio $ permitido", lambda p: 1 if p.allow_price_change else 0),
        "active": ("Activo", lambda p: 1 if p.active else 0),
        "service": ("Servicio", lambda p: 1 if p.service else 0),
        "includes_tax": ("IVA incl.", lambda p: 1 if p.includes_tax else 0),
        "is_investment": ("Es inversión", lambda p: 1 if p.is_investment else 0),
        "history_created_at": (
            "Fecha creación/importación",
            lambda p: history_map.get(p.id, {}).get("created_or_imported_at", ""),
        ),
        "history_last_modified_at": (
            "Fecha última modificación",
            lambda p: history_map.get(p.id, {}).get("last_modified_at", ""),
        ),
    }

    final_columns = list(columns) if columns else []
    expanded_columns: List[str] = []
    for key in final_columns:
        if key == "history":
            expanded_columns.extend(["history_created_at", "history_last_modified_at"])
        else:
            expanded_columns.append(key)
    final_columns = expanded_columns
    if "sku" not in final_columns:
        final_columns.insert(0, "sku")
    if "name" not in final_columns:
        final_columns.insert(1, "name")

    header = [column_map[key][0] for key in final_columns if key in column_map]
    rows = [
        [column_map[key][1](p) for key in final_columns if key in column_map]
        for p in products
    ]
    return header, rows


def _model_dump(payload: Any) -> Dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=True)
    return payload.dict(exclude_unset=True)


def _build_product_changes(
    current_product: models.Product,
    update_payload: schemas.ProductUpdate,
) -> Dict[str, Dict[str, Any]]:
    incoming = _model_dump(update_payload)
    changes: Dict[str, Dict[str, Any]] = {}
    for field, new_value in incoming.items():
        old_value = getattr(current_product, field, None)
        if old_value != new_value:
            changes[field] = {"before": old_value, "after": new_value}
    return changes


@router.get("/", response_model=List[schemas.ProductRead])
def list_products(
    skip: int = 0,
    limit: int = 10000,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products = crud.get_products(db, skip=skip, limit=limit, tenant_id=tenant_id)
    return products


@router.get("/catalog-version", response_model=schemas.CatalogVersion)
def get_catalog_version(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products_ts, groups_ts, updated_at, products_count, groups_count = crud.get_catalog_version(
        db,
        tenant_id=tenant_id,
    )
    return {
        "products_updated_at": products_ts,
        "groups_updated_at": groups_ts,
        "updated_at": updated_at,
        "products_count": products_count,
        "groups_count": groups_count,
    }


@router.post("/cost-suggestion", response_model=schemas.ProductCostSuggestionResponse)
def get_product_cost_suggestion(
    payload: schemas.ProductCostSuggestionRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    try:
        return crud.suggest_product_cost(
            db,
            tenant_id=tenant_id,
            price=payload.price,
            mode=payload.mode,
            group_name=payload.group_name,
            brand=payload.brand,
            supplier=payload.supplier,
            exclude_product_id=payload.exclude_product_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/duplicate-candidates",
    response_model=schemas.ProductDuplicateCandidatesResponse,
)
def get_product_duplicate_candidates(
    payload: schemas.ProductDuplicateCandidatesRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    search_terms = _search_terms_from_payload(payload)
    if not search_terms and not (payload.sku or "").strip() and not (payload.barcode or "").strip():
        return schemas.ProductDuplicateCandidatesResponse(candidates=[], has_high_risk=False)

    base_query = db.query(models.Product)
    if tenant_id is not None:
        base_query = base_query.filter(models.Product.tenant_id == tenant_id)

    coarse_conditions = []
    payload_sku = (payload.sku or "").strip()
    payload_barcode = (payload.barcode or "").strip()
    if payload_sku:
        coarse_conditions.append(models.Product.sku == payload_sku)
        coarse_conditions.append(models.Product.sku.ilike(f"%{payload_sku}%"))
    if payload_barcode:
        coarse_conditions.append(models.Product.barcode == payload_barcode)
        coarse_conditions.append(models.Product.barcode.ilike(f"%{payload_barcode}%"))
    for term in search_terms[:18]:
        coarse_conditions.extend(
            [
                models.Product.name.ilike(f"%{term}%"),
                models.Product.sku.ilike(f"%{term}%"),
                models.Product.barcode.ilike(f"%{term}%"),
                models.Product.group_name.ilike(f"%{term}%"),
                models.Product.brand.ilike(f"%{term}%"),
                models.Product.supplier.ilike(f"%{term}%"),
            ]
        )

    if coarse_conditions:
        # Priorizamos recall sobre agresividad para no perder coincidencias antiguas.
        candidates_rows = (
            base_query.filter(or_(*coarse_conditions))
            .order_by(models.Product.id.desc())
            .limit(5000)
            .all()
        )
    else:
        candidates_rows = base_query.order_by(models.Product.id.desc()).limit(3000).all()

    scored: List[schemas.ProductDuplicateCandidate] = []
    for row in candidates_rows:
        candidate = _build_duplicate_candidate(payload, row)
        if candidate:
            scored.append(candidate)

    family_rows = candidates_rows
    if len(scored) < 3:
        family_terms: List[str] = []
        family_seen: Set[str] = set()
        for raw in [payload.group_name, payload.brand, payload.supplier, payload.name]:
            normalized = _normalize_text(raw)
            if normalized and normalized not in family_seen:
                family_seen.add(normalized)
                family_terms.append(normalized)
        if family_terms:
            family_conditions = []
            for term in family_terms[:10]:
                family_conditions.extend(
                    [
                        models.Product.name.ilike(f"%{term}%"),
                        models.Product.sku.ilike(f"%{term}%"),
                        models.Product.barcode.ilike(f"%{term}%"),
                        models.Product.group_name.ilike(f"%{term}%"),
                        models.Product.brand.ilike(f"%{term}%"),
                        models.Product.supplier.ilike(f"%{term}%"),
                    ]
                )
            family_rows = (
                base_query.filter(or_(*family_conditions))
                .order_by(models.Product.id.desc())
                .limit(5000)
                .all()
            )
        family_scored: List[schemas.ProductDuplicateCandidate] = []
        for row in family_rows:
            candidate = _build_family_duplicate_candidate(payload, row)
            if candidate:
                family_scored.append(candidate)
        scored.extend(family_scored)

    deduped: Dict[int, schemas.ProductDuplicateCandidate] = {}
    for item in scored:
        previous = deduped.get(item.product_id)
        if previous is None or item.similarity_score > previous.similarity_score:
            deduped[item.product_id] = item
    scored = list(deduped.values())

    scored.sort(key=_duplicate_candidate_rank, reverse=True)
    limited = scored[: payload.limit]
    has_high_risk = any(item.risk_level == "alto" for item in limited)
    return schemas.ProductDuplicateCandidatesResponse(
        candidates=limited,
        has_high_risk=has_high_risk,
    )


@router.post("/", response_model=schemas.ProductRead)
def create_product(
    product_in: schemas.ProductCreate,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("products.manage")),
):
    raw_create_payload = _model_dump(product_in)
    cost_suggestion_meta = raw_create_payload.pop("cost_suggestion_meta", None)
    product_in = schemas.ProductCreate(**raw_create_payload)
    if actor.role != "Administrador":
        product_in = product_in.model_copy(update={"is_investment": False})

    tenant_id = crud.resolve_user_tenant_id(db, actor)
    # Si quieres evitar SKUs duplicados:
    if product_in.sku:
        existing = crud.get_product_by_sku(db, product_in.sku, tenant_id=tenant_id)
        if existing:
            raise HTTPException(status_code=400, detail="SKU already registered")
    if product_in.barcode:
        existing_barcode = crud.get_product_by_barcode(
            db,
            product_in.barcode,
            tenant_id=tenant_id,
        )
        if existing_barcode:
            raise HTTPException(status_code=400, detail="Barcode already registered")

    try:
        product = crud.create_product(db, product_in, tenant_id=tenant_id)
    except IntegrityError as exc:
        db.rollback()
        message = str(getattr(exc, "orig", exc)).lower()
        if "sku" in message:
            raise HTTPException(status_code=400, detail="SKU already registered") from exc
        if "barcode" in message:
            raise HTTPException(status_code=400, detail="Barcode already registered") from exc
        raise HTTPException(status_code=400, detail="No se pudo crear el producto por restricción de datos.") from exc
    crud.create_product_audit_log(
        db,
        product_id=product.id,
        action="create",
        actor_user=actor,
        changes={
            "after": _model_dump(product_in),
            **({"cost_suggestion_meta": cost_suggestion_meta} if cost_suggestion_meta else {}),
        },
    )
    return product

@router.get("/{product_id}", response_model=schemas.ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    db_product = crud.get_product(db, product_id, tenant_id=tenant_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
    return db_product


# 🔹 Actualizar producto
@router.put("/{product_id}", response_model=schemas.ProductRead)
def update_product(
    product_id: int,
    product_in: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("products.manage")),
):
    incoming_payload = _model_dump(product_in)
    cost_suggestion_meta = incoming_payload.pop("cost_suggestion_meta", None)
    product_in = schemas.ProductUpdate(**incoming_payload)
    tenant_id = crud.resolve_user_tenant_id(db, actor)
    db_product = crud.get_product(db, product_id, tenant_id=tenant_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    if actor.role != "Administrador":
        sanitized = _model_dump(product_in)
        sanitized.pop("is_investment", None)
        sanitized.pop("investment_status", None)
        sanitized.pop("investment_enabled_at", None)
        sanitized.pop("investment_disabled_at", None)
        product_in = schemas.ProductUpdate(**sanitized)

    # Si cambia el SKU, comprobamos duplicado
    if product_in.sku and product_in.sku != db_product.sku:
        existing = crud.get_product_by_sku(db, product_in.sku, tenant_id=tenant_id)
        if existing:
            raise HTTPException(status_code=400, detail="SKU already registered")
    if (
        product_in.barcode
        and product_in.barcode != db_product.barcode
    ):
        existing_barcode = crud.get_product_by_barcode(
            db,
            product_in.barcode,
            tenant_id=tenant_id,
        )
        if existing_barcode:
            raise HTTPException(status_code=400, detail="Barcode already registered")

    changes = _build_product_changes(db_product, product_in)
    try:
        updated = crud.update_product(db, db_product, product_in)
    except IntegrityError as exc:
        db.rollback()
        message = str(getattr(exc, "orig", exc)).lower()
        if "sku" in message:
            raise HTTPException(status_code=400, detail="SKU already registered") from exc
        if "barcode" in message:
            raise HTTPException(status_code=400, detail="Barcode already registered") from exc
        raise HTTPException(status_code=400, detail="No se pudo actualizar el producto por restricción de datos.") from exc
    crud.create_product_audit_log(
        db,
        product_id=updated.id,
        action="update",
        actor_user=actor,
        changes=((changes or {}) | ({"cost_suggestion_meta": cost_suggestion_meta} if cost_suggestion_meta else {})) or None,
    )
    return updated


# 🔹 Eliminar producto
@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    actor: models.PosUser = Depends(require_permission("products.manage")),
):
    tenant_id = crud.resolve_user_tenant_id(db, actor)
    db_product = crud.get_product(db, product_id, tenant_id=tenant_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    crud.create_product_audit_log(
        db,
        product_id=db_product.id,
        action="delete",
        actor_user=actor,
        changes={
            "before": {
                "id": db_product.id,
                "sku": db_product.sku,
                "name": db_product.name,
                "active": db_product.active,
            }
        },
    )
    crud.delete_product(db, db_product)
    return Response(status_code=204)


@router.get("/{product_id}/audit", response_model=List[schemas.ProductAuditLogRead])
def get_product_audit_log(
    product_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    db_product = crud.get_product(db, product_id, tenant_id=tenant_id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
    return crud.list_product_audit_logs(db, product_id=product_id, limit=limit, tenant_id=tenant_id)


@router.get("/audit/recent", response_model=List[schemas.ProductAuditLogRead])
def get_recent_product_audit_logs(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    return crud.list_recent_product_audit_logs(db, limit=limit, tenant_id=tenant_id)

@router.get("/export/csv")
def export_products_csv(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products = crud.get_products(db, skip=0, limit=100000, tenant_id=tenant_id)

    output = io.StringIO()
    writer = csv.writer(output)

    # Cabeceras
    writer.writerow(
        [
            "id",
            "sku",
            "name",
            "price",
            "cost",
            "barcode",
            "label_format",
            "unit",
            "image_url",
            "image_thumb_url",
            "stock_min",
            "active",
            "service",
            "includes_tax",
        ]
    )

    # Filas
    for p in products:
        writer.writerow(
            [
                p.id,
                p.sku or "",
                p.name,
                p.price,
                p.cost,
                p.barcode or "",
                p.label_format or "",
                p.unit or "",
                p.image_url or "",
                p.image_thumb_url or "",
                p.stock_min,
                int(p.active),
                int(p.service),
                int(p.includes_tax),
            ]
        )

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=products.csv"},
    )

router.get("/export/xlsx")
def export_products_xlsx(db: Session = Depends(get_db)):
    products = crud.get_products(db, skip=0, limit=100000)

    data = []
    for p in products:
        data.append(
            {
                "sku": p.sku,
                "nombre": p.name,
                "precio": p.price,
                "costo": p.cost,
                "grupo": p.group_name or "",
                "imagen": p.image_url or "",
                "imagen_thumb": p.image_thumb_url or "",
                "codigo_barras": p.barcode or "",
                "formato_etiquetas": p.label_format or "",
                "marca": p.brand or "",
                "proveedor": p.supplier or "",
                "cantidad_stock_bajo": p.stock_min,
                "unidad_medida": p.unit or "",
                "precio_incluye_impuestos": 1 if p.includes_tax else 0,
                "servicio_no_stock": 1 if p.service else 0,
                "producto_activo": 1 if p.active else 0,
            }
        )

    df = pd.DataFrame(data)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=products.xlsx"},
    )


@router.post("/export/xlsx")
def export_products_xlsx_custom(
    payload: ExportProductsRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products = crud.get_products(db, skip=0, limit=100000, tenant_id=tenant_id)
    filtered = _filter_products(products, payload)
    header, rows = _build_export_rows(db, filtered, payload.columns)

    df = pd.DataFrame(rows, columns=header)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")
    output.seek(0)

    file_name = payload.file_name or "productos"
    safe_name = file_name.strip() or "productos"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={safe_name}.xlsx"
        },
    )


@router.post("/export/csv")
def export_products_csv_custom(
    payload: ExportProductsRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products = crud.get_products(db, skip=0, limit=100000, tenant_id=tenant_id)
    filtered = _filter_products(products, payload)
    header, rows = _build_export_rows(db, filtered, payload.columns)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    output.seek(0)

    file_name = payload.file_name or "productos"
    safe_name = file_name.strip() or "productos"
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe_name}.csv"},
    )

@router.post("/import/xlsx")
async def import_products_xlsx(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.import")),
):
    """
    Importa productos desde un Excel con columnas:
    sku, nombre, precio, costo, grupo, codigo_barras, marca, proveedor,
    cantidad_stock_bajo, unidad_medida, precio_incluye_impuestos,
    servicio_no_stock, producto_activo.
    """
    contents = await file.read()
    try:
        df = pd.read_excel(BytesIO(contents))
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo Excel")

    created = 0
    updated = 0
    tenant_id = crud.resolve_user_tenant_id(db, current_user)

    for _, row in df.iterrows():
        row_map = _row_to_normalized_map(row)

        sku_val = _coalesce_row_value(row_map, "sku")
        sku = str(sku_val).strip() if sku_val is not None and not pd.isna(sku_val) else ""

        codigo_val = _coalesce_row_value(row_map, "codigo_barras", "barcode")
        barcode = (
            str(codigo_val).strip()
            if codigo_val is not None and not pd.isna(codigo_val)
            else None
        )
        if barcode == "":
            barcode = None

        if not sku and not barcode:
            continue

        nombre_val = _coalesce_row_value(row_map, "nombre", "name")
        if nombre_val is None or pd.isna(nombre_val) or str(nombre_val).strip() == "":
            continue
        name = str(nombre_val).strip()

        precio = _parse_floatish(_coalesce_row_value(row_map, "precio", "price"), 0.0)
        costo = _parse_floatish(_coalesce_row_value(row_map, "costo", "cost"), 0.0)

        # mapeos directos
        grupo_val = _coalesce_row_value(row_map, "grupo", "group_name", "group")
        group_name = str(grupo_val).strip() if not pd.isna(grupo_val) else None
        if group_name == "":
            group_name = None

        marca_val = _coalesce_row_value(row_map, "marca", "brand")
        brand = str(marca_val).strip() if not pd.isna(marca_val) else None
        if brand == "":
            brand = None

        prov_val = _coalesce_row_value(row_map, "proveedor", "supplier")
        supplier = str(prov_val).strip() if not pd.isna(prov_val) else None
        if supplier == "":
            supplier = None

        unidad_val = _coalesce_row_value(row_map, "unidad_medida", "unidad", "unit")
        unit = str(unidad_val).strip() if not pd.isna(unidad_val) else None
        if unit == "":
            unit = None

        stock_min_val = _coalesce_row_value(
            row_map,
            "cantidad_stock_bajo",
            "stock_min",
            "stock_minimo",
        )
        stock_min = _parse_intish(stock_min_val, 0)

        preferred_val = _coalesce_row_value(
            row_map,
            "cantidad_preferida",
            "cant_preferida",
            "preferred_qty",
        )
        preferred_qty = _parse_intish(preferred_val, 0)

        reorder_val = _coalesce_row_value(row_map, "punto_pedido", "reorder_point")
        reorder_point = _parse_intish(reorder_val, 0)

        low_stock_val = _coalesce_row_value(
            row_map,
            "advertencia_stock_bajo",
            "alerta_stock",
            "low_stock_alert",
        )
        low_stock_alert = _parse_boolish(low_stock_val, False)

        change_val = _coalesce_row_value(
            row_map,
            "cambio_precio_permitido",
            "cambio_permitido",
            "allow_price_change",
        )
        allow_price_change = _parse_boolish(change_val, False)

        incluye_iva_val = _coalesce_row_value(
            row_map,
            "precio_incluye_impuestos",
            "includes_tax",
        )
        includes_tax = _parse_boolish(incluye_iva_val, False)

        servicio_val = _coalesce_row_value(row_map, "servicio_no_stock", "service")
        service = _parse_boolish(servicio_val, False)

        activo_val = _coalesce_row_value(row_map, "producto_activo", "activo", "active")
        active = _parse_boolish(activo_val, True)

        product_data = schemas.ProductCreate(
            sku=sku or None,
            name=name,
            price=precio,
            cost=costo,
            barcode=barcode,
            unit=unit,
            stock_min=stock_min,
            preferred_qty=preferred_qty,
            reorder_point=reorder_point,
            low_stock_alert=low_stock_alert,    
            allow_price_change=allow_price_change,
            active=active,
            service=service,
            includes_tax=includes_tax,
            group_name=group_name,
            brand=brand,
            supplier=supplier,
        )

        existing = None
        if sku:
            existing = crud.get_product_by_sku(db, sku, tenant_id=tenant_id)
        if not existing and barcode:
            existing = crud.get_product_by_barcode(db, barcode, tenant_id=tenant_id)
        if existing:
            crud.update_product(db, existing, product_data)
            updated += 1
        else:
            crud.create_product(db, product_data, tenant_id=tenant_id)
            created += 1

    return {"created": created, "updated": updated}

@router.get("/export/xlsx")
def export_products_xlsx(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    """
    Exporta todos los productos a un archivo Excel (.xlsx)
    con las mismas columnas que usamos para importar.
    """
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products = crud.get_products(db, skip=0, limit=1_000_000, tenant_id=tenant_id)

    data = []
    for p in products:
        data.append(
            {
                "sku": p.sku or "",
                "nombre": p.name,
                "precio": p.price,
                "costo": p.cost,
                "grupo": p.group_name or "",
                "codigo_barras": p.barcode or "",
                "formato_etiquetas": p.label_format or "",
                "marca": p.brand or "",
                "proveedor": p.supplier or "",
                "cantidad_stock_bajo": p.stock_min,
                "unidad_medida": p.unit or "",
                "precio_incluye_impuestos": 1 if p.includes_tax else 0,
                "servicio_no_stock": 1 if p.service else 0,
                "producto_activo": 1 if p.active else 0,
            }
        )

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")
    output.seek(0)

    headers = {
        "Content-Disposition": 'attachment; filename="productos_kensar.xlsx"'
    }

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/export/csv")
def export_products_csv(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("products.view")),
):
    """
    Exporta todos los productos a un archivo CSV.
    """
    tenant_id = crud.resolve_user_tenant_id(db, current_user)
    products = crud.get_products(db, skip=0, limit=1_000_000, tenant_id=tenant_id)

    data = []
    for p in products:
        data.append(
            {
                "sku": p.sku or "",
                "nombre": p.name,
                "precio": p.price,
                "costo": p.cost,
                "grupo": p.group_name or "",
                "codigo_barras": p.barcode or "",
                "formato_etiquetas": p.label_format or "",
                "marca": p.brand or "",
                "proveedor": p.supplier or "",
                "cantidad_stock_bajo": p.stock_min,
                "cantidad_preferida": p.preferred_qty,
                "punto_pedido": p.reorder_point,
                "advertencia_stock_bajo": 1 if p.low_stock_alert else 0,
                "unidad_medida": p.unit or "",
                "precio_incluye_impuestos": 1 if p.includes_tax else 0,
                "servicio_no_stock": 1 if p.service else 0,
                "cambio_precio_permitido": 1 if p.allow_price_change else 0,
                "producto_activo": 1 if p.active else 0,
            }
        )

    df = pd.DataFrame(data)
    csv_data = df.to_csv(index=False)

    headers = {
        "Content-Disposition": 'attachment; filename="productos_kensar.csv"'
    }

    return PlainTextResponse(csv_data, media_type="text/csv", headers=headers)
