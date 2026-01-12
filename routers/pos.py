from datetime import datetime
from html import escape
from typing import List, Optional
import base64
import os

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Response,
)
from sqlalchemy.orm import Session
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import schemas, crud, models
from database import get_db
from dependencies import require_permission
from services import email as email_service
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


def _station_to_read(station: models.PosStation) -> schemas.PosStationRead:
    email = ""
    if station and station.user:
        email = station.user.email
    return schemas.PosStationRead(
        id=station.id,
        label=station.label,
        pos_user_email=email,
        is_active=bool(station.is_active),
        last_login_at=station.last_login_at,
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


@router.post("/sales", response_model=schemas.SaleRead, status_code=201)
def create_sale(
    sale_in: schemas.SaleCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("pos.sales")),
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

    try:
        sale = crud.create_sale(db, sale_in)
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
    _: models.PosUser = Depends(require_permission("settings.manage")),
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

    closure_html = ticket_renderer.render_closure_html(closure)
    body_parts = []
    if email_in.message:
        body_parts.append(f"<p>{escape(email_in.message)}</p>")
    body_parts.append(closure_html)

    attachments = []
    if email_in.attach_pdf:
        pdf_bytes = ticket_renderer.render_closure_pdf(closure)
        attachments.append(
            (
                f"cierre_{closure.consecutive or closure.id}.pdf",
                pdf_bytes,
                "application/pdf",
            )
        )

    subject = (
        email_in.subject
        or f"Reporte Z {closure.consecutive or f'CL-{closure.id:06d}'}"
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
    closure = crud.get_pos_closure(db, closure_id)
    if not closure:
        raise HTTPException(status_code=404, detail="Cierre no encontrado")
    crud.delete_pos_closure(db, closure)
