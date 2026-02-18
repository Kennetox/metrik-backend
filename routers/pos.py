from datetime import datetime, timezone
from html import escape
from typing import List, Optional
import base64
import os
import unicodedata
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    Response,
)
from sqlalchemy.orm import Session
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import schemas, crud, models
from database import get_db
from dependencies import get_current_active_user, require_permission, require_role
from services import email as email_service
from services import pdf_utils
from services import ticket_renderer
from services import storage
from services.password_reset import (
    PASSWORD_RESET_TOKEN_TTL_SECONDS,
    build_reset_link,
    generate_token_and_expiry,
)


router = APIRouter(
    prefix="/pos",
    tags=["pos"],
)

FREE_SALE_NAME_FRAGMENT = "venta libre"
FREE_SALE_REASON_LABEL = "motivo venta libre"
FREE_SALE_REASON_REQUIRED = (
    os.getenv("FREE_SALE_REASON_REQUIRED", "true").strip().lower()
    not in {"0", "false", "no", "off"}
)


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()


def _sale_contains_free_sale(sale_in: schemas.SaleCreate) -> bool:
    for item in sale_in.items or []:
        name = _normalize_text(getattr(item, "product_name", ""))
        sku = _normalize_text(getattr(item, "product_sku", ""))
        if FREE_SALE_NAME_FRAGMENT in name or "venta-libre" in sku or "venta libre" in sku:
            return True
    return False


def _has_required_free_sale_reason(notes: Optional[str]) -> bool:
    normalized_notes = _normalize_text(notes)
    if not normalized_notes:
        return False
    label_index = normalized_notes.find(FREE_SALE_REASON_LABEL)
    if label_index < 0:
        return False
    tail = normalized_notes[label_index + len(FREE_SALE_REASON_LABEL) :].strip(" :\n\t\r-")
    return bool(tail)


def _load_qz_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise HTTPException(
            status_code=500,
            detail=f"Falta configurar {name} en el servidor.",
        )
    value = value.replace("\\n", "\n")
    if "-----BEGIN" not in value:
        try:
            value = base64.b64decode(value).decode("utf-8")
        except Exception:
            pass
    return value


def _get_qz_cert() -> str:
    return _load_qz_env("QZ_CERT")


def _get_qz_private_key() -> str:
    return _load_qz_env("QZ_PRIVATE_KEY")


def _get_qz_signature_hash() -> hashes.HashAlgorithm:
    algo = os.getenv("QZ_SIGNATURE_ALGO", "sha256").strip().lower()
    if algo in ("sha1", "sha-1"):
        return hashes.SHA1()
    if algo in ("sha256", "sha-256"):
        return hashes.SHA256()
    raise HTTPException(
        status_code=500,
        detail=f"Algoritmo de firma QZ invalido: {algo}. Usa sha256 o sha1.",
    )


def _sign_qz_payload(payload: str) -> str:
    private_key_pem = _get_qz_private_key()
    try:
        hash_algo = _get_qz_signature_hash()
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        signature = private_key.sign(
            payload.encode("utf-8"),
            padding.PKCS1v15(),
            hash_algo,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo firmar el reto de QZ: {exc}",
        ) from exc
    return base64.b64encode(signature).decode("utf-8")


def _to_bogota_date(dt: datetime) -> datetime.date:
    bogota_tz = ZoneInfo("America/Bogota")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(bogota_tz).date()


def _sum_payments(payments: list[tuple[str, float]]) -> float:
    return sum(amount for _, amount in payments)


def _station_to_read(station: models.PosStation) -> schemas.PosStationRead:
    email = station.station_email if station else None
    return schemas.PosStationRead(
        id=station.id,
        label=station.label,
        station_email=email,
        is_active=bool(station.is_active),
        last_login_at=station.last_login_at,
        bound_device_id=station.bound_device_id,
        bound_device_label=station.bound_device_label,
        bound_at=station.bound_at,
        bound_by_user_id=station.bound_by_user_id,
        bound_by_user_name=station.bound_by_user_name,
        printer_mode=station.printer_mode,
        printer_name=station.printer_name,
        printer_width=station.printer_width,
        printer_auto_open_drawer=station.printer_auto_open_drawer,
        printer_show_drawer_button=station.printer_show_drawer_button,
        created_at=station.created_at,
        updated_at=station.updated_at,
    )


def _station_to_response(
    station: models.PosStation,
    pin_plain: Optional[str] = None,
) -> schemas.PosStationResponse:
    data = _station_to_read(station).model_dump()
    data["pin_plain"] = pin_plain
    return schemas.PosStationResponse(**data)


def _station_printer_config(
    station: models.PosStation,
) -> schemas.PosStationPrinterConfigRead:
    return schemas.PosStationPrinterConfigRead(
        printer_mode=station.printer_mode,
        printer_name=station.printer_name,
        printer_width=station.printer_width,
        printer_auto_open_drawer=station.printer_auto_open_drawer,
        printer_show_drawer_button=station.printer_show_drawer_button,
    )


def _serialize_sale_response(sale: models.Sale) -> schemas.SaleRead:
    sale_schema = schemas.SaleRead.model_validate(sale)
    updates = {}

    order = getattr(sale, "separated_order", None)
    if order:
        updates["is_separated"] = True
        order_total = float(order.total_amount or sale_schema.total or 0.0)
        updates["total"] = order_total
        updates["balance"] = float(order.balance or 0.0)
        updates["initial_payment_amount"] = float(
            order.initial_payment
            or sale_schema.initial_payment_amount
            or 0.0
        )
        updates["initial_payment_method"] = (
            sale.initial_payment_method or sale_schema.initial_payment_method
        )
        cart_value = float(sale.cart_discount_value or 0.0)
        cart_percent = float(sale.cart_discount_percent or 0.0)
        if abs(cart_value - updates["balance"]) < 0.01:
            cart_value = 0.0
            cart_percent = 0.0
        updates["cart_discount_value"] = cart_value
        updates["cart_discount_percent"] = cart_percent
    else:
        updates["balance"] = None
        updates["initial_payment_method"] = (
            sale.initial_payment_method or sale_schema.initial_payment_method
        )
        updates["initial_payment_amount"] = (
            sale.initial_payment_amount or sale_schema.initial_payment_amount
        )

    if not order:
        updates.setdefault("total", sale_schema.total)

    cash_methods = {"cash", "efectivo"}
    has_cash_payment = False
    for payment in getattr(sale, "payments", []) or []:
        method = (payment.method or "").strip().lower()
        if method in cash_methods and float(payment.amount or 0.0) > 0:
            has_cash_payment = True
            break
    if not has_cash_payment:
        method = (sale.main_payment_method or sale.payment_method or "").strip().lower()
        if method in cash_methods and float(sale.paid_amount or 0.0) > 0:
            has_cash_payment = True
    updates["has_cash_payment"] = has_cash_payment

    return sale_schema.model_copy(update=updates)


@router.get(
    "/payment-methods",
    response_model=List[schemas.PaymentMethodRead],
)
def list_payment_methods(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.payment_methods.view")),
):
    methods = crud.list_payment_methods(db)
    return methods


@router.post(
    "/payment-methods",
    response_model=schemas.PaymentMethodRead,
    status_code=201,
)
def create_payment_method(
    payload: schemas.PaymentMethodCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    try:
        return crud.create_payment_method(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/payment-methods/{method_id}",
    response_model=schemas.PaymentMethodRead,
)
def update_payment_method(
    method_id: int,
    payload: schemas.PaymentMethodUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    method = crud.get_payment_method(db, method_id)
    if not method or method.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Método no encontrado")
    try:
        return crud.update_payment_method(db, method, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch(
    "/payment-methods/{method_id}/toggle",
    response_model=schemas.PaymentMethodRead,
)
def toggle_payment_method(
    method_id: int,
    payload: schemas.PaymentMethodToggleRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    method = crud.get_payment_method(db, method_id)
    if not method or method.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Método no encontrado")
    try:
        return crud.toggle_payment_method(db, method, payload.is_active)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch(
    "/payment-methods/reorder",
    response_model=List[schemas.PaymentMethodRead],
)
def reorder_payment_methods(
    payload: schemas.PaymentMethodReorderRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Debes enviar la nueva orden")
    try:
        updated = crud.reorder_payment_methods(db, payload.items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.delete("/payment-methods/{method_id}", status_code=204)
def delete_payment_method(
    method_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.payment_methods")),
):
    method = crud.get_payment_method(db, method_id)
    if not method or method.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Método no encontrado")
    try:
        crud.delete_payment_method(db, method)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get(
    "/sales/next-number",
    response_model=schemas.NextSaleNumberResponse,
)
def get_next_sale_number(
    pos_id: Optional[str] = None,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    """Devuelve el siguiente consecutivo disponible."""

    next_number = crud.get_next_sale_number(db, pos_id=pos_id)
    return schemas.NextSaleNumberResponse(next_sale_number=next_number)


@router.post(
    "/sales/reserve-number",
    response_model=schemas.SaleNumberReservationResponse,
    status_code=201,
)
def reserve_sale_number(
    payload: schemas.SaleNumberReservationRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    """Reserva un consecutivo para una venta nueva."""
    try:
        reservation = crud.reserve_sale_number(
            db,
            pos_name=payload.pos_name,
            station_id=payload.station_id,
            reserved_by_user_id=current_user.id,
            min_sale_number=payload.min_sale_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.SaleNumberReservationResponse(
        reservation_id=reservation.id,
        sale_number=reservation.sale_number,
        document_number=reservation.document_number,
        status=reservation.status,
    )


@router.post(
    "/sales/reservations/{reservation_id}/cancel",
    response_model=schemas.SaleNumberReservationResponse,
)
def cancel_sale_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    try:
        reservation = crud.cancel_sale_reservation(db, reservation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.SaleNumberReservationResponse(
        reservation_id=reservation.id,
        sale_number=reservation.sale_number,
        document_number=reservation.document_number,
        status=reservation.status,
    )


@router.post("/sales", response_model=schemas.SaleRead, status_code=201)
def create_sale(
    sale_in: schemas.SaleCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    """
    Crea una venta en el POS.

    - Debe tener al menos un ítem.
    - paid_amount no puede ser negativo.
    - El detalle de cómo se calculan los totales y se guardan
      los SaleItem y SalePayment está en crud.create_sale.
    """
    if not sale_in.items or len(sale_in.items) == 0:
        raise HTTPException(
            status_code=400,
            detail="La venta debe tener al menos un ítem",
        )

    if sale_in.paid_amount < 0:
        raise HTTPException(
            status_code=400,
            detail="paid_amount no puede ser negativo",
        )
    if (
        FREE_SALE_REASON_REQUIRED
        and _sale_contains_free_sale(sale_in)
        and not _has_required_free_sale_reason(
        sale_in.notes
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "La nota debe incluir el motivo de venta libre cuando se use este producto."
            ),
        )

    try:
        sale = crud.create_sale(db, sale_in, created_by_user_id=current_user.id)
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "ticket" in message.lower() and "existe" in message.lower() else 400
        raise HTTPException(status_code=status_code, detail=message) from exc

    return _serialize_sale_response(sale)


@router.get("/sales", response_model=List[schemas.SaleRead])
def list_sales(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    """
    Lista las ventas registradas en el POS.
    Más adelante se puede ampliar con filtros, paginación real, etc.
    """
    sales = crud.get_sales(db, skip=skip, limit=limit)
    return [_serialize_sale_response(sale) for sale in sales]


@router.get("/sales/{sale_id}", response_model=schemas.SaleRead)
def get_sale(
    sale_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    sale = crud.get_sale(db, sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return _serialize_sale_response(sale)


@router.post(
    "/sales/{sale_id}/void",
    response_model=schemas.SaleVoidResponse,
)
def void_sale(
    sale_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_role(["Administrador"])),
):
    sale = crud.get_sale(db, sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    try:
        sale = crud.void_sale(db, sale, current_user, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.refresh(sale)
    return schemas.SaleVoidResponse(
        sale=_serialize_sale_response(sale),
        return_document=None,
    )


@router.post(
    "/documents/{doc_type}/{doc_id}/adjust",
    response_model=schemas.DocumentAdjustmentRead,
    status_code=201,
)
def create_document_adjustment(
    doc_type: str,
    doc_id: int,
    payload: schemas.DocumentAdjustmentCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_role(["Administrador"])),
):
    if doc_type != "sale":
        raise HTTPException(status_code=400, detail="Tipo de documento no soportado")

    sale = crud.get_sale(db, doc_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    if sale.status == "voided":
        raise HTTPException(status_code=400, detail="La venta está anulada")

    if _to_bogota_date(sale.created_at) != _to_bogota_date(
        datetime.utcnow().replace(tzinfo=timezone.utc)
    ):
        raise HTTPException(
            status_code=400,
            detail="Los ajustes solo se permiten el mismo día de la venta",
        )

    total_delta = float(payload.total_delta or 0.0)
    payment_delta = float(payload.payment_delta or 0.0)
    adjustment_type = payload.adjustment_type
    payload_payments = payload.payload.get("payments") if payload.payload else None
    payments_list: list[tuple[str, float]] = []
    if isinstance(payload_payments, list):
        for entry in payload_payments:
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
            payments_list.append((method, numeric))

    if adjustment_type == "note":
        if abs(total_delta) > 0.01 or abs(payment_delta) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="Las notas no pueden modificar valores de pago o total",
            )
        if not payload.payload or not payload.payload.get("note"):
            raise HTTPException(
                status_code=400,
                detail="La nota es obligatoria para este ajuste",
            )
    elif adjustment_type == "discount":
        if abs(total_delta - payment_delta) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="El descuento debe cuadrar con el ajuste de pagos",
            )
        if not payments_list:
            raise HTTPException(
                status_code=400,
                detail="Debes ajustar los pagos junto con el descuento",
            )
    if payments_list:
        original_total = sum(float(p.amount or 0.0) for p in sale.payments)
        if original_total <= 0:
            original_total = float(sale.paid_amount or sale.total or 0.0)
        expected_total = original_total + payment_delta
        adjusted_total = _sum_payments(payments_list)
        if abs(adjusted_total - expected_total) > 0.01:
            raise HTTPException(
                status_code=400,
                detail="El total de pagos ajustados no cuadra con el ajuste",
            )

    is_post_closure = sale.closure_id is not None
    adjustment = crud.create_document_adjustment(
        db,
        doc_type=doc_type,
        doc_id=doc_id,
        adjustment_type=adjustment_type,
        reason=payload.reason,
        payload=payload.payload or {},
        total_delta=total_delta,
        payment_delta=payment_delta,
        is_post_closure=is_post_closure,
        original_closure_id=sale.closure_id,
        user=current_user,
    )
    return adjustment


@router.get(
    "/documents/{doc_type}/{doc_id}/adjustments",
    response_model=List[schemas.DocumentAdjustmentRead],
)
def list_document_adjustments(
    doc_type: str,
    doc_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    if doc_type != "sale":
        raise HTTPException(status_code=400, detail="Tipo de documento no soportado")
    sale = crud.get_sale(db, doc_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return crud.list_document_adjustments(db, doc_type=doc_type, doc_id=doc_id)


@router.get(
    "/documents/adjustments",
    response_model=List[schemas.DocumentAdjustmentRead],
)
def list_document_adjustments_bulk(
    doc_type: str,
    doc_ids: str,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    if doc_type != "sale":
        raise HTTPException(status_code=400, detail="Tipo de documento no soportado")
    try:
        ids = [int(item) for item in doc_ids.split(",") if item.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="doc_ids inválidos") from exc
    if not ids:
        return []
    return crud.list_document_adjustments_for_docs(db, doc_type=doc_type, doc_ids=ids)


@router.post("/returns", response_model=schemas.SaleReturnRead, status_code=201)
def create_return(
    return_in: schemas.SaleReturnCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.returns")),
):
    """Registra una devolución parcial o total vinculada a una venta."""

    try:
        sale_return = crud.create_return(db, return_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return sale_return


@router.post("/changes", response_model=schemas.SaleChangeRead, status_code=201)
def create_change(
    change_in: schemas.SaleChangeCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.returns")),
):
    """Registra un cambio de productos vinculado a una venta."""

    try:
        sale_change = crud.create_change(db, change_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return sale_change


@router.post(
    "/sales/{sale_id}/email",
    response_model=schemas.EmailSendResponse,
)
def email_sale_ticket(
    sale_id: int,
    email_in: schemas.EmailSendRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.sales")
    ),
):
    sale = crud.get_sale(db, sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    recipients = list(email_in.recipients or [])
    if not recipients:
        raise HTTPException(
            status_code=400, detail="Debe especificar al menos un destinatario"
        )

    settings = crud.get_pos_settings(db)
    ticket_html = ticket_renderer.render_sale_ticket_html(
        sale,
        settings=settings,
    )
    body_parts = []
    if email_in.message:
        body_parts.append(f"<p>{escape(email_in.message)}</p>")
    body_parts.append(ticket_html)

    attachments = []
    if email_in.attach_pdf:
        pdf_bytes = ticket_renderer.render_sale_ticket_pdf(
            sale,
            settings=settings,
        )
        attachments.append(
            (
                f"ticket_{sale.sale_number or sale.id}.pdf",
                pdf_bytes,
                "application/pdf",
            )
        )

    cc = list(settings.ticket_email_cc or [])

    subject = (
        email_in.subject
        or f"Ticket venta #{sale.sale_number or sale.id}"
    )

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            html_body="".join(body_parts),
            cc=cc,
            attachments=attachments,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return schemas.EmailSendResponse(status="sent")


@router.get("/returns", response_model=List[schemas.SaleReturnRead])
def list_returns(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.returns")),
):
    returns = crud.list_returns(db, skip=skip, limit=limit)
    return returns


@router.get("/changes", response_model=List[schemas.SaleChangeRead])
def list_changes(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.returns")),
):
    changes = crud.list_changes(db, skip=skip, limit=limit)
    return changes


@router.get("/returns/{return_id}", response_model=schemas.SaleReturnRead)
def get_return(
    return_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.returns")),
):
    sale_return = crud.get_sale_return(db, return_id)
    if not sale_return:
        raise HTTPException(status_code=404, detail="Devolución no encontrada")
    return sale_return


@router.post(
    "/returns/{return_id}/void",
    response_model=schemas.SaleReturnRead,
)
def void_return(
    return_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_role(["Administrador"])),
):
    sale_return = crud.get_sale_return(db, return_id)
    if not sale_return:
        raise HTTPException(status_code=404, detail="Devolución no encontrada")
    try:
        updated = crud.void_return(db, sale_return, current_user, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.get("/changes/{change_id}", response_model=schemas.SaleChangeRead)
def get_change(
    change_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.returns")),
):
    sale_change = crud.get_sale_change(db, change_id)
    if not sale_change:
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    return sale_change


@router.post(
    "/changes/{change_id}/void",
    response_model=schemas.SaleChangeRead,
)
def void_change(
    change_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_role(["Administrador"])),
):
    sale_change = crud.get_sale_change(db, change_id)
    if not sale_change:
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    try:
        updated = crud.void_change(db, sale_change, current_user, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.get("/settings", response_model=schemas.PosSettingsRead)
def get_pos_settings(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.view")),
):
    settings = crud.get_pos_settings(db)
    return settings


@router.put("/settings", response_model=schemas.PosSettingsRead)
def update_pos_settings(
    settings_in: schemas.PosSettingsUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.manage")),
):
    settings = crud.get_pos_settings(db)
    updated = crud.update_pos_settings(db, settings, settings_in)
    return updated


@router.post("/settings/test-email", response_model=schemas.EmailSendResponse)
def send_settings_test_email(
    payload: schemas.SmtpTestEmailRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.manage")),
):
    recipients = list(payload.recipients or [])
    if not recipients:
        raise HTTPException(status_code=400, detail="Agrega al menos un destinatario")

    settings = crud.get_pos_settings(db)
    smtp_config = {
        "smtp_host": payload.smtp_host or settings.smtp_host,
        "smtp_port": payload.smtp_port or settings.smtp_port,
        "smtp_user": payload.smtp_user or settings.smtp_user,
        "smtp_password": payload.smtp_password or settings.smtp_password,
        "smtp_use_tls": (
            payload.smtp_use_tls
            if payload.smtp_use_tls is not None
            else settings.smtp_use_tls
        ),
        "email_from": payload.email_from or settings.email_from,
    }
    subject = payload.subject or "Prueba de correo - Kensar POS"
    message = payload.message or "Este es un correo de prueba del POS."

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            body=f"<p>{escape(message)}</p>",
            smtp_config=smtp_config,
        )
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return schemas.EmailSendResponse(status="sent")


@router.get("/qz/cert")
def get_qz_certificate(
    db: Session = Depends(get_db),
):
    cert = _get_qz_cert()
    return Response(content=cert, media_type="text/plain")


@router.post("/qz/sign", response_model=schemas.QzSignResponse)
def sign_qz_request(
    payload: schemas.QzSignRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    signature = _sign_qz_payload(payload.data)
    return schemas.QzSignResponse(signature=signature)


@router.post("/logo-upload", response_model=schemas.UploadLogoResponse)
@router.post("/settings/logo", response_model=schemas.UploadLogoResponse)
async def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.manage")),
):
    try:
        result = await storage.save_pos_logo(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar el logo: {exc}") from exc

    settings = crud.get_pos_settings(db)
    settings.logo_url = result.url
    settings.ticket_logo_url = result.url
    db.commit()
    db.refresh(settings)
    return schemas.UploadLogoResponse(url=result.url)


@router.get(
    "/roles/permissions",
    response_model=schemas.RolePermissionMatrix,
)
def get_role_permissions(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.view")),
):
    modules = crud.get_role_permissions(db)
    return schemas.RolePermissionMatrix(modules=modules)


@router.put(
    "/roles/permissions",
    response_model=schemas.RolePermissionMatrix,
)
def update_role_permissions(
    payload: schemas.RolePermissionMatrix,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("settings.manage")),
):
    modules_payload = [module.model_dump() for module in payload.modules]
    modules = crud.update_role_permissions(db, modules_payload)
    return schemas.RolePermissionMatrix(modules=modules)


@router.get("/profile", response_model=schemas.PosUserProfileRead)
def get_profile(
    current_user: models.PosUser = Depends(get_current_active_user),
):
    return schemas.PosUserProfileRead.model_validate(current_user)


@router.patch("/profile", response_model=schemas.PosUserProfileRead)
def update_profile(
    payload: schemas.PosUserProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="El nombre es obligatorio.")
        data["name"] = name

    for field, value in data.items():
        setattr(current_user, field, value)

    db.commit()
    db.refresh(current_user)
    return schemas.PosUserProfileRead.model_validate(current_user)


@router.post("/profile/avatar", response_model=schemas.UploadAvatarResponse)
async def upload_profile_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    try:
        result = await storage.save_user_avatar(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar la imagen: {exc}") from exc

    current_user.avatar_url = result.url
    db.commit()
    db.refresh(current_user)
    return schemas.UploadAvatarResponse(url=result.url)


@router.get("/profile/documents", response_model=List[schemas.PosUserDocumentRead])
def list_profile_documents(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    return crud.list_user_documents(db, current_user.id)


@router.post("/profile/documents", response_model=schemas.PosUserDocumentRead, status_code=201)
async def upload_profile_document(
    file: UploadFile = File(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    existing = crud.list_user_documents(db, current_user.id)
    if len(existing) >= 10:
        raise HTTPException(status_code=400, detail="Se alcanzó el límite de 10 documentos.")
    try:
        result = await storage.save_user_document(file, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar el documento: {exc}") from exc

    doc = crud.create_user_document(
        db,
        user_id=current_user.id,
        file_name=result.filename,
        file_url=result.url,
        file_size=result.size,
        note=note.strip() if note else None,
    )
    return doc


@router.delete("/profile/documents/{doc_id}", status_code=204)
def delete_profile_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    deleted = crud.delete_user_document(db, current_user.id, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return Response(status_code=204)


@router.get("/users", response_model=List[schemas.PosUserRead])
def list_pos_users(
    status: Optional[str] = None,
    role: Optional[str] = None,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("users.manage")),
):
    users = crud.list_pos_users(db, status=status, role=role)
    return users


@router.post("/users", response_model=schemas.PosUserRead, status_code=201)
def create_pos_user(
    user_in: schemas.PosUserCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("users.manage")),
):
    try:
        user = crud.create_pos_user(db, user_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user


@router.patch("/users/{user_id}", response_model=schemas.PosUserRead)
def update_pos_user(
    user_id: int,
    user_in: schemas.PosUserUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("users.manage")),
):
    user = crud.get_pos_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    try:
        updated = crud.update_pos_user(db, user, user_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated


@router.post("/users/{user_id}/invite")
def invite_pos_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("users.invite")),
):
    user = crud.get_pos_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not user.email:
        raise HTTPException(status_code=400, detail="El usuario no tiene un correo configurado")

    crud.invalidate_password_reset_tokens(db, user.id)
    token, expires_at = generate_token_and_expiry()
    crud.create_password_reset_token(db, user, token, expires_at)

    user.invited_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    settings = crud.get_pos_settings(db)
    reset_link = build_reset_link(token)
    html_body = (
        f"<p>Hola {user.name or user.email},</p>"
        "<p>Has sido invitado a usar el POS Kensar. "
        "Haz clic en el siguiente enlace para configurar tu contraseña:</p>"
        f"<p><a href='{reset_link}' target='_blank'>Configurar contraseña</a></p>"
        f"<p>El enlace expirará en {PASSWORD_RESET_TOKEN_TTL_SECONDS // 60} minutos.</p>"
    )

    try:
        email_service.send_email(
            recipients=[user.email],
            subject="Invitación a Metrik POS",
            html_body=html_body,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "detail": "Enviamos una invitación al usuario",
        "expires_in": PASSWORD_RESET_TOKEN_TTL_SECONDS,
    }


@router.get(
    "/stations",
    response_model=List[schemas.PosStationRead],
)
def list_pos_stations(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("stations.manage")),
):
    stations = crud.list_pos_stations(db)
    return [_station_to_read(station) for station in stations]


@router.post(
    "/stations",
    response_model=schemas.PosStationResponse,
    status_code=201,
)
def create_pos_station(
    payload: schemas.PosStationCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("stations.manage")),
):
    try:
        station, pin_plain = crud.create_pos_station(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _station_to_response(station, pin_plain)


@router.put(
    "/stations/{station_id}",
    response_model=schemas.PosStationResponse,
)
def update_pos_station(
    station_id: str,
    payload: schemas.PosStationUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("stations.manage")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    station, pin_plain = crud.update_pos_station(db, station, payload)
    return _station_to_response(station, pin_plain)


def _station_notice_to_read(
    notice: models.PosStationNotice,
) -> schemas.PosStationNoticeRead:
    return schemas.PosStationNoticeRead(
        id=notice.id,
        station_id=notice.station_id,
        message=notice.message,
        created_at=notice.created_at,
        created_by_user_name=(
            notice.created_by_user.name if notice.created_by_user else None
        ),
    )


@router.post(
    "/stations/{station_id}/notice",
    response_model=schemas.PosStationNoticeRead,
)
def create_pos_station_notice(
    station_id: str,
    payload: schemas.PosStationNoticeCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("stations.manage")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    notice = crud.create_pos_station_notice(
        db,
        station=station,
        message=payload.message,
        user=current_user,
    )
    return _station_notice_to_read(notice)


@router.get(
    "/stations/{station_id}/notice",
    response_model=Optional[schemas.PosStationNoticeRead],
)
def get_pos_station_notice(
    station_id: str,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    notice = crud.get_active_pos_station_notice(db, station_id)
    if not notice:
        return None
    return _station_notice_to_read(notice)


@router.delete("/stations/{station_id}/notice/{notice_id}", status_code=204)
def dismiss_pos_station_notice(
    station_id: str,
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("pos.sales")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    notice = crud.get_pos_station_notice(db, station_id, notice_id)
    if not notice:
        raise HTTPException(status_code=404, detail="Aviso no encontrado")
    crud.dismiss_pos_station_notice(db, notice, current_user)
    return Response(status_code=204)


@router.get(
    "/stations/{station_id}/printer-config",
    response_model=schemas.PosStationPrinterConfigRead,
)
def get_station_printer_config(
    station_id: str,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    return _station_printer_config(station)


@router.put(
    "/stations/{station_id}/printer-config",
    response_model=schemas.PosStationPrinterConfigRead,
)
def update_station_printer_config(
    station_id: str,
    payload: schemas.PosStationPrinterConfigUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    station = crud.update_pos_station_printer_config(db, station, payload)
    return _station_printer_config(station)


@router.delete("/stations/{station_id}", status_code=204)
def deactivate_pos_station(
    station_id: str,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("stations.manage")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    crud.deactivate_pos_station(db, station)
    return Response(status_code=204)


@router.post(
    "/stations/{station_id}/unbind",
    response_model=schemas.PosStationRead,
)
def unbind_pos_station(
    station_id: str,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("stations.manage")),
):
    station = crud.get_pos_station(db, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Estación no encontrada")
    station.bound_device_id = None
    station.bound_device_label = None
    station.bound_at = None
    station.bound_by_user_id = None
    station.bound_by_user_name = None
    db.commit()
    db.refresh(station)
    return _station_to_read(station)


@router.get("/customers", response_model=List[schemas.PosCustomerRead])
def list_pos_customers(
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.customers")),
):
    customers = crud.list_pos_customers(
        db,
        search=search,
        skip=skip,
        limit=limit,
        include_inactive=include_inactive,
    )
    return customers


@router.get("/customers/frequent", response_model=List[schemas.PosCustomerFrequentRead])
def list_pos_frequent_customers(
    min_sales: int = 5,
    limit: int = 12,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.customers")),
):
    return crud.list_pos_frequent_customers(
        db,
        min_sales=min_sales,
        limit=limit,
    )


@router.post(
    "/customers",
    response_model=schemas.PosCustomerRead,
    status_code=201,
)
def create_pos_customer(
    customer_in: schemas.PosCustomerCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.customers")),
):
    customer = crud.create_pos_customer(db, customer_in)
    return customer


@router.put("/customers/{customer_id}", response_model=schemas.PosCustomerRead)
def update_pos_customer(
    customer_id: int,
    customer_in: schemas.PosCustomerUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.customers")),
):
    customer = crud.get_pos_customer(db, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    updated = crud.update_pos_customer(db, customer, customer_in)
    return updated


@router.delete("/customers/{customer_id}", status_code=204)
def delete_pos_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.customers")),
):
    customer = crud.get_pos_customer(db, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    crud.soft_delete_pos_customer(db, customer)
    return Response(status_code=204)


@router.post(
    "/closures",
    response_model=schemas.PosClosureRead,
    status_code=201,
)
def create_pos_closure(
    closure_in: schemas.PosClosureCreate,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    try:
        closure = crud.create_pos_closure(db, closure_in, current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return closure


@router.post(
    "/closures/{closure_id}/email",
    response_model=schemas.EmailSendResponse,
)
def email_closure_report(
    closure_id: int,
    email_in: schemas.EmailSendRequest,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    closure = crud.get_pos_closure(db, closure_id)
    if not closure:
        raise HTTPException(status_code=404, detail="Cierre no encontrado")

    settings = crud.get_pos_settings(db)
    recipients = list(email_in.recipients or [])
    if not recipients:
        recipients = list(settings.closure_email_recipients or [])

    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="Debe indicar destinatarios o configurar correos por defecto",
        )

    closure_html = ticket_renderer.render_closure_html(closure, settings=settings)
    body_parts = []
    if email_in.message:
        body_parts.append(f"<p>{escape(email_in.message)}</p>")
    body_parts.append(closure_html)

    attachments = []
    if email_in.attach_pdf:
        if not pdf_utils.can_render_html_pdf():
            raise HTTPException(
                status_code=503,
                detail=(
                    "El servidor no tiene habilitada la generacion de PDF HTML "
                    "(dependencias de WeasyPrint faltantes)."
                ),
            )
        pdf_bytes = ticket_renderer.render_closure_pdf(closure, settings=settings)
        attachments.append(
            (
                f"cierre_{closure.consecutive or closure.id}.pdf",
                pdf_bytes,
                "application/pdf",
            )
        )
        if email_in.extra_html_attachments:
            for html_attachment in email_in.extra_html_attachments:
                if not html_attachment.document_html.strip():
                    continue
                filename = (html_attachment.filename or "").strip()
                if not filename:
                    continue
                if not filename.lower().endswith(".pdf"):
                    filename = f"{filename}.pdf"
                pdf_bytes_extra = pdf_utils.build_pdf_from_html(
                    html_attachment.title or filename,
                    html_attachment.document_html,
                )
                attachments.append((filename, pdf_bytes_extra, "application/pdf"))
        else:
            products_pdf = ticket_renderer.render_closure_products_detail_pdf(
                closure, settings=settings
            )
            attachments.append(
                (
                    f"productos_vendidos_detalle_{closure.consecutive or closure.id}.pdf",
                    products_pdf,
                    "application/pdf",
                )
            )
            hourly_pdf = ticket_renderer.render_closure_hourly_sales_pdf(
                closure, settings=settings
            )
            attachments.append(
                (
                    f"ventas_por_hora_{closure.consecutive or closure.id}.pdf",
                    hourly_pdf,
                    "application/pdf",
                )
            )

    subject = (
        email_in.subject
        or (
            f"Cierre del dia {closure.closed_at.strftime('%d/%m/%Y')} - "
            f"{closure.consecutive or f'CL-{closure.id:06d}'}"
        )
    )

    try:
        email_service.send_email(
            recipients=recipients,
            subject=subject,
            html_body="".join(body_parts),
            cc=None,
            attachments=attachments,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return schemas.EmailSendResponse(status="sent")


@router.get(
    "/closures",
    response_model=List[schemas.PosClosureList],
)
def list_pos_closures(
    skip: int = 0,
    limit: int = 100,
    pos_name: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    closures = crud.list_pos_closures(
        db,
        skip=skip,
        limit=limit,
        pos_name=pos_name,
        date_from=date_from,
        date_to=date_to,
    )
    return closures


@router.get(
    "/closures/{closure_id}",
    response_model=schemas.PosClosureRead,
)
def get_pos_closure(
    closure_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(
        require_permission("pos.closures")
    ),
):
    closure = crud.get_pos_closure(db, closure_id)
    if not closure:
        raise HTTPException(status_code=404, detail="Cierre no encontrado")
    return closure


@router.delete(
    "/closures/{closure_id}",
    status_code=204,
)
def delete_pos_closure(
    closure_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.closures")),
):
    raise HTTPException(
        status_code=403,
        detail="Los cierres no se pueden eliminar.",
    )
