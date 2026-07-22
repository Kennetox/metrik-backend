from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    JSON,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import deferred, relationship

from database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    lifecycle_stage = Column(String(24), nullable=False, default="active")
    trial_started_at = Column(DateTime, nullable=True)
    trial_ends_at = Column(DateTime, nullable=True)
    converted_at = Column(DateTime, nullable=True)
    enabled_modules = Column(JSON, nullable=True)
    module_user_access = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class PlatformUser(Base):
    __tablename__ = "platform_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    password_hash = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class PlatformLogin2FAChallenge(Base):
    __tablename__ = "platform_login_2fa_challenges"

    id = Column(Integer, primary_key=True, index=True)
    platform_user_id = Column(Integer, ForeignKey("platform_users.id"), nullable=False, index=True)
    code_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    consumed_at = Column(DateTime, nullable=True)
    user_agent = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("PlatformUser")


class PlatformTrustedDevice(Base):
    __tablename__ = "platform_trusted_devices"

    id = Column(Integer, primary_key=True, index=True)
    platform_user_id = Column(Integer, ForeignKey("platform_users.id"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    device_label = Column(String(255), nullable=True)
    user_agent = Column(Text, nullable=True)
    last_ip = Column(String(64), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("PlatformUser")


class DemoSignupAudit(Base):
    __tablename__ = "demo_signup_audits"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    ip_address = Column(String(64), nullable=True, index=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    sku = Column(String, index=True, nullable=True)
    name = Column(String, index=True, nullable=False)

    price = Column(Float, nullable=False)
    cost = Column(Float, nullable=False)

    barcode = Column(String, nullable=True)
    label_format = Column(String(64), nullable=False, default="Kensar1")
    unit = Column(String, nullable=True)
    image_url = Column(String(512), nullable=True)
    image_thumb_url = Column(String(512), nullable=True)
    tile_color = Column(String(7), nullable=True)
    web_name = Column(String(255), nullable=True)
    web_slug = Column(String(160), nullable=True, index=True)
    web_published = Column(Boolean, nullable=False, default=False)
    web_published_at = Column(DateTime, nullable=True)
    web_featured = Column(Boolean, nullable=False, default=False)
    web_short_description = Column(String(280), nullable=True)
    web_long_description = Column(Text, nullable=True)
    web_compare_price = Column(Float, nullable=True)
    web_price_source = Column(String(24), nullable=False, default="base")
    web_price_value = Column(Float, nullable=True)
    web_badge_text = Column(String(80), nullable=True)
    web_category_key = Column(String(64), nullable=True)
    web_sort_order = Column(Integer, nullable=False, default=0)
    web_visible_when_out_of_stock = Column(Boolean, nullable=False, default=True)
    web_price_mode = Column(String(24), nullable=False, default="visible")
    web_whatsapp_message = Column(Text, nullable=True)
    web_warranty_text = Column(String(160), nullable=True)
    web_gallery_urls = Column(Text, nullable=True)
    web_video_url = Column(String(512), nullable=True)

    stock_min = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    service = Column(Boolean, default=False)
    includes_tax = Column(Boolean, default=False)
    is_investment = Column(Boolean, nullable=False, default=False)
    preferred_qty = Column(Integer, default=0)
    reorder_point = Column(Integer, default=0)
    low_stock_alert = Column(Boolean, default=False)
    allow_price_change = Column(Boolean, default=False)

    # info adicional
    group_name = Column(String, nullable=True)   # viene de 'grupo' en Excel
    brand = Column(String, nullable=True)        # 'marca'
    supplier = Column(String, nullable=True)
    investment_enabled_at = Column(DateTime, nullable=True)
    investment_disabled_at = Column(DateTime, nullable=True)
    investment_status = Column(String(16), nullable=False, default="active")
    group_meta = None  # runtime attribute for schemas
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ProductAuditLog(Base):
    __tablename__ = "product_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    product_id = Column(Integer, nullable=False, index=True)
    action = Column(String, nullable=False)  # create | update | delete
    actor_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    actor_name = Column(String, nullable=True)
    actor_email = Column(String, nullable=True)
    changes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    actor = relationship("PosUser")


class ProductGroup(Base):
    __tablename__ = "product_groups"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    path = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    parent_path = Column(String, nullable=True)
    image_url = Column(String(512), nullable=True)
    image_thumb_url = Column(String(512), nullable=True)
    tile_color = Column(String(7), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    qty_delta = Column(Float, nullable=False, default=0)
    reason = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    reference_type = Column(String, nullable=True)
    reference_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    product = relationship("Product")
    created_by = relationship("PosUser")


class InventoryRecount(Base):
    __tablename__ = "inventory_recounts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    code = Column(String, unique=True, nullable=True, index=True)
    status = Column(String, nullable=False, default="draft")
    source = Column(String, nullable=False, default="web")  # web | app
    stock_device_id = Column(String, ForeignKey("stock_devices.id"), nullable=True, index=True)
    stock_device_name = Column(String(120), nullable=True)
    scope_type = Column(String, nullable=False, default="all")  # all | group
    scope_value = Column(String, nullable=True)  # group_name when scope_type=group
    count_mode = Column(String, nullable=False, default="blind")  # blind | visible
    title = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    applied_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    started_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    created_by = relationship("PosUser", foreign_keys=[created_by_user_id])
    closed_by = relationship("PosUser", foreign_keys=[closed_by_user_id])
    applied_by = relationship("PosUser", foreign_keys=[applied_by_user_id])
    lines = relationship(
        "InventoryRecountLine",
        back_populates="recount",
        cascade="all, delete-orphan",
    )

    @property
    def created_by_user_name(self) -> str | None:
        return self.created_by.name if self.created_by else None

    @property
    def closed_by_user_name(self) -> str | None:
        return self.closed_by.name if self.closed_by else None

    @property
    def applied_by_user_name(self) -> str | None:
        return self.applied_by.name if self.applied_by else None


class InventoryRecountLine(Base):
    __tablename__ = "inventory_recount_lines"
    __table_args__ = (
        UniqueConstraint("recount_id", "product_id", name="uq_recount_product"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    recount_id = Column(
        Integer,
        ForeignKey("inventory_recounts.id"),
        nullable=False,
        index=True,
    )
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    product_name_snapshot = Column(String, nullable=False)
    sku_snapshot = Column(String, nullable=True)
    barcode_snapshot = Column(String, nullable=True)
    group_name_snapshot = Column(String, nullable=True)
    system_qty = Column(Float, nullable=False, default=0)
    counted_qty = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    counted_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    counted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    recount = relationship("InventoryRecount", back_populates="lines")
    product = relationship("Product")
    counted_by = relationship("PosUser")


class InventoryRecountDraft(Base):
    __tablename__ = "inventory_recount_drafts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "recount_id",
            "user_id",
            name="uq_inventory_recount_drafts_user_scope",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    recount_id = Column(
        Integer,
        ForeignKey("inventory_recounts.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    counted_draft = Column(JSON, nullable=False, default=dict)
    free_count_draft = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    recount = relationship("InventoryRecount")
    user = relationship("PosUser")


class ReceivingLot(Base):
    __tablename__ = "receiving_lots"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    lot_number = Column(String, unique=True, index=True, nullable=True)
    status = Column(String, nullable=False, default="open")
    purchase_type = Column(String, nullable=False, default="cash")
    origin_name = Column(String, nullable=False)
    stock_device_id = Column(String, ForeignKey("stock_devices.id"), nullable=True, index=True)
    stock_device_name = Column(String(120), nullable=True)
    supplier_name = Column(String, nullable=True)
    invoice_reference = Column(String, nullable=True)
    source_reference = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    support_file_name = Column(String, nullable=True)
    support_file_url = Column(String(512), nullable=True)
    support_file_size = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    closed_at = Column(DateTime, nullable=True)

    created_by = relationship("PosUser", foreign_keys=[created_by_user_id])
    closed_by = relationship("PosUser", foreign_keys=[closed_by_user_id])
    items = relationship(
        "ReceivingLotItem",
        back_populates="lot",
        cascade="all, delete-orphan",
    )

    @property
    def created_by_user_name(self) -> str | None:
        return self.created_by.name if self.created_by else None

    @property
    def closed_by_user_name(self) -> str | None:
        return self.closed_by.name if self.closed_by else None


class ReceivingLotItem(Base):
    __tablename__ = "receiving_lot_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    lot_id = Column(Integer, ForeignKey("receiving_lots.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    product_name_snapshot = Column(String, nullable=False)
    sku_snapshot = Column(String, nullable=True)
    barcode_snapshot = Column(String, nullable=True)
    label_format_snapshot = Column(String(64), nullable=True)
    qty_received = Column(Float, nullable=False, default=0)
    unit_cost_snapshot = Column(Float, nullable=False, default=0)
    unit_price_snapshot = Column(Float, nullable=False, default=0)
    labels_printed_qty = Column(Integer, nullable=False, default=0)
    is_new_product = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    lot = relationship("ReceivingLot", back_populates="items")
    product = relationship("Product")


class ManualMovementDocument(Base):
    __tablename__ = "manual_movement_documents"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    document_number = Column(String, unique=True, index=True, nullable=True)
    kind = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="open", index=True)
    origin_name = Column(String, nullable=False, default="Metrik web")
    header_json = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    external_reference_type = Column(String, nullable=True)
    external_reference_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    closed_at = Column(DateTime, nullable=True)

    created_by = relationship("PosUser", foreign_keys=[created_by_user_id])
    closed_by = relationship("PosUser", foreign_keys=[closed_by_user_id])
    lines = relationship(
        "ManualMovementDocumentLine",
        back_populates="document",
        cascade="all, delete-orphan",
    )

    @property
    def created_by_user_name(self) -> str | None:
        return self.created_by.name if self.created_by else None

    @property
    def closed_by_user_name(self) -> str | None:
        return self.closed_by.name if self.closed_by else None


class ManualMovementDocumentLine(Base):
    __tablename__ = "manual_movement_document_lines"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    document_id = Column(
        Integer,
        ForeignKey("manual_movement_documents.id"),
        nullable=False,
        index=True,
    )
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    product_name_snapshot = Column(String, nullable=False)
    sku_snapshot = Column(String, nullable=True)
    barcode_snapshot = Column(String, nullable=True)
    qty = Column(Float, nullable=False, default=0)
    unit_cost_snapshot = Column(Float, nullable=True)
    unit_price_snapshot = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    document = relationship("ManualMovementDocument", back_populates="lines")
    product = relationship("Product")


class Sale(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, nullable=False, default="active")
    voided_at = Column(DateTime, nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    void_reason = Column(Text, nullable=True)
    adjustment_reference = Column(String, nullable=True)

    # Número secuencial interno del ticket (visible en POS)
    sale_number = Column(Integer, index=True, nullable=True)

    # Número de documento único tipo "V-000001"
    document_number = Column(String, index=True, nullable=True)

    # Identificador estable generado por el cliente para que reintentar una
    # confirmacion ambigua nunca cree una segunda venta.
    client_request_id = Column(String(64), index=True, nullable=True)

    # Método principal de pago (nuevo campo pensado para reportes)
    main_payment_method = Column(String, nullable=False, default="cash")

    # 🔹 Campo antiguo (para compatibilidad con el código actual y los schemas)
    #    De momento lo mantenemos igual que main_payment_method para no romper nada.
    payment_method = Column(String, nullable=False, default="cash")

    # Totales
    total = Column(Float, nullable=False, default=0)
    paid_amount = Column(Float, nullable=False, default=0)
    change_amount = Column(Float, nullable=False, default=0)

    # Descuentos globales del carrito (si aplican)
    cart_discount_value = Column(Float, nullable=False, default=0)
    cart_discount_percent = Column(Float, nullable=False, default=0)
    surcharge_amount = Column(Float, nullable=False, default=0)
    surcharge_label = Column(String(60), nullable=True)

    # Información de devoluciones
    refunded_total = Column(Float, nullable=False, default=0)
    refund_count = Column(Integer, nullable=False, default=0)

    # Info adicional
    customer_id = Column(Integer, ForeignKey("pos_customers.id"), nullable=True)
    customer_name = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    customer_tax_id = Column(String, nullable=True)
    customer_address = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    pos_name = Column(String, nullable=True)
    station_id = Column(String, ForeignKey("pos_stations.id"), nullable=True)
    vendor_name = Column(String, nullable=True)
    closure_id = Column(Integer, ForeignKey("pos_closures.id"), nullable=True)

    # Relación con ítems de la venta
    items = relationship(
        "SaleItem",
        back_populates="sale",
        cascade="all, delete-orphan",
    )

    # Relación con pagos (para pagos múltiples / parciales)
    payments = relationship(
        "SalePayment",
        back_populates="sale",
        cascade="all, delete-orphan",
    )

    returns = relationship(
        "SaleReturn",
        back_populates="sale",
        cascade="all, delete-orphan",
    )
    changes = relationship(
        "SaleChange",
        back_populates="sale",
        cascade="all, delete-orphan",
    )
    closure = relationship("PosClosure", back_populates="sales")
    station = relationship("PosStation")
    customer = relationship("PosCustomer", back_populates="sales")
    separated_order = relationship(
        "SeparatedOrder",
        back_populates="sale",
        uselist=False,
    )

    @property
    def refunded_balance(self) -> float:
        return max(0.0, float(self.total or 0) - float(self.refunded_total or 0))

    @property
    def refunded_payments(self):
        payments = []
        for sale_return in self.returns:
            payments.extend(sale_return.payments)
        return payments

    @property
    def is_separated(self) -> bool:
        return self.separated_order is not None

    def _primary_payment(self):
        if not self.payments:
            return None
        primary = next((payment for payment in self.payments if payment.is_primary), None)
        return primary or self.payments[0]

    @property
    def initial_payment_method(self) -> str | None:
        payment = self._primary_payment()
        if payment:
            return payment.method
        if self.is_separated:
            return None
        return self.main_payment_method

    @property
    def initial_payment_amount(self) -> float:
        if self.separated_order:
            return float(self.separated_order.initial_payment or 0.0)
        payment = self._primary_payment()
        if payment:
            return float(payment.amount or 0.0)
        return float(self.paid_amount or 0.0)

    @property
    def balance(self) -> float | None:
        if self.separated_order:
            return float(self.separated_order.balance or 0.0)
        return None


class DocumentAdjustment(Base):
    __tablename__ = "document_adjustments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    doc_type = Column(String, nullable=False, index=True)
    doc_id = Column(Integer, nullable=False, index=True)
    adjustment_type = Column(String, nullable=False)
    reason = Column(Text, nullable=True)
    payload = Column(JSON, nullable=True)
    total_delta = Column(Float, nullable=False, default=0)
    payment_delta = Column(Float, nullable=False, default=0)
    is_post_closure = Column(Boolean, nullable=False, default=False)
    original_closure_id = Column(Integer, ForeignKey("pos_closures.id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_by_user_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    created_by = relationship("PosUser")
    original_closure = relationship("PosClosure")


class SaleNumberReservation(Base):
    __tablename__ = "sale_number_reservations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, nullable=False, default="reserved")
    sale_number = Column(Integer, index=True, nullable=False)
    document_number = Column(String, index=True, nullable=False)
    pos_name = Column(String, nullable=True)
    station_id = Column(String, nullable=True)
    reserved_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)

    reserved_by = relationship("PosUser")
    sale = relationship("Sale")


class SaleItem(Base):
    __tablename__ = "sale_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)

    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)

    product_sku = Column(String, nullable=True)
    product_name = Column(String, nullable=False)
    product_barcode = Column(String, nullable=True)

    quantity = Column(Float, nullable=False, default=1)
    unit_price = Column(Float, nullable=False, default=0)

    # Precio original antes del descuento
    unit_price_original = Column(Float, nullable=False, default=0)

    # 🔵 Descuento específico de ESTA línea (0 si no tiene)
    discount = Column(Float, nullable=False, default=0)
    line_discount_value = Column(Float, nullable=False, default=0)

    # Total final de la línea (ya con descuento aplicado)
    total = Column(Float, nullable=False, default=0)
    combo_context_json = Column(JSON, nullable=True)

    sale = relationship("Sale", back_populates="items")


class SalePayment(Base):
    __tablename__ = "sale_payments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)

    # Relación con la venta
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)

    # Método de pago concreto (cash, card, qr, etc.)
    method = Column(String, nullable=False)

    # Monto pagado con este método
    amount = Column(Float, nullable=False, default=0)

    # Para marcar cuál es el principal (ej. el de mayor monto)
    is_primary = Column(Boolean, default=False, nullable=False)

    sale = relationship("Sale", back_populates="payments")


class InvestmentParticipant(Base):
    __tablename__ = "investment_participants"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True, index=True)
    display_name = Column(String(128), nullable=False)
    share_percent = Column(Float, nullable=False, default=0)
    profit_share_percent = Column(Float, nullable=False, default=0)
    capital_share_percent = Column(Float, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    user = relationship("PosUser")


class InvestmentCut(Base):
    __tablename__ = "investment_cuts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    gross_sales = Column(Float, nullable=False, default=0)
    collected_sales = Column(Float, nullable=False, default=0)
    cogs = Column(Float, nullable=False, default=0)
    profit_base = Column(Float, nullable=False, default=0)
    notes = Column(Text, nullable=True)
    reconciled = Column(Boolean, nullable=False, default=False)
    reconciled_at = Column(DateTime, nullable=True)
    reconciled_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    created_by = relationship("PosUser", foreign_keys=[created_by_user_id])
    reconciled_by = relationship("PosUser", foreign_keys=[reconciled_by_user_id])
    allocations = relationship(
        "InvestmentCutAllocation",
        back_populates="cut",
        cascade="all, delete-orphan",
    )


class InvestmentCutAllocation(Base):
    __tablename__ = "investment_cut_allocations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    cut_id = Column(Integer, ForeignKey("investment_cuts.id"), nullable=False, index=True)
    participant_id = Column(Integer, ForeignKey("investment_participants.id"), nullable=False, index=True)
    share_percent = Column(Float, nullable=False, default=0)
    profit_share_percent = Column(Float, nullable=False, default=0)
    capital_share_percent = Column(Float, nullable=False, default=0)
    profit_amount = Column(Float, nullable=False, default=0)
    capital_amount = Column(Float, nullable=False, default=0)
    amount_due = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    cut = relationship("InvestmentCut", back_populates="allocations")
    participant = relationship("InvestmentParticipant")


class InvestmentPayout(Base):
    __tablename__ = "investment_payouts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    participant_id = Column(Integer, ForeignKey("investment_participants.id"), nullable=False, index=True)
    cut_id = Column(Integer, ForeignKey("investment_cuts.id"), nullable=True, index=True)
    amount = Column(Float, nullable=False, default=0)
    paid_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    method = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    participant = relationship("InvestmentParticipant")
    cut = relationship("InvestmentCut")
    created_by = relationship("PosUser")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="payment_methods_tenant_slug_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, index=True, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    allow_change = Column(Boolean, nullable=False, default=False)
    order_index = Column(Integer, nullable=False, default=0)
    color = Column(String(32), nullable=True)
    icon = Column(String(64), nullable=True)
    deleted_at = Column(DateTime, nullable=True)


def default_notifications():
    return {
        "daily_summary_email": False,
        "cash_alert_email": False,
        "cash_alert_sms": False,
        "monthly_report_email": False,
    }


class PosSettings(Base):
    __tablename__ = "pos_settings"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    company_name = Column(String, nullable=False, default="Mi Negocio")
    tax_id = Column(String, nullable=True)
    address = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    theme_mode = Column(String, nullable=False, default="light")
    accent_color = Column(String, nullable=False, default="#0A84FF")
    ticket_footer = Column(Text, nullable=True)
    auto_close_ticket = Column(Boolean, nullable=False, default=False)
    low_stock_alert = Column(Boolean, nullable=False, default=True)
    require_seller_pin = Column(Boolean, nullable=False, default=False)
    notifications = Column(JSON, nullable=False, default=default_notifications)
    logo_url = Column(String, nullable=True)
    ticket_logo_url = Column(String, nullable=True)
    closure_email_recipients = Column(JSON, nullable=True)
    ticket_email_cc = Column(JSON, nullable=True)
    smtp_host = Column(String, nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_user = Column(String, nullable=True)
    smtp_password = Column(String, nullable=True)
    smtp_use_tls = Column(Boolean, nullable=True)
    email_from = Column(String, nullable=True)
    role_permissions = Column(JSON, nullable=True)
    web_pos_send_closure_email = Column(Boolean, nullable=True, default=True)
    station_closure_email_overrides = Column(JSON, nullable=True)
    web_personalization_bindings = Column(JSON, nullable=True)
    web_personalization_home_images = Column(JSON, nullable=True)
    web_brand_collage_images = Column(JSON, nullable=True)
    web_home_sections_mode = Column(String(20), nullable=True, default="categories")


class MonthlyReportDispatch(Base):
    __tablename__ = "monthly_report_dispatches"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    report_year = Column(Integer, nullable=False, index=True)
    report_month = Column(Integer, nullable=False, index=True)
    trigger = Column(String(16), nullable=False, default="manual")
    status = Column(String(16), nullable=False, default="pending")
    recipients = Column(JSON, nullable=True)
    subject = Column(String(180), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PosCustomer(Base):
    __tablename__ = "pos_customers"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    tax_id = Column(String, nullable=True)
    address = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    sales = relationship("Sale", back_populates="customer")
    web_accounts = relationship("WebCustomerAccount", back_populates="customer")


class WebCustomerAccount(Base):
    __tablename__ = "web_customer_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="web_customer_accounts_tenant_email_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    pos_customer_id = Column(Integer, ForeignKey("pos_customers.id"), nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    customer = relationship("PosCustomer", back_populates="web_accounts")
    sessions = relationship(
        "WebCustomerSession",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class WebCustomerSession(Base):
    __tablename__ = "web_customer_sessions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    account_id = Column(Integer, ForeignKey("web_customer_accounts.id"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_reason = Column(String, nullable=True)

    account = relationship("WebCustomerAccount", back_populates="sessions")


class WebCart(Base):
    __tablename__ = "web_carts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    account_id = Column(Integer, ForeignKey("web_customer_accounts.id"), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="active")
    currency = Column(String(8), nullable=False, default="COP")
    coupon_code = Column(String(64), nullable=True)
    coupon_discount_percent = Column(Float, nullable=False, default=0)
    coupon_discount_code_id = Column(Integer, ForeignKey("web_discount_codes.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    converted_at = Column(DateTime, nullable=True)

    account = relationship("WebCustomerAccount")
    coupon = relationship("WebDiscountCode")
    items = relationship(
        "WebCartItem",
        back_populates="cart",
        cascade="all, delete-orphan",
    )


class WebCartItem(Base):
    __tablename__ = "web_cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="web_cart_items_cart_product_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    cart_id = Column(Integer, ForeignKey("web_carts.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False, default=1)
    unit_price_snapshot = Column(Float, nullable=False, default=0)
    combo_context_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    cart = relationship("WebCart", back_populates="items")
    product = relationship("Product")


class WebDiscountCode(Base):
    __tablename__ = "web_discount_codes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="web_discount_codes_tenant_code_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    code = Column(String(64), nullable=False, index=True)
    discount_type = Column(String(16), nullable=False, default="percent")
    discount_value = Column(Float, nullable=False, default=0)
    discount_percent = Column(Float, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    max_uses = Column(Integer, nullable=True)
    uses_count = Column(Integer, nullable=False, default=0)
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    creator = relationship("PosUser")


class WebCatalogCategory(Base):
    __tablename__ = "web_catalog_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="web_catalog_categories_tenant_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    key = Column(String(64), nullable=False, index=True)
    parent_key = Column(String(64), nullable=True, index=True)
    name = Column(String(120), nullable=False)
    image_url = Column(String(512), nullable=True)
    tile_color = Column(String(7), nullable=True)
    home_featured = Column(Boolean, nullable=False, default=False)
    home_featured_order = Column(Integer, nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class WebCatalogDescriptionTemplate(Base):
    __tablename__ = "web_catalog_description_templates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "template_key",
            name="web_catalog_description_templates_tenant_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    template_key = Column(String(64), nullable=False, index=True)
    label = Column(String(120), nullable=False)
    assigned_category_key = Column(String(64), nullable=True, index=True)
    keywords_json = Column(Text, nullable=False, default="[]")
    paragraph1 = Column(Text, nullable=False, default="")
    paragraph2 = Column(Text, nullable=False, default="")
    paragraph3 = Column(Text, nullable=False, default="")
    closing = Column(Text, nullable=False, default="")
    sort_order = Column(Integer, nullable=False, default=0)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True, index=True)
    updated_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    creator = relationship("PosUser", foreign_keys=[created_by_user_id])
    updater = relationship("PosUser", foreign_keys=[updated_by_user_id])


class WebCatalogHomeSlider(Base):
    __tablename__ = "web_catalog_home_sliders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slot", name="web_catalog_home_sliders_tenant_slot_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    slot = Column(Integer, nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    image_url = Column(String(512), nullable=True)
    mobile_image_url = Column(String(512), nullable=True)
    alt_text = Column(String(180), nullable=True)
    cta_label = Column(String(90), nullable=True)
    cta_x_percent = Column(Float, nullable=False, default=50)
    cta_y_percent = Column(Float, nullable=False, default=80)
    link_type = Column(String(24), nullable=False, default="catalogo")
    link_value = Column(String(255), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    content_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class WebCatalogHomeVideo(Base):
    __tablename__ = "web_catalog_home_videos"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slot", name="web_catalog_home_videos_tenant_slot_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    slot = Column(Integer, nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    video_url = Column(String(512), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    content_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class WebCatalogCombo(Base):
    __tablename__ = "web_combos"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="web_combos_tenant_slug_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String(180), nullable=False)
    slug = Column(String(160), nullable=False, index=True)
    short_description = Column(String(280), nullable=True)
    long_description = Column(Text, nullable=True)
    image_url = Column(String(512), nullable=True)
    image_thumb_url = Column(String(512), nullable=True)
    gallery_urls = Column(JSON, nullable=False, default=list)
    video_url = Column(String(512), nullable=True)
    badge_text = Column(String(80), nullable=True)
    badge_color = Column(String(16), nullable=True)
    category_key = Column(String(64), nullable=True, index=True)
    price = Column(Float, nullable=False, default=0)
    compare_price = Column(Float, nullable=True)
    price_mode = Column(String(24), nullable=False, default="auto")
    stock_mode = Column(String(24), nullable=False, default="components")
    published = Column(Boolean, nullable=False, default=False)
    featured = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    visible_when_out_of_stock = Column(Boolean, nullable=False, default=True)
    active = Column(Boolean, nullable=False, default=True)
    warranty_text = Column(String(160), nullable=True)
    technical_specs = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    items = relationship(
        "WebCatalogComboItem",
        back_populates="combo",
        cascade="all, delete-orphan",
        order_by="WebCatalogComboItem.sort_order",
    )


class WebCatalogComboItem(Base):
    __tablename__ = "web_combo_items"
    __table_args__ = (
        UniqueConstraint("combo_id", "product_id", name="web_combo_items_combo_product_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    combo_id = Column(Integer, ForeignKey("web_combos.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False, default=1)
    required = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    product_name_snapshot = Column(String, nullable=False)
    product_sku_snapshot = Column(String, nullable=True)
    product_slug_snapshot = Column(String(160), nullable=True)
    product_image_url_snapshot = Column(String(512), nullable=True)
    product_image_thumb_url_snapshot = Column(String(512), nullable=True)
    product_brand_snapshot = Column(String, nullable=True)
    product_price_snapshot = Column(Float, nullable=False, default=0)
    product_price_attributed = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    combo = relationship("WebCatalogCombo", back_populates="items")
    product = relationship("Product")


class WebOrder(Base):
    __tablename__ = "web_orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "web_order_number", name="web_orders_tenant_number_key"),
        UniqueConstraint("tenant_id", "document_number", name="web_orders_tenant_document_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    web_order_number = Column(Integer, index=True, nullable=True)
    document_number = Column(String, index=True, nullable=True)
    account_id = Column(Integer, ForeignKey("web_customer_accounts.id"), nullable=False, index=True)
    pos_customer_id = Column(Integer, ForeignKey("pos_customers.id"), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="pending_payment")
    payment_status = Column(String(32), nullable=False, default="pending")
    fulfillment_status = Column(String(32), nullable=False, default="pending")
    customer_name = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    customer_tax_id = Column(String, nullable=True)
    customer_address = Column(String, nullable=True)
    subtotal = Column(Float, nullable=False, default=0)
    discount_amount = Column(Float, nullable=False, default=0)
    coupon_code = Column(String(64), nullable=True)
    coupon_discount_percent = Column(Float, nullable=False, default=0)
    coupon_discount_code_id = Column(Integer, ForeignKey("web_discount_codes.id"), nullable=True, index=True)
    coupon_consumed_at = Column(DateTime, nullable=True)
    shipping_amount = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    currency = Column(String(8), nullable=False, default="COP")
    notes = Column(Text, nullable=True)
    checkout_context_json = deferred(Column(JSON, nullable=True))
    submitted_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    converted_to_sale_at = Column(DateTime, nullable=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True, index=True)
    sale_document_number = Column(String, nullable=True)
    customer_approval_email_sent_at = Column(DateTime, nullable=True)
    customer_approval_email_last_error = Column(Text, nullable=True)
    internal_approval_email_sent_at = Column(DateTime, nullable=True)
    internal_approval_email_last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    account = relationship("WebCustomerAccount")
    customer = relationship("PosCustomer")
    coupon = relationship("WebDiscountCode")
    sale = relationship("Sale")
    items = relationship(
        "WebOrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
    )
    payments = relationship(
        "WebOrderPayment",
        back_populates="order",
        cascade="all, delete-orphan",
    )
    status_logs = relationship(
        "WebOrderStatusLog",
        back_populates="order",
        cascade="all, delete-orphan",
    )


class WebOrderItem(Base):
    __tablename__ = "web_order_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    web_order_id = Column(Integer, ForeignKey("web_orders.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    product_name_snapshot = Column(String, nullable=False)
    product_sku_snapshot = Column(String, nullable=True)
    product_barcode_snapshot = Column(String, nullable=True)
    unit_price_snapshot = Column(Float, nullable=False, default=0)
    quantity = Column(Float, nullable=False, default=1)
    line_discount_value = Column(Float, nullable=False, default=0)
    line_total = Column(Float, nullable=False, default=0)
    combo_context_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    order = relationship("WebOrder", back_populates="items")
    product = relationship("Product")


class WebOrderPayment(Base):
    __tablename__ = "web_order_payments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    web_order_id = Column(Integer, ForeignKey("web_orders.id"), nullable=False, index=True)
    provider = Column(String(64), nullable=True)
    provider_reference = Column(String, nullable=True)
    method = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    amount = Column(Float, nullable=False, default=0)
    currency = Column(String(8), nullable=False, default="COP")
    raw_payload = Column(JSON, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    order = relationship("WebOrder", back_populates="payments")


class WebOrderStatusLog(Base):
    __tablename__ = "web_order_status_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    web_order_id = Column(Integer, ForeignKey("web_orders.id"), nullable=False, index=True)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=False)
    note = Column(Text, nullable=True)
    actor_type = Column(String(32), nullable=False, default="system")
    actor_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    order = relationship("WebOrder", back_populates="status_logs")
    actor_user = relationship("PosUser")


class HREmployee(Base):
    __tablename__ = "hr_employees"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    email = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, default="Activo")
    phone = Column(String, nullable=True)
    position = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    avatar_url = Column(String(512), nullable=True)
    birth_date = Column(Date, nullable=True)
    location = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    payroll_frequency = Column(String, nullable=True)
    payroll_amount = Column(Float, nullable=True)
    payroll_currency = Column(String(8), nullable=True)
    payroll_payment_method = Column(String, nullable=True)
    payroll_day_of_week = Column(String, nullable=True)
    payroll_day_of_month = Column(Integer, nullable=True)
    payroll_last_paid_at = Column(Date, nullable=True)
    payroll_next_due_at = Column(Date, nullable=True)
    payroll_reference = Column(String, nullable=True)
    payroll_notes = Column(Text, nullable=True)
    show_in_schedule = Column(Boolean, nullable=False, default=True)
    row_color = Column(String(7), nullable=True)
    active_from = Column(Date, nullable=True)
    active_until = Column(Date, nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    system_user = relationship("PosUser", back_populates="employee", uselist=False)
    documents = relationship(
        "HREmployeeDocument",
        back_populates="employee",
        cascade="all, delete-orphan",
    )
    schedule_shifts = relationship(
        "ScheduleShift",
        back_populates="employee",
        cascade="all, delete-orphan",
    )


class ScheduleTemplate(Base):
    __tablename__ = "schedule_templates"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String, nullable=False)
    break_minutes = Column(Integer, nullable=False, default=0)
    color = Column(String(16), nullable=True)
    position = Column(String, nullable=True)
    is_time_off = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    shifts = relationship("ScheduleShift", back_populates="template")


class ScheduleWeek(Base):
    __tablename__ = "schedule_weeks"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    week_start = Column(Date, nullable=False, unique=True, index=True)
    status = Column(String, nullable=False, default="draft")
    notes = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True)
    published_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    published_by_user = relationship("PosUser", foreign_keys=[published_by_user_id])
    shifts = relationship(
        "ScheduleShift",
        back_populates="week",
        cascade="all, delete-orphan",
    )


class ScheduleShift(Base):
    __tablename__ = "schedule_shifts"
    __table_args__ = (
        UniqueConstraint("week_id", "employee_id", "shift_date", name="uq_schedule_shift_cell"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    week_id = Column(Integer, ForeignKey("schedule_weeks.id"), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("hr_employees.id"), nullable=False, index=True)
    shift_date = Column(Date, nullable=False, index=True)
    start_time = Column(String, nullable=True)
    end_time = Column(String, nullable=True)
    break_minutes = Column(Integer, nullable=False, default=0)
    position = Column(String, nullable=True)
    color = Column(String(16), nullable=True)
    note = Column(Text, nullable=True)
    is_time_off = Column(Boolean, nullable=False, default=False)
    source_template_id = Column(
        Integer,
        ForeignKey("schedule_templates.id"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    week = relationship("ScheduleWeek", back_populates="shifts")
    employee = relationship("HREmployee", back_populates="schedule_shifts")
    template = relationship("ScheduleTemplate", back_populates="shifts")


class PosUser(Base):
    __tablename__ = "pos_users"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True, index=True)
    role = Column(String, nullable=False, default="Vendedor")
    status = Column(String, nullable=False, default="Activo")
    is_active = Column(Boolean, nullable=False, default=True)
    password_hash = Column(String, nullable=False)
    pin_hash = Column(String, nullable=True)
    last_login = Column(DateTime, nullable=True)
    phone = Column(String, nullable=True)
    position = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    avatar_url = Column(String(512), nullable=True)
    birth_date = Column(Date, nullable=True)
    location = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    employee_id = Column(Integer, ForeignKey("hr_employees.id"), nullable=True, index=True)
    invited_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    stations = relationship(
        "PosStation",
        back_populates="user",
        foreign_keys="PosStation.pos_user_id",
    )
    sessions = relationship("PosSession", back_populates="user")
    documents = relationship("PosUserDocument", back_populates="user")
    employee = relationship("HREmployee", back_populates="system_user")


class UserNotification(Base):
    __tablename__ = "user_notifications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "dedupe_key",
            name="uq_user_notifications_recipient_dedupe",
        ),
        Index(
            "ix_user_notifications_inbox",
            "tenant_id",
            "user_id",
            "dismissed_at",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    source = Column(String(32), nullable=False, default="system")
    category = Column(String(48), nullable=False, default="general")
    severity = Column(String(16), nullable=False, default="info")
    module_id = Column(String(48), nullable=True)
    required_permission = Column(String(96), nullable=True)
    title = Column(String(160), nullable=False)
    message = Column(Text, nullable=False)
    action_label = Column(String(80), nullable=True)
    action_href = Column(String(512), nullable=True)
    dedupe_key = Column(String(160), nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    read_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    tenant = relationship("Tenant")
    user = relationship("PosUser")


class PosSession(Base):
    __tablename__ = "pos_sessions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False, index=True)
    session_type = Column(String, nullable=False)
    station_id = Column(String, nullable=True)
    device_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_reason = Column(String, nullable=True)

    user = relationship("PosUser", back_populates="sessions")


class ReportFavorite(Base):
    __tablename__ = "report_favorites"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "preset_id",
            name="report_favorites_tenant_user_preset_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    preset_id = Column(String(80), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("PosUser")


class LegacyImportBatch(Base):
    __tablename__ = "legacy_import_batches"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "batch_key",
            name="legacy_import_batches_tenant_batch_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    source_system = Column(String(40), nullable=False, default="aronium")
    batch_key = Column(String(80), nullable=False)
    title = Column(String(140), nullable=False)
    status = Column(String(24), nullable=False, default="draft")
    note = Column(Text, nullable=True)
    uploaded_sales_path = Column(String(512), nullable=True)
    uploaded_items_path = Column(String(512), nullable=True)
    uploaded_payments_path = Column(String(512), nullable=True)
    uploaded_refunds_path = Column(String(512), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class LegacySale(Base):
    __tablename__ = "legacy_sales"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_document_id",
            name="legacy_sales_tenant_source_doc_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    import_batch_id = Column(Integer, ForeignKey("legacy_import_batches.id"), nullable=False, index=True)
    source_system = Column(String(40), nullable=False, default="aronium")
    source_document_id = Column(String(80), nullable=False, index=True)
    source_document_number = Column(String(80), nullable=True, index=True)
    display_document_number = Column(String(100), nullable=True, index=True)
    sale_number = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, index=True)
    pos_name = Column(String(120), nullable=True)
    vendor_name = Column(String(120), nullable=True)
    customer_name = Column(String(160), nullable=True)
    customer_phone = Column(String(80), nullable=True)
    customer_email = Column(String(160), nullable=True)
    payment_method = Column(String(80), nullable=True)
    main_payment_method = Column(String(80), nullable=True)
    total = Column(Float, nullable=False, default=0.0)
    paid_amount = Column(Float, nullable=False, default=0.0)
    change_amount = Column(Float, nullable=False, default=0.0)
    status = Column(String(32), nullable=False, default="completed")
    imported_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LegacySaleItem(Base):
    __tablename__ = "legacy_sale_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    import_batch_id = Column(Integer, ForeignKey("legacy_import_batches.id"), nullable=False, index=True)
    legacy_sale_id = Column(Integer, ForeignKey("legacy_sales.id"), nullable=False, index=True)
    source_item_id = Column(String(80), nullable=True)
    product_id = Column(Integer, nullable=True, index=True)
    product_sku = Column(String(120), nullable=True, index=True)
    product_name = Column(String(255), nullable=False)
    product_group = Column(String(255), nullable=True, index=True)
    quantity = Column(Float, nullable=False, default=0.0)
    unit_price = Column(Float, nullable=False, default=0.0)
    line_discount_value = Column(Float, nullable=False, default=0.0)
    total = Column(Float, nullable=False, default=0.0)
    imported_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LegacyPayment(Base):
    __tablename__ = "legacy_payments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    import_batch_id = Column(Integer, ForeignKey("legacy_import_batches.id"), nullable=False, index=True)
    legacy_sale_id = Column(Integer, ForeignKey("legacy_sales.id"), nullable=False, index=True)
    source_payment_id = Column(String(80), nullable=True)
    method = Column(String(80), nullable=False)
    amount = Column(Float, nullable=False, default=0.0)
    imported_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PosUserDocument(Base):
    __tablename__ = "pos_user_documents"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    file_name = Column(String, nullable=False)
    file_url = Column(String(512), nullable=False)
    file_size = Column(Integer, nullable=False, default=0)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("PosUser", back_populates="documents")


class HREmployeeDocument(Base):
    __tablename__ = "hr_employee_documents"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    employee_id = Column(Integer, ForeignKey("hr_employees.id"), nullable=False, index=True)
    file_name = Column(String, nullable=False)
    file_url = Column(String(512), nullable=False)
    file_size = Column(Integer, nullable=False, default=0)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    employee = relationship("HREmployee", back_populates="documents")


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("PosUser")


class PosStation(Base):
    __tablename__ = "pos_stations"

    id = Column(String, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    label = Column(String, nullable=False)
    pos_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    station_email = Column(String, nullable=True)
    station_password_hash = Column(String, nullable=True)
    pin_hash = Column(String, nullable=True)
    station_type = Column(String, nullable=False, default="desktop")
    parent_station_id = Column(String, ForeignKey("pos_stations.id"), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    failed_attempts = Column(Integer, nullable=False, default=0)
    last_login_at = Column(DateTime, nullable=True)
    last_failed_at = Column(DateTime, nullable=True)
    bound_device_id = Column(String, nullable=True)
    bound_device_label = Column(String, nullable=True)
    bound_at = Column(DateTime, nullable=True)
    bound_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    bound_by_user_name = Column(String, nullable=True)
    printer_mode = Column(String, nullable=True)
    printer_name = Column(String, nullable=True)
    printer_width = Column(String, nullable=True)
    printer_auto_open_drawer = Column(Boolean, nullable=True)
    printer_show_drawer_button = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    user = relationship(
        "PosUser",
        back_populates="stations",
        foreign_keys=[pos_user_id],
    )
    parent_station = relationship(
        "PosStation",
        remote_side=[id],
        foreign_keys=[parent_station_id],
    )


class StockDevice(Base):
    __tablename__ = "stock_devices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_stock_device_tenant_name"),
    )

    id = Column(String, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String(120), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    bound_device_id = Column(String, nullable=True)
    bound_device_label = Column(String, nullable=True)
    setup_code_hash = Column(String, nullable=True)
    setup_code_expires_at = Column(DateTime, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    last_seen_at = Column(DateTime, nullable=True)

    created_by = relationship("PosUser", foreign_keys=[created_by_user_id])

    @property
    def created_by_user_name(self) -> str | None:
        return self.created_by.name if self.created_by else None

    @property
    def has_pending_setup_code(self) -> bool:
        return bool(
            self.setup_code_hash
            and self.setup_code_expires_at
            and self.setup_code_expires_at > datetime.utcnow()
        )


class PosStationNotice(Base):
    __tablename__ = "pos_station_notices"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    station_id = Column(String, ForeignKey("pos_stations.id"), nullable=False, index=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    dismissed_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)

    station = relationship("PosStation")
    created_by_user = relationship("PosUser", foreign_keys=[created_by_user_id])
    dismissed_by_user = relationship("PosUser", foreign_keys=[dismissed_by_user_id])


class PosClosure(Base):
    __tablename__ = "pos_closures"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    pos_name = Column(String, nullable=True)
    pos_identifier = Column(String, nullable=True)
    station_id = Column(String, ForeignKey("pos_stations.id"), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    closed_by_user_name = Column(String, nullable=False)
    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    consecutive = Column(String, index=True, nullable=True)

    total_amount = Column(Float, nullable=False, default=0)
    total_cash = Column(Float, nullable=False, default=0)
    total_card = Column(Float, nullable=False, default=0)
    total_qr = Column(Float, nullable=False, default=0)
    total_nequi = Column(Float, nullable=False, default=0)
    total_daviplata = Column(Float, nullable=False, default=0)
    total_credit = Column(Float, nullable=False, default=0)
    total_refunds = Column(Float, nullable=False, default=0)
    net_amount = Column(Float, nullable=False, default=0)
    counted_cash = Column(Float, nullable=False, default=0)
    difference = Column(Float, nullable=False, default=0)
    sales_count = Column(Integer, nullable=False, default=0)
    change_extra_total = Column(Float, nullable=False, default=0)
    change_refund_total = Column(Float, nullable=False, default=0)
    change_count = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=True)
    total_surcharge = Column(Float, nullable=False, default=0)
    station_breakdown = Column(JSON, nullable=True)
    methods_breakdown = Column(JSON, nullable=True)
    separated_summary = Column(JSON, nullable=True)
    user_breakdown = Column(JSON, nullable=True)

    closed_by_user = relationship("PosUser")
    station = relationship("PosStation")
    sales = relationship("Sale", back_populates="closure")
    separated_payments = relationship(
        "SeparatedOrderPayment",
        back_populates="closure",
    )
    returns = relationship("SaleReturn", back_populates="closure")


class SeparatedOrder(Base):
    __tablename__ = "separated_orders"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, unique=True)
    customer_id = Column(Integer, ForeignKey("pos_customers.id"), nullable=True)
    customer_name = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    total_amount = Column(Float, nullable=False, default=0)
    initial_payment = Column(Float, nullable=False, default=0)
    balance = Column(Float, nullable=False, default=0)
    due_date = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default="reservado")
    sale_document_number = Column(String, nullable=False)
    sale_number = Column(Integer, nullable=True)
    barcode = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    surcharge_amount = Column(Float, nullable=False, default=0)
    surcharge_label = Column(String(60), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    sale = relationship("Sale", back_populates="separated_order")
    customer = relationship("PosCustomer")
    payments = relationship(
        "SeparatedOrderPayment",
        back_populates="separated_order",
        cascade="all, delete-orphan",
    )

    @property
    def initial_payments(self):
        sale = getattr(self, "sale", None)
        if sale and getattr(sale, "payments", None):
            return sale.payments
        return []


class SeparatedOrderPayment(Base):
    __tablename__ = "separated_order_payments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    separated_order_id = Column(
        Integer,
        ForeignKey("separated_orders.id"),
        nullable=False,
        index=True,
    )
    method = Column(String, nullable=False)
    amount = Column(Float, nullable=False, default=0)
    paid_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reference = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="active")
    voided_at = Column(DateTime, nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    void_reason = Column(Text, nullable=True)
    adjustment_reference = Column(String, nullable=True)
    closure_id = Column(Integer, ForeignKey("pos_closures.id"), nullable=True)
    station_id = Column(String, ForeignKey("pos_stations.id"), nullable=True)

    separated_order = relationship("SeparatedOrder", back_populates="payments")
    closure = relationship("PosClosure", back_populates="separated_payments")
    station = relationship("PosStation")

class SaleReturn(Base):
    __tablename__ = "sale_returns"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)

    document_number = Column(String, index=True, nullable=True)
    status = Column(String, nullable=False, default="confirmed")
    notes = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    voided_at = Column(DateTime, nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    void_reason = Column(Text, nullable=True)
    adjustment_reference = Column(String, nullable=True)
    total_refund = Column(Float, nullable=False, default=0)
    closure_id = Column(Integer, ForeignKey("pos_closures.id"), nullable=True)

    sale = relationship("Sale", back_populates="returns")
    closure = relationship("PosClosure", back_populates="returns")
    items = relationship(
        "SaleReturnItem",
        back_populates="return_",
        cascade="all, delete-orphan",
    )
    payments = relationship(
        "SaleReturnPayment",
        back_populates="return_",
        cascade="all, delete-orphan",
    )

    @property
    def station_id(self) -> Optional[str]:
        return self.sale.station_id if self.sale else None

    @property
    def pos_name(self) -> Optional[str]:
        return self.sale.pos_name if self.sale else None

    @property
    def sale_document_number(self) -> Optional[str]:
        return self.sale.document_number if self.sale else None


class SaleReturnItem(Base):
    __tablename__ = "sale_return_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    return_id = Column(
        Integer,
        ForeignKey("sale_returns.id"),
        nullable=False,
        index=True,
    )
    sale_item_id = Column(Integer, ForeignKey("sale_items.id"), nullable=False)

    product_id = Column(Integer, nullable=False)
    product_name = Column(String, nullable=False)
    product_sku = Column(String, nullable=True)
    product_barcode = Column(String, nullable=True)

    reason = Column(String, nullable=True)
    quantity = Column(Float, nullable=False, default=0)

    unit_price_original = Column(Float, nullable=False, default=0)
    unit_price_net = Column(Float, nullable=False, default=0)
    line_discount_value = Column(Float, nullable=False, default=0)
    cart_discount_share = Column(Float, nullable=False, default=0)
    total_refund = Column(Float, nullable=False, default=0)

    return_ = relationship("SaleReturn", back_populates="items")
    sale_item = relationship("SaleItem")


class SaleReturnPayment(Base):
    __tablename__ = "sale_return_payments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    return_id = Column(
        Integer,
        ForeignKey("sale_returns.id"),
        nullable=False,
        index=True,
    )

    method = Column(String, nullable=False)
    amount = Column(Float, nullable=False, default=0)

    return_ = relationship("SaleReturn", back_populates="payments")


class SaleChange(Base):
    __tablename__ = "sale_changes"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)
    closure_id = Column(Integer, ForeignKey("pos_closures.id"), nullable=True)

    document_number = Column(String, index=True, nullable=True)
    status = Column(String, nullable=False, default="confirmed")
    notes = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    voided_at = Column(DateTime, nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    void_reason = Column(Text, nullable=True)
    adjustment_reference = Column(String, nullable=True)
    pos_name = Column(String, nullable=True)
    seller_name = Column(String, nullable=True)
    station_id = Column(String, ForeignKey("pos_stations.id"), nullable=True)

    total_credit = Column(Float, nullable=False, default=0)
    total_new = Column(Float, nullable=False, default=0)
    net_total = Column(Float, nullable=False, default=0)
    extra_payment = Column(Float, nullable=False, default=0)
    refund_due = Column(Float, nullable=False, default=0)

    sale = relationship("Sale", back_populates="changes")
    closure = relationship("PosClosure")
    items_returned = relationship(
        "SaleChangeReturnItem",
        back_populates="change",
        cascade="all, delete-orphan",
    )
    items_new = relationship(
        "SaleChangeNewItem",
        back_populates="change",
        cascade="all, delete-orphan",
    )
    payments = relationship(
        "SaleChangePayment",
        back_populates="change",
        cascade="all, delete-orphan",
    )

    @property
    def sale_document_number(self) -> str | None:
        return self.sale.document_number if self.sale else None


class SaleChangeReturnItem(Base):
    __tablename__ = "sale_change_return_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    change_id = Column(
        Integer,
        ForeignKey("sale_changes.id"),
        nullable=False,
        index=True,
    )
    sale_item_id = Column(Integer, ForeignKey("sale_items.id"), nullable=False)

    product_id = Column(Integer, nullable=False)
    product_name = Column(String, nullable=False)
    product_sku = Column(String, nullable=True)
    product_barcode = Column(String, nullable=True)

    reason = Column(String, nullable=True)
    quantity = Column(Float, nullable=False, default=0)

    unit_price_original = Column(Float, nullable=False, default=0)
    unit_price_net = Column(Float, nullable=False, default=0)
    line_discount_value = Column(Float, nullable=False, default=0)
    cart_discount_share = Column(Float, nullable=False, default=0)
    total_credit = Column(Float, nullable=False, default=0)

    change = relationship("SaleChange", back_populates="items_returned")
    sale_item = relationship("SaleItem")


class SaleChangeNewItem(Base):
    __tablename__ = "sale_change_new_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    change_id = Column(
        Integer,
        ForeignKey("sale_changes.id"),
        nullable=False,
        index=True,
    )

    product_id = Column(Integer, nullable=False)
    product_name = Column(String, nullable=False)
    product_sku = Column(String, nullable=True)
    product_barcode = Column(String, nullable=True)
    quantity = Column(Float, nullable=False, default=0)
    unit_price = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)

    change = relationship("SaleChange", back_populates="items_new")


class SaleChangePayment(Base):
    __tablename__ = "sale_change_payments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    change_id = Column(
        Integer,
        ForeignKey("sale_changes.id"),
        nullable=False,
        index=True,
    )

    method = Column(String, nullable=False)
    amount = Column(Float, nullable=False, default=0)

    change = relationship("SaleChange", back_populates="payments")
