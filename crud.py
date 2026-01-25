from collections import defaultdict
from datetime import datetime
import hashlib
import logging
import re
import secrets
import string
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from sqlalchemy import func, or_

from sqlalchemy.orm import Session, selectinload, joinedload

import models, schemas
from services import permissions
from security import hash_password, verify_password


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def revoke_user_sessions(
    db: Session,
    user_id: int,
    reason: str = "replaced",
) -> None:
    now = datetime.utcnow()
    (
        db.query(models.PosSession)
        .filter(
            models.PosSession.user_id == user_id,
            models.PosSession.revoked_at.is_(None),
        )
        .update(
            {
                models.PosSession.revoked_at: now,
                models.PosSession.revoked_reason: reason,
            },
            synchronize_session=False,
        )
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
    session = models.PosSession(
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


# ===================== PRODUCTS =====================


def get_products(db: Session, skip: int = 0, limit: int = 100):
    products = (
        db.query(models.Product)
        .order_by(models.Product.id.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    group_names = {p.group_name for p in products if p.group_name}
    group_map = {}
    if group_names:
        groups = (
            db.query(models.ProductGroup)
            .filter(models.ProductGroup.path.in_(group_names))
            .all()
        )
        group_map = {g.path: g for g in groups}

    for product in products:
        product.group_meta = group_map.get(product.group_name or "")

    return products


def get_product_by_sku(db: Session, sku: str):
    return db.query(models.Product).filter(models.Product.sku == sku).first()


# 🔹 Obtener producto por ID
def _attach_group_meta(db: Session, product: Optional[models.Product]):
    if not product:
        return None
    if not getattr(product, "group_name", None):
        product.group_meta = None
        return product
    group = get_product_group_by_path(db, product.group_name)
    product.group_meta = group
    return product


def get_product(db: Session, product_id: int):
    product = (
        db.query(models.Product).filter(models.Product.id == product_id).first()
    )
    return _attach_group_meta(db, product)


def create_product(db: Session, product_in: schemas.ProductCreate):
    db_product = models.Product(
        sku=product_in.sku,
        name=product_in.name,
        price=product_in.price,
        cost=product_in.cost,
        barcode=product_in.barcode,
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
        group_name=product_in.group_name,
        brand=product_in.brand,
        supplier=product_in.supplier,
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
    for field, value in data.items():
        setattr(db_product, field, value)
    db.commit()
    db.refresh(db_product)
    return _attach_group_meta(db, db_product)


# 🔹 Eliminar producto
def delete_product(db: Session, db_product: models.Product):
    db.delete(db_product)
    db.commit()


# ===================== PAYMENT METHODS =====================


def _normalize_slug(slug: str) -> str:
    return slug.strip().lower()


def list_payment_methods(db: Session, include_deleted: bool = False):
    query = db.query(models.PaymentMethod)
    if not include_deleted:
        query = query.filter(models.PaymentMethod.deleted_at.is_(None))
    return (
        query.order_by(models.PaymentMethod.order_index.asc(), models.PaymentMethod.id.asc())
        .all()
    )


def get_payment_method(db: Session, method_id: int):
    return (
        db.query(models.PaymentMethod)
        .filter(models.PaymentMethod.id == method_id)
        .first()
    )


def get_payment_method_by_slug(db: Session, slug: str):
    return (
        db.query(models.PaymentMethod)
        .filter(models.PaymentMethod.slug == slug)
        .first()
    )


def _count_active_payment_methods(db: Session, exclude_id: Optional[int] = None) -> int:
    query = db.query(models.PaymentMethod).filter(
        models.PaymentMethod.is_active.is_(True),
        models.PaymentMethod.deleted_at.is_(None),
    )
    if exclude_id is not None:
        query = query.filter(models.PaymentMethod.id != exclude_id)
    return query.count()


def _ensure_slug_available(db: Session, slug: str, current_id: Optional[int] = None) -> None:
    normalized = _normalize_slug(slug)
    query = db.query(models.PaymentMethod).filter(models.PaymentMethod.slug == normalized)
    if current_id is not None:
        query = query.filter(models.PaymentMethod.id != current_id)
    if query.first():
        raise ValueError("Ya existe un método con ese slug")


def _next_order_index(db: Session) -> int:
    max_value = db.query(func.max(models.PaymentMethod.order_index)).scalar()
    return int(max_value or 0) + 10


def create_payment_method(db: Session, payload: schemas.PaymentMethodCreate):
    slug = _normalize_slug(payload.slug)
    _ensure_slug_available(db, slug)
    order_index = payload.order_index if payload.order_index is not None else _next_order_index(db)
    method = models.PaymentMethod(
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
    db.commit()
    db.refresh(method)
    return method


def update_payment_method(
    db: Session,
    method: models.PaymentMethod,
    payload: schemas.PaymentMethodUpdate,
):
    data = payload.model_dump(exclude_unset=True)
    if "slug" in data:
        slug = _normalize_slug(data["slug"])
        _ensure_slug_available(db, slug, current_id=method.id)
        data["slug"] = slug
    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()

    if "is_active" in data and data["is_active"] is False and method.is_active:
        if _count_active_payment_methods(db, exclude_id=method.id) == 0:
            raise ValueError("Debe existir al menos un método de pago activo")

    for field, value in data.items():
        setattr(method, field, value)
    db.commit()
    db.refresh(method)
    return method


def toggle_payment_method(
    db: Session,
    method: models.PaymentMethod,
    is_active: bool,
):
    if not is_active and method.is_active:
        if _count_active_payment_methods(db, exclude_id=method.id) == 0:
            raise ValueError("Debe existir al menos un método de pago activo")
    method.is_active = is_active
    db.commit()
    db.refresh(method)
    return method


def reorder_payment_methods(
    db: Session,
    reorder_items: List[schemas.PaymentMethodReorderItem],
):
    ids = [item.id for item in reorder_items]
    methods = (
        db.query(models.PaymentMethod)
        .filter(models.PaymentMethod.id.in_(ids))
        .all()
    )
    methods_map = {m.id: m for m in methods}
    if len(methods_map) != len(ids):
        raise ValueError("Algún método de pago no existe")
    for item in reorder_items:
        methods_map[item.id].order_index = item.order_index
    db.commit()
    return list_payment_methods(db)


def delete_payment_method(db: Session, method: models.PaymentMethod):
    if method.is_active and _count_active_payment_methods(db, exclude_id=method.id) == 0:
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
):
    return (
        db.query(models.ProductGroup)
        .order_by(models.ProductGroup.path.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_product_group(db: Session, group_id: int):
    return (
        db.query(models.ProductGroup)
        .filter(models.ProductGroup.id == group_id)
        .first()
    )


def get_product_group_by_path(db: Session, path: str):
    return (
        db.query(models.ProductGroup)
        .filter(models.ProductGroup.path == path)
        .first()
    )


def create_product_group(db: Session, group_in: schemas.ProductGroupCreate):
    existing = get_product_group_by_path(db, group_in.path)
    if existing:
        raise ValueError("Ya existe un grupo con ese path")

    group = models.ProductGroup(
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
        existing = get_product_group_by_path(db, data["path"])
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

    def _clean_field(value):
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
        return value

    customer_payload = {
        "customer_id": getattr(sale_in, "customer_id", None),
        "customer_name": getattr(sale_in, "customer_name", None),
        "customer_phone": getattr(sale_in, "customer_phone", None),
        "customer_email": getattr(sale_in, "customer_email", None),
        "customer_tax_id": getattr(sale_in, "customer_tax_id", None),
        "customer_address": getattr(sale_in, "customer_address", None),
    }

    if customer_payload["customer_id"] is not None:
        customer = get_pos_customer(db, customer_payload["customer_id"])
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

    sale_number_preassigned = getattr(sale_in, "sale_number_preassigned", None)
    if sale_number_preassigned is not None:
        existing_sale_number = (
            db.query(models.Sale)
            .filter(models.Sale.sale_number == sale_number_preassigned)
            .first()
        )
        if existing_sale_number:
            raise ValueError(
                f"El número de ticket {sale_number_preassigned} ya existe en otra venta"
            )

    # 3) Crear la venta (aún sin sale_number / document_number)

    pos_name = _clean_field(getattr(sale_in, "pos_name", None))
    station_id = _resolve_station_id(db, getattr(sale_in, "station_id", None))
    is_pos_web = _is_pos_web_name(pos_name)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede registrar estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(db, pos_name)
    if not station_id and not is_pos_web:
        raise ValueError("Debe seleccionar una estación para registrar la venta")

    sale = models.Sale(
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
        surcharge_amount=surcharge_amount,
        surcharge_label=surcharge_label,
    )

    db.add(sale)
    db.flush()  # para obtener sale.id

    # 3b) Generar número de ticket y documento basados en id
    if sale.sale_number is None:
        sale.sale_number = sale.id

    if not sale.document_number:
        sale.document_number = f"V-{sale.id:06d}"

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
            sale_id=sale.id,
            method=pay.method,
            amount=pay.amount,
            is_primary=(idx == 0),
        )
        db.add(db_payment)

    db.commit()
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


def get_separated_order(db: Session, order_id: int) -> Optional[models.SeparatedOrder]:
    return (
        db.query(models.SeparatedOrder)
        .filter(models.SeparatedOrder.id == order_id)
        .first()
    )


def list_separated_orders(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    barcode: Optional[str] = None,
    sale_number: Optional[int] = None,
    customer: Optional[str] = None,
    status: Optional[str] = None,
) -> List[models.SeparatedOrder]:
    query = db.query(models.SeparatedOrder)
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

    station_id = _resolve_station_id(db, payment_in.station_id)
    if not station_id and order.sale.station_id:
        station_id = _resolve_station_id(db, order.sale.station_id)
    is_pos_web = _is_pos_web_name(order.sale.pos_name)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede registrar estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(db, order.sale.pos_name)
    if not station_id and not is_pos_web:
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


def get_sales(db: Session, skip: int = 0, limit: int = 100):
    return (
        db.query(models.Sale)
        .order_by(models.Sale.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_next_sale_number(db: Session, pos_id: Optional[str] = None) -> int:
    """Return the next available sale_number. pos_id reserved for future use."""

    max_sale_number = db.query(func.max(models.Sale.sale_number)).scalar()
    max_sale_id = db.query(func.max(models.Sale.id)).scalar()

    candidates = [value for value in [max_sale_number, max_sale_id] if value is not None]
    current = int(max(candidates)) if candidates else 0
    return current + 1


def get_sale(db: Session, sale_id: int) -> Optional[models.Sale]:
    return db.query(models.Sale).filter(models.Sale.id == sale_id).first()


def get_sale_by_document(db: Session, document_number: str) -> Optional[models.Sale]:
    return (
        db.query(models.Sale)
        .filter(models.Sale.document_number == document_number)
        .first()
    )


def get_sale_return(db: Session, return_id: int) -> Optional[models.SaleReturn]:
    return (
        db.query(models.SaleReturn)
        .filter(models.SaleReturn.id == return_id)
        .first()
    )


def list_returns(db: Session, skip: int = 0, limit: int = 100):
    return (
        db.query(models.SaleReturn)
        .options(joinedload(models.SaleReturn.sale))
        .order_by(models.SaleReturn.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def list_changes(db: Session, skip: int = 0, limit: int = 100):
    return (
        db.query(models.SaleChange)
        .order_by(models.SaleChange.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_sale_change(db: Session, change_id: int) -> Optional[models.SaleChange]:
    return (
        db.query(models.SaleChange)
        .filter(models.SaleChange.id == change_id)
        .first()
    )


def create_return(db: Session, return_in: schemas.SaleReturnCreate) -> models.SaleReturn:
    if not return_in.items or len(return_in.items) == 0:
        raise ValueError("La devolución debe incluir al menos un ítem")

    sale: Optional[models.Sale] = None
    if return_in.sale_id is not None:
        sale = get_sale(db, return_in.sale_id)
    elif return_in.sale_document_number:
        sale = get_sale_by_document(db, return_in.sale_document_number)

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

    payments_payload = (
        list(return_in.payments)
        if return_in.payments and len(return_in.payments) > 0
        else [
            schemas.ReturnPaymentCreate(
                method=sale.main_payment_method,
                amount=total_refund,
            )
        ]
    )

    payments_total = sum(float(p.amount) for p in payments_payload)
    if abs(payments_total - total_refund) > 0.01:
        raise ValueError(
            "La suma de los pagos de reembolso debe coincidir con el total a devolver"
        )

    status = return_in.status or "confirmed"

    sale_return = models.SaleReturn(
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


def create_change(db: Session, change_in: schemas.SaleChangeCreate) -> models.SaleChange:
    if not change_in.return_items or len(change_in.return_items) == 0:
        raise ValueError("El cambio debe incluir al menos un ítem devuelto")
    if not change_in.new_items or len(change_in.new_items) == 0:
        raise ValueError("El cambio debe incluir al menos un ítem nuevo")

    sale: Optional[models.Sale] = None
    if change_in.sale_id is not None:
        sale = get_sale(db, change_in.sale_id)
    elif change_in.sale_document_number:
        sale = get_sale_by_document(db, change_in.sale_document_number)

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
        product = db.query(models.Product).filter(models.Product.id == item_in.product_id).first()
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


def get_separated_order_payment(
    db: Session,
    payment_id: int,
) -> Optional[models.SeparatedOrderPayment]:
    return (
        db.query(models.SeparatedOrderPayment)
        .filter(models.SeparatedOrderPayment.id == payment_id)
        .first()
    )


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


def get_pos_settings(db: Session) -> models.PosSettings:
    settings = db.query(models.PosSettings).order_by(models.PosSettings.id.asc()).first()
    if not settings:
        settings = models.PosSettings()
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
    settings.email_from = settings.email_from or ""
    if settings.web_pos_send_closure_email is None:
        settings.web_pos_send_closure_email = True
    settings.station_closure_email_overrides = (
        settings.station_closure_email_overrides or {}
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
    data = settings_in.model_dump()
    notifications = data.pop("notifications", None)
    for field, value in data.items():
        setattr(settings, field, value)
    if notifications is not None:
        settings.notifications = notifications
    db.commit()
    db.refresh(settings)
    return settings


def get_role_permissions(db: Session):
    settings = get_pos_settings(db)
    return permissions.ensure_permissions(settings.role_permissions)


def update_role_permissions(db: Session, modules: List[Dict[str, Any]]):
    settings = get_pos_settings(db)
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
):
    query = db.query(models.PosUser)
    if status:
        query = query.filter(models.PosUser.status == status)
    if role:
        query = query.filter(models.PosUser.role == role)
    return query.order_by(models.PosUser.created_at.desc()).all()


def get_pos_user(db: Session, user_id: int) -> Optional[models.PosUser]:
    return db.query(models.PosUser).filter(models.PosUser.id == user_id).first()


def get_pos_user_by_email(db: Session, email: str) -> Optional[models.PosUser]:
    return (
        db.query(models.PosUser)
        .filter(func.lower(models.PosUser.email) == email.lower())
        .first()
    )


def _count_active_admins(db: Session) -> int:
    return (
        db.query(models.PosUser)
        .filter(models.PosUser.role == "Administrador")
        .filter(models.PosUser.status == "Activo")
        .count()
    )


def create_pos_user(db: Session, user_in: schemas.PosUserCreate) -> models.PosUser:
    existing = get_pos_user_by_email(db, user_in.email)
    if existing:
        raise ValueError("Ya existe un usuario con ese email")

    raw_password = user_in.password or secrets.token_urlsafe(16)

    user = models.PosUser(
        name=user_in.name,
        email=user_in.email,
        role=user_in.role,
        status="Activo",
        is_active=True,
        password_hash=hash_password(raw_password),
        phone=user_in.phone,
        position=user_in.position,
        notes=user_in.notes,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
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
        existing = get_pos_user_by_email(db, new_email)
        if existing and existing.id != user.id:
            raise ValueError("Ya existe un usuario con ese email")

    new_role = data.get("role", user.role)
    new_status = data.get("status", user.status)

    was_active_admin = user.role == "Administrador" and user.status == "Activo"
    will_be_active_admin = new_role == "Administrador" and new_status == "Activo"

    if was_active_admin and not will_be_active_admin:
        if _count_active_admins(db) <= 1:
            raise ValueError("No se puede desactivar o cambiar al último Administrador activo")

    for field, value in data.items():
        if field == "password":
            user.password_hash = hash_password(value)
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
) -> list[models.PosUserDocument]:
    return (
        db.query(models.PosUserDocument)
        .filter(models.PosUserDocument.user_id == user_id)
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
) -> models.PosUserDocument:
    doc = models.PosUserDocument(
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
) -> bool:
    doc = (
        db.query(models.PosUserDocument)
        .filter(models.PosUserDocument.user_id == user_id)
        .filter(models.PosUserDocument.id == doc_id)
        .first()
    )
    if not doc:
        return False
    db.delete(doc)
    db.commit()
    return True


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
) -> Optional[str]:
    label = _station_label_from_pos_name(pos_name)
    if not label:
        return None
    stations = (
        db.query(models.PosStation)
        .filter(
            func.lower(models.PosStation.label) == label.lower(),
            models.PosStation.is_active.is_(True),
        )
        .all()
    )
    if len(stations) == 1:
        return stations[0].id
    return None


def _resolve_station_id(
    db: Session,
    station_id: Optional[str],
) -> Optional[str]:
    if not station_id:
        return None
    station = (
        db.query(models.PosStation)
        .filter(models.PosStation.id == station_id)
        .first()
    )
    if not station or not station.is_active:
        raise ValueError("Estación inválida o inactiva")
    return station.id


def _generate_station_pin(length: int = 6) -> str:
    digits = string.digits
    return "".join(secrets.choice(digits) for _ in range(length))


def list_pos_stations(db: Session) -> List[models.PosStation]:
    return (
        db.query(models.PosStation)
        .options(selectinload(models.PosStation.user))
        .order_by(models.PosStation.created_at.desc())
        .all()
    )


def get_pos_station(db: Session, station_id: str) -> Optional[models.PosStation]:
    return (
        db.query(models.PosStation)
        .options(selectinload(models.PosStation.user))
        .filter(models.PosStation.id == station_id)
        .first()
    )


def create_pos_station(
    db: Session,
    payload: schemas.PosStationCreate,
) -> tuple[models.PosStation, str]:
    user = get_pos_user_by_email(db, payload.pos_user_email)
    if not user:
        raise ValueError("Usuario POS no encontrado")

    pin_plain = payload.pin_plain or _generate_station_pin()
    station = models.PosStation(
        id=str(uuid4()),
        label=payload.label,
        pos_user_id=user.id,
        pin_hash=hash_password(pin_plain),
        is_active=True,
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return station, pin_plain


def update_pos_station(
    db: Session,
    station: models.PosStation,
    payload: schemas.PosStationUpdate,
) -> tuple[models.PosStation, Optional[str]]:
    data = payload.model_dump(exclude_unset=True)
    pin_plain: Optional[str] = None
    if "label" in data and data["label"] is not None:
        station.label = data["label"]
    if "is_active" in data and data["is_active"] is not None:
        station.is_active = data["is_active"]
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
    if station.failed_attempts >= 5:
        station.is_active = False
        _station_logger.warning(
            "POS station %s desactivada por múltiples intentos fallidos", station.id
        )
    db.commit()


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
) -> Optional[models.PasswordReset]:
    token_hash = _password_reset_token_hash(token)
    return (
        db.query(models.PasswordReset)
        .filter(models.PasswordReset.token_hash == token_hash)
        .first()
    )


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
):
    query = db.query(models.PosCustomer)
    if not include_inactive:
        query = query.filter(models.PosCustomer.is_active.is_(True))

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
):
    count_expr = func.count(models.Sale.id)
    query = (
        db.query(models.PosCustomer, count_expr.label("sales_count"))
        .join(models.Sale, models.Sale.customer_id == models.PosCustomer.id)
        .filter(models.PosCustomer.is_active.is_(True))
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


def get_pos_customer(db: Session, customer_id: int) -> Optional[models.PosCustomer]:
    return (
        db.query(models.PosCustomer)
        .filter(models.PosCustomer.id == customer_id)
        .first()
    )


def create_pos_customer(
    db: Session,
    customer_in: schemas.PosCustomerCreate,
) -> models.PosCustomer:
    customer = models.PosCustomer(
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


# ===================== POS CLOSURES =====================


def create_pos_closure(
    db: Session,
    closure_in: schemas.PosClosureCreate,
    user: models.PosUser,
) -> models.PosClosure:
    pos_name = closure_in.pos_name.strip() if closure_in.pos_name else None
    station_id = _resolve_station_id(db, closure_in.station_id)
    is_pos_web = _is_pos_web_name(pos_name)

    if station_id and is_pos_web:
        raise ValueError("POS Web no puede cerrar con estación")
    if not station_id and not is_pos_web:
        station_id = _resolve_station_id_from_pos_name(db, pos_name)
    if not station_id and not is_pos_web:
        raise ValueError("Debe seleccionar una estación para cerrar caja")

    pending_sales_query = db.query(models.Sale).filter(
        models.Sale.closure_id.is_(None),
        or_(models.Sale.status.is_(None), models.Sale.status != "voided"),
    )
    if station_id:
        pending_sales_query = pending_sales_query.filter(
            models.Sale.station_id == station_id
        )
    elif pos_name:
        pending_sales_query = _filter_pos_name(
            pending_sales_query,
            models.Sale.pos_name,
            pos_name,
        )

    pending_sales = pending_sales_query.order_by(models.Sale.created_at.asc()).all()
    admin_fallback_used = False

    if not pending_sales and station_id and user.role == "Administrador":
        fallback_query = db.query(models.Sale).filter(
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
            models.SaleReturn.closure_id.is_(None),
            models.SaleReturn.status == "confirmed",
        )
    )
    if station_id:
        pending_returns_query = pending_returns_query.filter(
            models.Sale.station_id == station_id
        )
    elif pos_name:
        pending_returns_query = _filter_pos_name(
            pending_returns_query,
            models.Sale.pos_name,
            pos_name,
        )

    pending_returns = pending_returns_query.order_by(models.SaleReturn.created_at.asc()).all()

    pending_changes_base = (
        db.query(models.SaleChange)
        .filter(
            models.SaleChange.closure_id.is_(None),
            models.SaleChange.status == "confirmed",
        )
    )
    if station_id:
        pending_changes_base = pending_changes_base.filter(
            models.SaleChange.station_id == station_id
        )
    elif pos_name:
        pending_changes_base = _filter_pos_name(
            pending_changes_base,
            models.SaleChange.pos_name,
            pos_name,
        )

    pending_changes_all = pending_changes_base.order_by(models.SaleChange.created_at.asc()).all()

    sep_paid_at = (
        db.query(func.min(models.SeparatedOrderPayment.paid_at))
        .join(models.SeparatedOrder, models.SeparatedOrderPayment.separated_order_id == models.SeparatedOrder.id)
        .join(models.Sale, models.SeparatedOrder.sale_id == models.Sale.id)
        .filter(
            models.SeparatedOrderPayment.closure_id.is_(None),
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            ),
        )
    )
    if station_id:
        sep_paid_at = sep_paid_at.filter(
            or_(
                models.SeparatedOrderPayment.station_id == station_id,
                models.Sale.station_id == station_id,
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
    total_amount = sum(float(sale.total or 0.0) for sale in pending_sales)
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
    rows = (
        db.query(models.SalePayment.method, func.sum(models.SalePayment.amount))
        .filter(models.SalePayment.sale_id.in_(sale_ids))
        .group_by(models.SalePayment.method)
        .all()
    )
    method_map = {
        "cash": "cash",
        "card": "card",
        "qr": "qr",
        "nequi": "nequi",
        "daviplata": "daviplata",
        "credit": "credit",
    }
    for method, amount in rows:
        key = method_map.get((method or "").lower())
        if key:
            payment_totals[key] += float(amount or 0.0)

    if pending_returns:
        return_ids = [ret.id for ret in pending_returns]
        return_rows = (
            db.query(models.SaleReturnPayment.method, func.sum(models.SaleReturnPayment.amount))
            .filter(models.SaleReturnPayment.return_id.in_(return_ids))
            .group_by(models.SaleReturnPayment.method)
            .all()
        )
        for method, amount in return_rows:
            key = method_map.get((method or "").lower())
            if key:
                payment_totals[key] -= float(amount or 0.0)

    sep_payment_filter = (
        db.query(
            models.SeparatedOrderPayment.method,
            func.sum(models.SeparatedOrderPayment.amount),
        )
        .join(models.SeparatedOrder, models.SeparatedOrderPayment.separated_order_id == models.SeparatedOrder.id)
        .join(models.Sale, models.SeparatedOrder.sale_id == models.Sale.id)
        .filter(
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
            models.SeparatedOrderPayment.closure_id.is_(None),
            or_(
                models.SeparatedOrderPayment.status.is_(None),
                models.SeparatedOrderPayment.status != "voided",
            ),
        )
    )
    if station_id:
        sep_payment_filter = sep_payment_filter.filter(
            or_(
                models.SeparatedOrderPayment.station_id == station_id,
                models.Sale.station_id == station_id,
            )
        )
        sep_ids_query = sep_ids_query.filter(
            or_(
                models.SeparatedOrderPayment.station_id == station_id,
                models.Sale.station_id == station_id,
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

    sep_rows = sep_payment_filter.group_by(models.SeparatedOrderPayment.method).all()
    sep_payment_ids = [row[0] for row in sep_ids_query.all()]

    for method, amount in sep_rows:
        key = method_map.get((method or "").lower())
        if key:
            payment_totals[key] += float(amount or 0.0)

    pending_changes = [
        change
        for change in pending_changes
        if change.created_at >= range_start
    ]
    change_extra_total = sum(float(change.extra_payment or 0.0) for change in pending_changes)
    change_refund_total = sum(float(change.refund_due or 0.0) for change in pending_changes)
    change_count = len(pending_changes)

    for change in pending_changes:
        for payment in change.payments:
            key = method_map.get((payment.method or "").lower())
            if key:
                payment_totals[key] += float(payment.amount or 0.0)
        if float(change.refund_due or 0.0) > 0:
            payment_totals["cash"] -= float(change.refund_due or 0.0)

    net_amount = total_amount - total_refunds + change_extra_total - change_refund_total

    difference = float(closure_in.counted_cash or 0.0) - payment_totals["cash"]
    total_surcharge = sum(float(sale.surcharge_amount or 0.0) for sale in pending_sales)

    closure = models.PosClosure(
        pos_name=pos_name,
        pos_identifier=closure_in.pos_identifier,
        station_id=station_id,
        closed_by_user_id=user.id,
        closed_by_user_name=user.name,
        opened_at=range_start,
        closed_at=closed_at,
        total_amount=total_amount,
        total_cash=payment_totals["cash"],
        total_card=payment_totals["card"],
        total_qr=payment_totals["qr"],
        total_nequi=payment_totals["nequi"],
        total_daviplata=payment_totals["daviplata"],
        total_credit=payment_totals["credit"],
        total_refunds=total_refunds,
        net_amount=net_amount,
        counted_cash=closure_in.counted_cash,
        difference=difference,
        notes=closure_in.notes,
        sales_count=sales_count,
        change_extra_total=change_extra_total,
        change_refund_total=change_refund_total,
        change_count=change_count,
        total_surcharge=total_surcharge,
    )
    db.add(closure)
    db.flush()
    if not closure.consecutive:
        closure.consecutive = f"CL-{closure.id:06d}"

    for sale in pending_sales:
        sale.closure_id = closure.id

    if pending_returns:
        (
            db.query(models.SaleReturn)
            .filter(models.SaleReturn.id.in_([ret.id for ret in pending_returns]))
            .update({"closure_id": closure.id}, synchronize_session=False)
        )

    if sep_payment_ids:
        (
            db.query(models.SeparatedOrderPayment)
                .filter(models.SeparatedOrderPayment.id.in_(sep_payment_ids))
                .update({"closure_id": closure.id}, synchronize_session=False)
        )
    if pending_changes:
        (
            db.query(models.SaleChange)
            .filter(models.SaleChange.id.in_([change.id for change in pending_changes]))
            .update({"closure_id": closure.id}, synchronize_session=False)
        )
        if admin_fallback_used and station_id:
            (
                db.query(models.SeparatedOrderPayment)
                .filter(models.SeparatedOrderPayment.id.in_(sep_payment_ids))
                .filter(models.SeparatedOrderPayment.station_id.is_(None))
                .update({"station_id": station_id}, synchronize_session=False)
            )

    db.commit()
    db.refresh(closure)
    return closure


def get_pos_closure(db: Session, closure_id: int) -> Optional[models.PosClosure]:
    return (
        db.query(models.PosClosure)
        .filter(models.PosClosure.id == closure_id)
        .first()
    )


def list_pos_closures(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    pos_name: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[models.PosClosure]:
    query = db.query(models.PosClosure)
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
