from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import false, func, or_, text, true
from sqlalchemy.orm import Session

import models


MANUAL_TYPES = {
    "mov_salida_manual": "salida_manual",
    "mov_venta_manual": "venta_manual",
    "mov_ajuste": "ajuste",
    "mov_perdida_dano": "perdida_dano",
}


def _tenant_clause(column: Any, tenant_id: int | None):
    return column == tenant_id if tenant_id is not None else true()


def _contains(term: str, *columns: Any):
    pattern = f"%{term.strip().lower()}%"
    return or_(*(func.lower(func.coalesce(column, "")).like(pattern) for column in columns))


def _range(query: Any, column: Any, date_from: datetime | None, date_to: datetime | None):
    if date_from is not None:
        query = query.filter(column >= date_from)
    if date_to is not None:
        query = query.filter(column < date_to)
    return query


def _base_item(
    *,
    key: str,
    document_type: str,
    record_id: int,
    occurred_at: datetime,
    document_number: str,
    reference: str,
    detail: str,
    total: float = 0.0,
    payment_method: str | None = None,
    payment_stage: str | None = None,
    is_separated: bool = False,
    sale_id: int | None = None,
    customer: str | None = None,
    pos: str | None = None,
    vendor: str | None = None,
    status: str | None = None,
    payment_status: str | None = None,
    closure_id: int | None = None,
    source_system: str = "metrik",
    content_summary: str | None = None,
) -> dict[str, Any]:
    return {
        "id": key,
        "type": document_type,
        "record_id": record_id,
        "sale_id": sale_id,
        "occurred_at": occurred_at,
        "document_number": document_number,
        "reference": reference,
        "detail": detail,
        "total": float(total or 0.0),
        "payment_method": payment_method,
        "payment_stage": payment_stage,
        "is_separated": is_separated,
        "customer": customer,
        "pos": pos,
        "vendor": vendor,
        "status": status,
        "payment_status": payment_status,
        "closure_id": closure_id,
        "source_system": source_system,
        "content_summary": content_summary,
    }


def _format_item_quantity(value: Any) -> str:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        numeric = 0
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"


def _summarize_item_rows(
    items: Iterable[tuple[str, Any]],
    *,
    prefix: str | None = None,
    limit: int = 2,
) -> str:
    grouped: dict[str, float] = {}
    for raw_name, raw_quantity in items:
        name = (raw_name or "Producto").strip() or "Producto"
        try:
            quantity = float(raw_quantity or 0)
        except (TypeError, ValueError):
            quantity = 0
        grouped[name] = grouped.get(name, 0) + quantity
    entries = [
        f"{name} ×{_format_item_quantity(quantity)}"
        for name, quantity in list(grouped.items())[:limit]
    ]
    remaining = max(0, len(grouped) - limit)
    if remaining:
        entries.append(f"+{remaining} más")
    summary = " · ".join(entries)
    return f"{prefix}: {summary}" if prefix and summary else summary


def _group_preview_rows(rows: Iterable[Any]) -> dict[int, list[tuple[str, Any]]]:
    grouped: dict[int, list[tuple[str, Any]]] = {}
    for parent_id, name, quantity in rows:
        grouped.setdefault(int(parent_id), []).append((name, quantity))
    return grouped


def _attach_content_summaries(
    db: Session,
    page: list[dict[str, Any]],
    tenant_id: int | None,
) -> None:
    """Attach compact item previews using batched queries per document kind."""
    if not page:
        return

    sale_ids = {
        int(row["sale_id"] or row["record_id"])
        for row in page
        if row["type"] in {"venta", "abono"}
        and not str(row["id"]).startswith("legacy-sale-")
        and (row.get("sale_id") or row.get("record_id"))
    }
    sale_items: dict[int, list[tuple[str, Any]]] = {}
    if sale_ids:
        sale_items = _group_preview_rows(
            db.query(
                models.SaleItem.sale_id,
                models.SaleItem.product_name,
                models.SaleItem.quantity,
            )
            .filter(models.SaleItem.sale_id.in_(sale_ids))
            .filter(_tenant_clause(models.SaleItem.tenant_id, tenant_id))
            .order_by(models.SaleItem.sale_id, models.SaleItem.id)
            .all()
        )

    preview_specs = [
        ("legacy-sale-", models.LegacySaleItem, models.LegacySaleItem.legacy_sale_id, models.LegacySaleItem.product_name, models.LegacySaleItem.quantity, None),
        ("web-order-", models.WebOrderItem, models.WebOrderItem.web_order_id, models.WebOrderItem.product_name_snapshot, models.WebOrderItem.quantity, None),
        ("return-", models.SaleReturnItem, models.SaleReturnItem.return_id, models.SaleReturnItem.product_name, models.SaleReturnItem.quantity, "Devuelve"),
        ("receiving-", models.ReceivingLotItem, models.ReceivingLotItem.lot_id, models.ReceivingLotItem.product_name_snapshot, models.ReceivingLotItem.qty_received, "Recibe"),
        ("manual-movement-", models.ManualMovementDocumentLine, models.ManualMovementDocumentLine.document_id, models.ManualMovementDocumentLine.product_name_snapshot, models.ManualMovementDocumentLine.qty, None),
        (
            "recount-",
            models.InventoryRecountLine,
            models.InventoryRecountLine.recount_id,
            models.InventoryRecountLine.product_name_snapshot,
            func.coalesce(
                models.InventoryRecountLine.counted_qty,
                models.InventoryRecountLine.system_qty,
            ),
            "Conteo",
        ),
    ]
    previews_by_prefix: dict[str, tuple[dict[int, list[tuple[str, Any]]], str | None]] = {}
    for prefix, model, parent_column, name_column, quantity_column, label in preview_specs:
        parent_ids = {
            int(row["record_id"])
            for row in page
            if str(row["id"]).startswith(prefix)
        }
        if not parent_ids:
            continue
        preview_rows = (
            db.query(parent_column, name_column, quantity_column)
            .filter(parent_column.in_(parent_ids))
            .filter(_tenant_clause(model.tenant_id, tenant_id))
            .order_by(parent_column, model.id)
            .all()
        )
        previews_by_prefix[prefix] = (_group_preview_rows(preview_rows), label)

    change_ids = {
        int(row["record_id"])
        for row in page
        if row["type"] == "cambio"
    }
    change_out: dict[int, list[tuple[str, Any]]] = {}
    change_in: dict[int, list[tuple[str, Any]]] = {}
    if change_ids:
        change_out = _group_preview_rows(
            db.query(
                models.SaleChangeReturnItem.change_id,
                models.SaleChangeReturnItem.product_name,
                models.SaleChangeReturnItem.quantity,
            )
            .filter(models.SaleChangeReturnItem.change_id.in_(change_ids))
            .filter(_tenant_clause(models.SaleChangeReturnItem.tenant_id, tenant_id))
            .order_by(models.SaleChangeReturnItem.change_id, models.SaleChangeReturnItem.id)
            .all()
        )
        change_in = _group_preview_rows(
            db.query(
                models.SaleChangeNewItem.change_id,
                models.SaleChangeNewItem.product_name,
                models.SaleChangeNewItem.quantity,
            )
            .filter(models.SaleChangeNewItem.change_id.in_(change_ids))
            .filter(_tenant_clause(models.SaleChangeNewItem.tenant_id, tenant_id))
            .order_by(models.SaleChangeNewItem.change_id, models.SaleChangeNewItem.id)
            .all()
        )

    for row in page:
        row_id = str(row["id"])
        if row["type"] in {"venta", "abono"} and not row_id.startswith("legacy-sale-"):
            sale_id = int(row.get("sale_id") or row["record_id"])
            label = "Venta" if row["type"] == "abono" else None
            row["content_summary"] = _summarize_item_rows(
                sale_items.get(sale_id, []), prefix=label
            )
            continue
        if row["type"] == "cambio":
            outgoing = _summarize_item_rows(
                change_out.get(int(row["record_id"]), []), prefix="Sale", limit=1
            )
            incoming = _summarize_item_rows(
                change_in.get(int(row["record_id"]), []), prefix="Entra", limit=1
            )
            row["content_summary"] = " → ".join(
                part for part in (outgoing, incoming) if part
            )
            continue
        matched = False
        for prefix, (grouped, label) in previews_by_prefix.items():
            if row_id.startswith(prefix):
                row["content_summary"] = _summarize_item_rows(
                    grouped.get(int(row["record_id"]), []), prefix=label
                )
                matched = True
                break
        if not matched and row["type"] == "cierre":
            row["content_summary"] = row["detail"]
        if not row.get("content_summary"):
            row["content_summary"] = "Sin productos detallados"


def search_documents(
    db: Session,
    *,
    tenant_id: int | None,
    document_type: str,
    date_from: datetime | None,
    date_to: datetime | None,
    term: str | None,
    payment_method: str | None,
    customer: str | None,
    pos: str | None,
    vendor: str | None,
    skip: int,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    # Protect the transactional/POS database from a pathological free-text
    # filter. This is local to the current PostgreSQL transaction and does not
    # change the global database configuration.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL statement_timeout = '5000ms'"))

    scan_limit = min(5001, skip + limit + 1)
    rows: list[dict[str, Any]] = []
    normalized_type = (document_type or "all").strip().lower()
    cleaned_term = (term or "").strip()
    cleaned_payment = (payment_method or "").strip().lower()
    cleaned_customer = (customer or "").strip()
    cleaned_pos = (pos or "").strip()
    cleaned_vendor = (vendor or "").strip()

    include_sales = normalized_type in {"all", "venta", "anulacion"}
    if include_sales:
        query = (
            db.query(models.Sale, models.SeparatedOrder.id.label("separated_id"))
            .outerjoin(models.SeparatedOrder, models.SeparatedOrder.sale_id == models.Sale.id)
            .filter(_tenant_clause(models.Sale.tenant_id, tenant_id))
        )
        query = _range(query, models.Sale.created_at, date_from, date_to)
        if normalized_type == "anulacion":
            query = query.filter(models.Sale.status == "voided")
        if cleaned_term:
            query = query.filter(
                or_(
                    _contains(cleaned_term, models.Sale.document_number, models.Sale.notes),
                    models.Sale.items.any(
                        _contains(cleaned_term, models.SaleItem.product_name, models.SaleItem.product_sku)
                    ),
                )
            )
        if cleaned_payment == "separated":
            query = query.filter(models.SeparatedOrder.id.isnot(None))
        elif cleaned_payment == "mixed":
            payment_count = (
                db.query(func.count(models.SalePayment.id))
                .filter(models.SalePayment.sale_id == models.Sale.id)
                .correlate(models.Sale)
                .scalar_subquery()
            )
            query = query.filter(payment_count > 1)
        elif cleaned_payment:
            query = query.filter(
                or_(
                    func.lower(models.Sale.main_payment_method) == cleaned_payment,
                    func.lower(models.Sale.payment_method) == cleaned_payment,
                    models.Sale.payments.any(func.lower(models.SalePayment.method) == cleaned_payment),
                )
            )
        if cleaned_customer:
            query = query.filter(models.Sale.customer_name.ilike(f"%{cleaned_customer}%"))
        if cleaned_pos:
            query = query.filter(models.Sale.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            query = query.filter(models.Sale.vendor_name.ilike(f"%{cleaned_vendor}%"))
        for sale, separated_id in query.order_by(models.Sale.created_at.desc(), models.Sale.id.desc()).limit(scan_limit):
            rows.append(
                _base_item(
                    key=f"sale-{sale.id}", document_type="venta", record_id=sale.id,
                    occurred_at=sale.created_at,
                    document_number=sale.document_number or f"V-{sale.id:06d}",
                    reference=f"Ticket #{sale.sale_number or sale.id}", detail="Venta registrada",
                    total=sale.total, payment_method=sale.main_payment_method or sale.payment_method,
                    is_separated=separated_id is not None, sale_id=sale.id,
                    customer=sale.customer_name, pos=sale.pos_name, vendor=sale.vendor_name,
                    status=sale.status, closure_id=sale.closure_id,
                )
            )

    if normalized_type in {"all", "venta"}:
        query = db.query(models.LegacySale).filter(_tenant_clause(models.LegacySale.tenant_id, tenant_id))
        query = _range(query, models.LegacySale.created_at, date_from, date_to)
        if cleaned_term:
            query = query.filter(_contains(cleaned_term, models.LegacySale.display_document_number, models.LegacySale.source_document_number))
        if cleaned_payment in {"separated", "mixed"}:
            query = query.filter(false())
        elif cleaned_payment:
            query = query.filter(func.lower(models.LegacySale.main_payment_method) == cleaned_payment)
        if cleaned_customer:
            query = query.filter(models.LegacySale.customer_name.ilike(f"%{cleaned_customer}%"))
        if cleaned_pos:
            query = query.filter(models.LegacySale.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            query = query.filter(models.LegacySale.vendor_name.ilike(f"%{cleaned_vendor}%"))
        for sale in query.order_by(models.LegacySale.created_at.desc(), models.LegacySale.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"legacy-sale-{sale.id}", document_type="venta", record_id=sale.id,
                occurred_at=sale.created_at,
                document_number=sale.display_document_number or sale.source_document_number or f"LEG-{sale.id}",
                reference=f"Venta histórica · {sale.source_system}", detail="Venta importada",
                total=sale.total, payment_method=sale.main_payment_method or sale.payment_method,
                customer=sale.customer_name, pos=sale.pos_name, vendor=sale.vendor_name,
                status=sale.status, source_system=sale.source_system,
            ))

    if normalized_type in {"all", "orden_web"}:
        query = db.query(models.WebOrder).filter(_tenant_clause(models.WebOrder.tenant_id, tenant_id))
        query = _range(query, models.WebOrder.created_at, date_from, date_to)
        if cleaned_term:
            query = query.filter(or_(
                _contains(cleaned_term, models.WebOrder.document_number, models.WebOrder.notes),
                models.WebOrder.items.any(_contains(
                    cleaned_term,
                    models.WebOrderItem.product_name_snapshot,
                    models.WebOrderItem.product_sku_snapshot,
                )),
            ))
        if cleaned_customer:
            query = query.filter(models.WebOrder.customer_name.ilike(f"%{cleaned_customer}%"))
        if not cleaned_pos and not cleaned_vendor and not cleaned_payment:
            for order in query.order_by(models.WebOrder.created_at.desc(), models.WebOrder.id.desc()).limit(scan_limit):
                rows.append(_base_item(
                    key=f"web-order-{order.id}", document_type="orden_web", record_id=order.id,
                    occurred_at=order.created_at,
                    document_number=order.document_number or f"OW-{order.id:06d}",
                    reference="Orden de comercio web", detail="Pedido web",
                    total=order.total, customer=order.customer_name, status=order.status,
                    payment_status=order.payment_status, sale_id=order.sale_id,
                ))

    if normalized_type in {"all", "devolucion", "anulacion"}:
        query = (
            db.query(models.SaleReturn, models.Sale)
            .join(models.Sale, models.Sale.id == models.SaleReturn.sale_id)
            .filter(_tenant_clause(models.SaleReturn.tenant_id, tenant_id))
        )
        query = _range(query, models.SaleReturn.created_at, date_from, date_to)
        if normalized_type == "anulacion":
            query = query.filter(models.SaleReturn.status == "voided")
        if cleaned_term:
            query = query.filter(or_(
                _contains(cleaned_term, models.SaleReturn.document_number, models.Sale.document_number, models.SaleReturn.notes),
                models.SaleReturn.items.any(_contains(
                    cleaned_term,
                    models.SaleReturnItem.product_name,
                    models.SaleReturnItem.product_sku,
                )),
            ))
        if cleaned_customer:
            query = query.filter(models.Sale.customer_name.ilike(f"%{cleaned_customer}%"))
        if cleaned_pos:
            query = query.filter(models.Sale.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            query = query.filter(models.Sale.vendor_name.ilike(f"%{cleaned_vendor}%"))
        if cleaned_payment == "mixed":
            payment_count = (
                db.query(func.count(models.SaleReturnPayment.id))
                .filter(models.SaleReturnPayment.return_id == models.SaleReturn.id)
                .correlate(models.SaleReturn)
                .scalar_subquery()
            )
            query = query.filter(payment_count > 1)
        elif cleaned_payment == "separated":
            query = query.filter(false())
        elif cleaned_payment:
            query = query.filter(models.SaleReturn.payments.any(
                func.lower(models.SaleReturnPayment.method) == cleaned_payment
            ))
        for ret, sale in query.order_by(models.SaleReturn.created_at.desc(), models.SaleReturn.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"return-{ret.id}", document_type="devolucion", record_id=ret.id,
                occurred_at=ret.created_at,
                document_number=ret.document_number or f"R-{ret.id:06d}",
                reference=f"Ref. {sale.document_number or sale.id}", detail="Devolución registrada",
                total=-abs(ret.total_refund), sale_id=sale.id, customer=sale.customer_name,
                pos=sale.pos_name, vendor=sale.vendor_name, status=ret.status, closure_id=ret.closure_id,
            ))

    if normalized_type in {"all", "cambio", "anulacion"}:
        query = (
            db.query(models.SaleChange, models.Sale)
            .join(models.Sale, models.Sale.id == models.SaleChange.sale_id)
            .filter(_tenant_clause(models.SaleChange.tenant_id, tenant_id))
        )
        query = _range(query, models.SaleChange.created_at, date_from, date_to)
        if normalized_type == "anulacion":
            query = query.filter(models.SaleChange.status == "voided")
        if cleaned_term:
            query = query.filter(or_(
                _contains(cleaned_term, models.SaleChange.document_number, models.Sale.document_number, models.SaleChange.notes),
                models.SaleChange.items_returned.any(_contains(
                    cleaned_term,
                    models.SaleChangeReturnItem.product_name,
                    models.SaleChangeReturnItem.product_sku,
                )),
                models.SaleChange.items_new.any(_contains(
                    cleaned_term,
                    models.SaleChangeNewItem.product_name,
                    models.SaleChangeNewItem.product_sku,
                )),
            ))
        if cleaned_customer:
            query = query.filter(models.Sale.customer_name.ilike(f"%{cleaned_customer}%"))
        if cleaned_pos:
            query = query.filter(models.SaleChange.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            query = query.filter(models.SaleChange.seller_name.ilike(f"%{cleaned_vendor}%"))
        if cleaned_payment == "mixed":
            payment_count = (
                db.query(func.count(models.SaleChangePayment.id))
                .filter(models.SaleChangePayment.change_id == models.SaleChange.id)
                .correlate(models.SaleChange)
                .scalar_subquery()
            )
            query = query.filter(payment_count > 1)
        elif cleaned_payment == "separated":
            query = query.filter(false())
        elif cleaned_payment:
            query = query.filter(models.SaleChange.payments.any(
                func.lower(models.SaleChangePayment.method) == cleaned_payment
            ))
        for change, sale in query.order_by(models.SaleChange.created_at.desc(), models.SaleChange.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"change-{change.id}", document_type="cambio", record_id=change.id,
                occurred_at=change.created_at,
                document_number=change.document_number or f"CB-{change.id:06d}",
                reference=f"Ref. {sale.document_number or sale.id}", detail="Cambio registrado",
                total=change.extra_payment - change.refund_due, sale_id=sale.id,
                customer=sale.customer_name, pos=change.pos_name or sale.pos_name,
                vendor=change.seller_name or sale.vendor_name, status=change.status,
                closure_id=change.closure_id,
            ))

    if normalized_type in {"all", "abono"}:
        initial = (
            db.query(models.SeparatedOrder, models.Sale)
            .join(models.Sale, models.Sale.id == models.SeparatedOrder.sale_id)
            .filter(_tenant_clause(models.SeparatedOrder.tenant_id, tenant_id))
            .filter(models.SeparatedOrder.initial_payment > 0)
        )
        initial = _range(initial, models.SeparatedOrder.created_at, date_from, date_to)
        if cleaned_term:
            initial = initial.filter(_contains(cleaned_term, models.SeparatedOrder.sale_document_number, models.SeparatedOrder.customer_name, models.SeparatedOrder.notes))
        if cleaned_payment == "mixed":
            payment_count = (
                db.query(func.count(models.SalePayment.id))
                .filter(models.SalePayment.sale_id == models.Sale.id)
                .correlate(models.Sale)
                .scalar_subquery()
            )
            initial = initial.filter(payment_count > 1)
        elif cleaned_payment not in {"", "separated"}:
            initial = initial.filter(or_(func.lower(models.Sale.main_payment_method) == cleaned_payment, func.lower(models.Sale.payment_method) == cleaned_payment))
        if cleaned_customer:
            initial = initial.filter(models.SeparatedOrder.customer_name.ilike(f"%{cleaned_customer}%"))
        if cleaned_pos:
            initial = initial.filter(models.Sale.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            initial = initial.filter(models.Sale.vendor_name.ilike(f"%{cleaned_vendor}%"))
        for order, sale in initial.order_by(models.SeparatedOrder.created_at.desc(), models.SeparatedOrder.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"abono-initial-{sale.id}", document_type="abono", record_id=sale.id,
                occurred_at=order.created_at, document_number=f"ABI-{sale.id:06d}",
                reference=f"Venta separada {order.sale_document_number}", detail=f"Abono inicial {order.sale_document_number}",
                total=order.initial_payment, payment_method=sale.main_payment_method or sale.payment_method,
                payment_stage="initial", sale_id=sale.id, customer=order.customer_name,
                pos=sale.pos_name, vendor=sale.vendor_name, status=sale.status, closure_id=sale.closure_id,
            ))

        payments = (
            db.query(models.SeparatedOrderPayment, models.SeparatedOrder, models.Sale)
            .join(models.SeparatedOrder, models.SeparatedOrder.id == models.SeparatedOrderPayment.separated_order_id)
            .join(models.Sale, models.Sale.id == models.SeparatedOrder.sale_id)
            .filter(_tenant_clause(models.SeparatedOrderPayment.tenant_id, tenant_id))
            .filter(models.SeparatedOrderPayment.status != "voided")
        )
        payments = _range(payments, models.SeparatedOrderPayment.paid_at, date_from, date_to)
        if cleaned_term:
            payments = payments.filter(_contains(cleaned_term, models.SeparatedOrder.sale_document_number, models.SeparatedOrder.customer_name, models.SeparatedOrderPayment.note, models.SeparatedOrderPayment.reference))
        if cleaned_payment == "mixed":
            payments = payments.filter(false())
        elif cleaned_payment not in {"", "separated"}:
            payments = payments.filter(func.lower(models.SeparatedOrderPayment.method) == cleaned_payment)
        if cleaned_customer:
            payments = payments.filter(models.SeparatedOrder.customer_name.ilike(f"%{cleaned_customer}%"))
        if cleaned_pos:
            payments = payments.filter(models.Sale.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            payments = payments.filter(models.Sale.vendor_name.ilike(f"%{cleaned_vendor}%"))
        for payment, order, sale in payments.order_by(models.SeparatedOrderPayment.paid_at.desc(), models.SeparatedOrderPayment.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"abono-{payment.id}", document_type="abono", record_id=payment.id,
                occurred_at=payment.paid_at, document_number=f"AB-{payment.id:06d}",
                reference=f"Separado {order.sale_document_number}", detail=f"Abono posterior {order.sale_document_number}",
                total=payment.amount, payment_method=payment.method, payment_stage="posterior",
                sale_id=sale.id, customer=order.customer_name, pos=sale.pos_name,
                vendor=sale.vendor_name, status=payment.status, closure_id=payment.closure_id,
            ))

    if normalized_type in {"all", "cierre"} and not cleaned_customer and not cleaned_payment:
        query = db.query(models.PosClosure).filter(_tenant_clause(models.PosClosure.tenant_id, tenant_id))
        query = _range(query, models.PosClosure.closed_at, date_from, date_to)
        if cleaned_term:
            query = query.filter(_contains(cleaned_term, models.PosClosure.consecutive, models.PosClosure.notes))
        if cleaned_pos:
            query = query.filter(models.PosClosure.pos_name.ilike(f"%{cleaned_pos}%"))
        if cleaned_vendor:
            query = query.filter(models.PosClosure.closed_by_user_name.ilike(f"%{cleaned_vendor}%"))
        for closure in query.order_by(models.PosClosure.closed_at.desc(), models.PosClosure.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"closure-{closure.id}", document_type="cierre", record_id=closure.id,
                occurred_at=closure.closed_at, document_number=closure.consecutive or f"CL-{closure.id:06d}",
                reference=f"Reporte Z - {closure.pos_name or 'POS'}", detail=f"Cierre de caja {closure.pos_name or 'POS'}",
                total=closure.net_amount, payment_method="cierre", pos=closure.pos_name,
                vendor=closure.closed_by_user_name,
            ))

    manual_kind = MANUAL_TYPES.get(normalized_type)
    if normalized_type in {"all", "movimiento_manual", *MANUAL_TYPES.keys()} and not cleaned_customer and not cleaned_payment:
        query = db.query(models.ManualMovementDocument).filter(_tenant_clause(models.ManualMovementDocument.tenant_id, tenant_id)).filter(models.ManualMovementDocument.status == "closed")
        query = _range(query, models.ManualMovementDocument.closed_at, date_from, date_to)
        if manual_kind:
            query = query.filter(models.ManualMovementDocument.kind == manual_kind)
        if cleaned_term:
            query = query.filter(or_(
                _contains(cleaned_term, models.ManualMovementDocument.document_number, models.ManualMovementDocument.notes),
                models.ManualMovementDocument.lines.any(_contains(
                    cleaned_term,
                    models.ManualMovementDocumentLine.product_name_snapshot,
                    models.ManualMovementDocumentLine.sku_snapshot,
                )),
            ))
        if cleaned_pos:
            query = query.filter(models.ManualMovementDocument.origin_name.ilike(f"%{cleaned_pos}%"))
        for doc in query.order_by(models.ManualMovementDocument.closed_at.desc(), models.ManualMovementDocument.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"manual-movement-{doc.id}", document_type="movimiento_manual", record_id=doc.id,
                occurred_at=doc.closed_at or doc.created_at, document_number=doc.document_number or f"MM-{doc.id:06d}",
                reference=f"Movimiento manual - {doc.origin_name}", detail=doc.notes or doc.kind,
                payment_method="movimiento_manual", pos=doc.origin_name, status=doc.status,
            ))

    if normalized_type in {"all", "recepcion"} and not cleaned_customer and not cleaned_payment and not cleaned_vendor:
        query = db.query(models.ReceivingLot).filter(_tenant_clause(models.ReceivingLot.tenant_id, tenant_id)).filter(models.ReceivingLot.status == "closed")
        query = _range(query, models.ReceivingLot.closed_at, date_from, date_to)
        if cleaned_term:
            query = query.filter(or_(
                _contains(cleaned_term, models.ReceivingLot.lot_number, models.ReceivingLot.invoice_reference, models.ReceivingLot.supplier_name, models.ReceivingLot.notes),
                models.ReceivingLot.items.any(_contains(
                    cleaned_term,
                    models.ReceivingLotItem.product_name_snapshot,
                    models.ReceivingLotItem.sku_snapshot,
                )),
            ))
        if cleaned_pos:
            query = query.filter(models.ReceivingLot.origin_name.ilike(f"%{cleaned_pos}%"))
        for lot in query.order_by(models.ReceivingLot.closed_at.desc(), models.ReceivingLot.id.desc()).limit(scan_limit):
            rows.append(_base_item(
                key=f"receiving-{lot.id}", document_type="recepcion", record_id=lot.id,
                occurred_at=lot.closed_at or lot.created_at, document_number=lot.lot_number or f"RC-{lot.id:06d}",
                reference=f"Recepción - {lot.origin_name}", detail=lot.invoice_reference or lot.notes or "Recepción de inventario",
                payment_method="recepcion", pos=lot.origin_name, status=lot.status,
            ))

    if normalized_type in {"all", "recuento"} and not cleaned_customer and not cleaned_payment:
        query = db.query(models.InventoryRecount).filter(_tenant_clause(models.InventoryRecount.tenant_id, tenant_id)).filter(models.InventoryRecount.status.in_(["closed", "applied"]))
        operation_at = func.coalesce(models.InventoryRecount.applied_at, models.InventoryRecount.closed_at, models.InventoryRecount.created_at)
        query = _range(query, operation_at, date_from, date_to)
        if cleaned_term:
            query = query.filter(or_(
                _contains(cleaned_term, models.InventoryRecount.code, models.InventoryRecount.title, models.InventoryRecount.notes),
                models.InventoryRecount.lines.any(_contains(
                    cleaned_term,
                    models.InventoryRecountLine.product_name_snapshot,
                    models.InventoryRecountLine.sku_snapshot,
                )),
            ))
        if cleaned_pos:
            query = query.filter(models.InventoryRecount.source.ilike(f"%{cleaned_pos}%"))
        for recount in query.order_by(operation_at.desc(), models.InventoryRecount.id.desc()).limit(scan_limit):
            occurred_at = recount.applied_at or recount.closed_at or recount.created_at
            rows.append(_base_item(
                key=f"recount-{recount.id}", document_type="recuento", record_id=recount.id,
                occurred_at=occurred_at, document_number=recount.code or f"RCN-{recount.id:06d}",
                reference=f"Recuento - {recount.source}", detail=recount.title or recount.notes or "Recuento de inventario",
                pos="Metrik Stock App" if recount.source == "app" else "Metrik Web", status=recount.status,
            ))

    rows.sort(key=lambda row: (row["occurred_at"], row["id"]), reverse=True)
    page = rows[skip : skip + limit]
    _attach_content_summaries(db, page, tenant_id)
    return page, len(rows) > skip + limit
