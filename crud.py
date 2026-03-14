from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import re
import secrets
import string
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from sqlalchemy import String, case, cast, func, or_, text, true
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


def _normalize_label(value: Optional[str]) -> str:
    return (value or "").strip().lower()


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


def build_tenant_session_read(tenant: Optional[models.Tenant]) -> Optional[schemas.TenantSessionRead]:
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
    limit: int = 20,
    tenant_id: Optional[int] = None,
) -> List[models.Product]:
    query = db.query(models.Product).filter(models.Product.active.is_(True))
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.Product.tenant_id == effective_tenant_id)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                models.Product.name.ilike(like),
                models.Product.sku.ilike(like),
                models.Product.barcode.ilike(like),
            )
        )
    return (
        query.order_by(models.Product.name.asc(), models.Product.id.asc())
        .limit(limit)
        .all()
    )


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
    if unit_cost is not None:
        item.unit_cost_snapshot = float(unit_cost)
    if notes is not None:
        item.notes = _clean_field(notes)
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
    db_product = models.Product(
        tenant_id=effective_tenant_id,
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

    effective_tenant_id = get_default_tenant_id(db)
    if created_by_user_id:
        creator = db.query(models.PosUser).filter(models.PosUser.id == created_by_user_id).first()
        if creator:
            effective_tenant_id = resolve_user_tenant_id(db, creator)

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
    if user.employee_id:
        employee = get_hr_employee(db, user.employee_id, tenant_id=user.tenant_id)
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
        .order_by(models.HREmployee.created_at.desc())
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
    employee = models.HREmployee(
        tenant_id=effective_tenant_id,
        **payload.model_dump(),
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
    for field, value in data.items():
        setattr(employee, field, value)
    db.commit()
    db.refresh(employee)
    if employee.system_user:
        user = employee.system_user
        user.name = employee.name
        user.email = employee.email or user.email
        user.phone = employee.phone
        user.position = employee.position
        user.notes = employee.notes
        user.avatar_url = employee.avatar_url
        user.birth_date = employee.birth_date
        user.location = employee.location
        user.bio = employee.bio
        db.commit()
        db.refresh(employee)
    return employee


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
    employee_rows = [
        schemas.ScheduleEmployeeRow(
            id=employee.id,
            name=employee.name,
            status="Activo" if employee.status == "Activo" else "Inactivo",
            position=employee.position,
            avatar_url=employee.avatar_url,
        )
        for employee in sorted(employees, key=lambda row: (row.status != "Activo", row.name.lower()))
    ]
    return schemas.ScheduleWeekView(
        week=schemas.ScheduleWeekRead.model_validate(week),
        employees=employee_rows,
        shifts=shift_payload,
        day_totals=day_totals,
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
    tenant_id: Optional[int] = None,
):
    query = db.query(models.PosCustomer)
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    if effective_tenant_id is not None:
        query = query.filter(models.PosCustomer.tenant_id == effective_tenant_id)
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
    tenant_id: Optional[int] = None,
):
    effective_tenant_id = tenant_id if tenant_id is not None else get_default_tenant_id(db)
    count_expr = func.count(models.Sale.id)
    query = (
        db.query(models.PosCustomer, count_expr.label("sales_count"))
        .join(models.Sale, models.Sale.customer_id == models.PosCustomer.id)
        .filter(models.PosCustomer.is_active.is_(True))
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

    pending_sales = pending_sales_query.order_by(models.Sale.created_at.asc()).all()
    admin_fallback_used = False

    if not pending_sales and station_id and user.role == "Administrador":
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
            if key:
                payment_totals[key] += float(amount or 0.0)
                station_bucket[f"total_{key}"] += float(amount or 0.0)

        # El cambio de la venta (vuelto) siempre sale de caja en efectivo.
        # Se descuenta del total de efectivo para no inflar cifras en cierre.
        sale_change_amount = max(float(sale.change_amount or 0.0), 0.0)
        if sale_change_amount > 0:
            payment_totals["cash"] -= sale_change_amount
            station_bucket["total_cash"] -= sale_change_amount

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
            if key:
                payment_totals[key] -= float(amount or 0.0)
                station_bucket = _station_bucket(ret_station_id)
                station_bucket[f"total_{key}"] -= float(amount or 0.0)

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

    sep_rows = sep_payment_filter.group_by(
        "resolved_station_id",
        models.SeparatedOrderPayment.method,
    ).all()
    sep_payment_ids = [row[0] for row in sep_ids_query.all()]

    for sep_station_id, method, amount in sep_rows:
        key = method_map.get((method or "").lower())
        if key:
            payment_totals[key] += float(amount or 0.0)
            station_bucket = _station_bucket(sep_station_id)
            station_bucket[f"total_{key}"] += float(amount or 0.0)

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
            if key:
                payment_totals[key] += float(payment.amount or 0.0)
                station_bucket[f"total_{key}"] += float(payment.amount or 0.0)
        if float(change.refund_due or 0.0) > 0:
            payment_totals["cash"] -= float(change.refund_due or 0.0)
            station_bucket["total_cash"] -= float(change.refund_due or 0.0)

    net_amount = total_amount - total_refunds + change_extra_total - change_refund_total
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

    closure = models.PosClosure(
        tenant_id=effective_tenant_id,
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
        station_breakdown=station_breakdown,
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
