from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

import models
from services import storage


@dataclass
class ProcessResult:
    sales_loaded: int
    items_loaded: int
    payments_loaded: int
    warnings: list[str]


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    # Drop non-printable control chars that can break PostgreSQL text fields.
    text = re.sub(r"[\x01-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    return text.strip()


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _clean_str(value).lower()).strip("_")


def _find_key(row: dict[str, Any], *candidates: str) -> Optional[str]:
    if not row:
        return None
    by_norm = {_norm_key(k): k for k in row.keys()}
    for candidate in candidates:
        key = by_norm.get(_norm_key(candidate))
        if key:
            return key
    return None


def _get_value(row: dict[str, Any], *candidates: str) -> str:
    key = _find_key(row, *candidates)
    if not key:
        return ""
    return _clean_str(row.get(key))


def _to_float(value: Any) -> float:
    raw = _clean_str(value)
    if not raw:
        return 0.0
    normalized = raw.replace("$", "").replace(" ", "")
    if "." in normalized and "," in normalized:
        # 1.234,56 -> 1234.56
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        # 1234,56 -> 1234.56
        normalized = normalized.replace(",", ".")
    else:
        # 1234.56 or 123456
        normalized = normalized
    try:
        return float(normalized)
    except Exception:
        return 0.0


def _to_int(value: Any) -> Optional[int]:
    raw = _clean_str(value)
    if not raw:
        return None
    digits = re.sub(r"[^0-9-]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime:
    raw = _clean_str(value)
    if not raw:
        return datetime.utcnow()
    patterns = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%y",
    ]
    for pattern in patterns:
        try:
            parsed = datetime.strptime(raw, pattern)
            bogota_tz = ZoneInfo("America/Bogota")
            # Legacy exports carry local business time; persist as UTC-naive
            # to match the rest of the schema/query conventions.
            return (
                parsed.replace(tzinfo=bogota_tz)
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )
        except Exception:
            continue
    return datetime.utcnow()


def _batch_dir(tenant_id: Optional[int], batch_id: int) -> Path:
    root = storage.get_uploads_root_dir()
    tenant_fragment = str(int(tenant_id)) if tenant_id is not None else "global"
    path = root / "legacy-imports" / tenant_fragment / str(batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_uploaded_file(
    *,
    tenant_id: Optional[int],
    batch_id: int,
    file_kind: str,
    filename: str,
    content: bytes,
) -> str:
    safe_kind = _norm_key(file_kind) or "file"
    extension = Path(filename or "").suffix or ".csv"
    target = _batch_dir(tenant_id, batch_id) / f"{safe_kind}{extension.lower()}"
    target.write_bytes(content)
    return str(target)


def _must_path(path_value: Optional[str], label: str) -> Path:
    if not path_value:
        raise ValueError(f"Falta archivo de {label} en el lote")
    path = Path(path_value)
    if not path.exists():
        raise ValueError(f"No existe archivo de {label}: {path_value}")
    return path


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def process_batch(db: Session, batch: models.LegacyImportBatch) -> ProcessResult:
    sales_path = _must_path(batch.uploaded_sales_path, "ventas")
    items_path = _must_path(batch.uploaded_items_path, "items")
    payments_path = _must_path(batch.uploaded_payments_path, "pagos")

    sales_rows = _read_csv(sales_path)
    items_rows = _read_csv(items_path)
    payment_rows = _read_csv(payments_path)

    if not sales_rows:
        raise ValueError("El CSV de ventas está vacío")

    batch.status = "processing"
    db.flush()

    by_source_doc: dict[str, models.LegacySale] = {}
    warnings: list[str] = []

    for row in sales_rows:
        source_document_id = _get_value(
            row,
            "document_id",
            "source_document_id",
            "sale_id",
            "id",
            "doc_id",
        )
        if not source_document_id:
            warnings.append("Se omitió una venta sin document_id")
            continue

        source_document_number = _get_value(
            row,
            "document_number",
            "source_document_number",
            "invoice_number",
            "number",
            "doc_number",
        )
        display_number = (
            f"ARO-{source_document_number}" if source_document_number else f"ARO-{source_document_id}"
        )
        created_at = _parse_dt(_get_value(row, "created_at", "date", "document_date"))
        total = _to_float(_get_value(row, "total", "grand_total", "amount"))
        paid_amount = _to_float(_get_value(row, "paid_amount", "paid", "amount_paid"))
        change_amount = _to_float(_get_value(row, "change_amount", "change"))
        sale_number = _to_int(_get_value(row, "sale_number", "number", "document_sequence"))

        existing = (
            db.query(models.LegacySale)
            .filter(models.LegacySale.tenant_id == batch.tenant_id)
            .filter(models.LegacySale.source_system == batch.source_system)
            .filter(models.LegacySale.source_document_id == source_document_id)
            .first()
        )
        if existing:
            legacy_sale = existing
            legacy_sale.import_batch_id = batch.id
        else:
            legacy_sale = models.LegacySale(
                tenant_id=batch.tenant_id,
                import_batch_id=batch.id,
                source_system=batch.source_system,
                source_document_id=source_document_id,
            )
            db.add(legacy_sale)

        legacy_sale.source_document_number = source_document_number or None
        legacy_sale.display_document_number = display_number
        legacy_sale.sale_number = sale_number
        legacy_sale.created_at = created_at
        legacy_sale.pos_name = _get_value(row, "pos", "pos_name", "station", "terminal") or "POS Legacy"
        legacy_sale.vendor_name = _get_value(row, "seller", "vendor", "seller_name", "cashier") or ""
        legacy_sale.customer_name = _get_value(row, "customer", "customer_name") or ""
        legacy_sale.customer_phone = _get_value(row, "customer_phone", "phone") or ""
        legacy_sale.customer_email = _get_value(row, "customer_email", "email") or ""
        legacy_sale.payment_method = _get_value(row, "payment_method", "method") or "mixed"
        legacy_sale.main_payment_method = _get_value(row, "main_payment_method", "payment_method", "method") or "mixed"
        legacy_sale.total = total
        legacy_sale.paid_amount = paid_amount if paid_amount > 0 else total
        legacy_sale.change_amount = change_amount
        legacy_sale.status = "completed"
        legacy_sale.imported_at = datetime.utcnow()
        by_source_doc[source_document_id] = legacy_sale

    db.flush()

    sale_ids = [sale.id for sale in by_source_doc.values() if sale.id is not None]
    if sale_ids:
        db.query(models.LegacySaleItem).filter(models.LegacySaleItem.legacy_sale_id.in_(sale_ids)).delete(
            synchronize_session=False
        )
        db.query(models.LegacyPayment).filter(models.LegacyPayment.legacy_sale_id.in_(sale_ids)).delete(
            synchronize_session=False
        )

    items_loaded = 0
    for row in items_rows:
        source_document_id = _get_value(
            row,
            "document_id",
            "source_document_id",
            "sale_id",
            "doc_id",
        )
        sale = by_source_doc.get(source_document_id)
        if not sale:
            continue
        quantity = _to_float(_get_value(row, "quantity", "qty", "units"))
        unit_price = _to_float(_get_value(row, "unit_price", "price"))
        line_total = _to_float(_get_value(row, "total", "line_total", "amount"))
        discount_value = _to_float(_get_value(row, "discount", "discount_value", "line_discount"))

        item = models.LegacySaleItem(
            tenant_id=batch.tenant_id,
            import_batch_id=batch.id,
            legacy_sale_id=sale.id,
            source_item_id=_get_value(row, "item_id", "id") or None,
            product_id=_to_int(_get_value(row, "product_id")),
            product_sku=_get_value(row, "product_sku", "sku") or None,
            product_name=_get_value(row, "product_name", "name") or "Producto legacy",
            product_group=_get_value(row, "product_group", "group", "group_name", "category") or None,
            quantity=quantity,
            unit_price=unit_price,
            line_discount_value=discount_value,
            total=line_total if line_total != 0 else max((unit_price * quantity) - discount_value, 0.0),
            imported_at=datetime.utcnow(),
        )
        db.add(item)
        items_loaded += 1

    payments_loaded = 0
    for row in payment_rows:
        source_document_id = _get_value(
            row,
            "document_id",
            "source_document_id",
            "sale_id",
            "doc_id",
        )
        sale = by_source_doc.get(source_document_id)
        if not sale:
            continue
        payment = models.LegacyPayment(
            tenant_id=batch.tenant_id,
            import_batch_id=batch.id,
            legacy_sale_id=sale.id,
            source_payment_id=_get_value(row, "payment_id", "id") or None,
            method=_get_value(row, "payment_method", "method", "payment_type_name") or "legacy",
            amount=_to_float(_get_value(row, "amount", "paid_amount", "value")),
            imported_at=datetime.utcnow(),
        )
        db.add(payment)
        payments_loaded += 1

    batch.status = "published"
    batch.processed_at = datetime.utcnow()
    batch.published_at = datetime.utcnow()
    batch.updated_at = datetime.utcnow()
    db.commit()

    return ProcessResult(
        sales_loaded=len(by_source_doc),
        items_loaded=items_loaded,
        payments_loaded=payments_loaded,
        warnings=warnings,
    )


def list_batches(db: Session, tenant_id: Optional[int], limit: int = 50) -> list[models.LegacyImportBatch]:
    query = db.query(models.LegacyImportBatch)
    if tenant_id is not None:
        query = query.filter(models.LegacyImportBatch.tenant_id == tenant_id)
    return query.order_by(models.LegacyImportBatch.created_at.desc()).limit(limit).all()


def map_legacy_sales_to_report_rows(
    db: Session,
    *,
    tenant_id: Optional[int],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> list[dict[str, Any]]:
    query = db.query(models.LegacySale)
    if tenant_id is not None:
        query = query.filter(models.LegacySale.tenant_id == tenant_id)
    query = query.filter(models.LegacySale.status == "completed")
    if date_from is not None:
        query = query.filter(models.LegacySale.created_at >= date_from)
    if date_to is not None:
        query = query.filter(models.LegacySale.created_at < date_to)
    sales = query.order_by(models.LegacySale.created_at.desc()).all()
    if not sales:
        return []

    sale_ids = [sale.id for sale in sales]
    items_by_sale: dict[int, list[models.LegacySaleItem]] = {}
    payments_by_sale: dict[int, list[models.LegacyPayment]] = {}

    item_rows = db.query(models.LegacySaleItem).filter(models.LegacySaleItem.legacy_sale_id.in_(sale_ids)).all()
    for item in item_rows:
        items_by_sale.setdefault(int(item.legacy_sale_id), []).append(item)

    payment_rows = db.query(models.LegacyPayment).filter(models.LegacyPayment.legacy_sale_id.in_(sale_ids)).all()
    for payment in payment_rows:
        payments_by_sale.setdefault(int(payment.legacy_sale_id), []).append(payment)

    out: list[dict[str, Any]] = []
    for sale in sales:
        row_items = items_by_sale.get(int(sale.id), [])
        row_payments = payments_by_sale.get(int(sale.id), [])
        out.append(
            {
                "id": -int(sale.id),
                "sale_number": sale.sale_number,
                "document_number": sale.display_document_number or sale.source_document_number or f"ARO-{sale.source_document_id}",
                "created_at": sale.created_at,
                "status": "completed",
                "voided_at": None,
                "total": float(sale.total or 0.0),
                "paid_amount": float(sale.paid_amount or sale.total or 0.0),
                "payment_method": sale.main_payment_method or sale.payment_method or "legacy",
                "payments": [
                    {"id": -(idx + 1), "method": payment.method or "legacy", "amount": float(payment.amount or 0.0)}
                    for idx, payment in enumerate(row_payments)
                ],
                "pos_name": sale.pos_name or "POS Legacy",
                "vendor_name": sale.vendor_name,
                "customer_name": sale.customer_name,
                "customer_phone": sale.customer_phone,
                "customer_email": sale.customer_email,
                "notes": f"Importado desde {sale.source_system}",
                "cart_discount_value": 0.0,
                "cart_discount_percent": 0.0,
                "surcharge_amount": 0.0,
                "surcharge_label": None,
                "items": [
                    {
                        "id": -(item.id),
                        "product_id": int(item.product_id) if item.product_id is not None else 0,
                        "quantity": float(item.quantity or 0.0),
                        "unit_price": float(item.unit_price or 0.0),
                        "unit_price_original": float(item.unit_price or 0.0),
                        "product_sku": item.product_sku,
                        "product_name": item.product_name,
                        "product_barcode": None,
                        "discount": float(item.line_discount_value or 0.0),
                        "line_discount_value": float(item.line_discount_value or 0.0),
                        "total": float(item.total or 0.0),
                    }
                    for item in row_items
                ],
                "returns": [],
                "changes": [],
                "refunded_payments": [],
                "refunded_total": 0.0,
                "refund_count": 0,
                "refunded_balance": 0.0,
                "closure_id": None,
                "is_separated": False,
                "initial_payment_method": None,
                "initial_payment_amount": None,
                "balance": None,
                "has_cash_payment": any((payment.method or "").strip().lower() in {"cash", "efectivo"} for payment in row_payments),
                "change_amount": float(sale.change_amount or 0.0),
                "source_system": sale.source_system,
                "is_imported": True,
            }
        )
    return out
