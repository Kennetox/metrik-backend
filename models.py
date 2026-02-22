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
    Text,
)
from sqlalchemy.orm import relationship

from database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, index=True, nullable=True)
    name = Column(String, index=True, nullable=False)

    price = Column(Float, nullable=False)
    cost = Column(Float, nullable=False)

    barcode = Column(String, nullable=True)
    unit = Column(String, nullable=True)
    image_url = Column(String(512), nullable=True)
    image_thumb_url = Column(String(512), nullable=True)
    tile_color = Column(String(7), nullable=True)

    stock_min = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    service = Column(Boolean, default=False)
    includes_tax = Column(Boolean, default=False)
    preferred_qty = Column(Integer, default=0)
    reorder_point = Column(Integer, default=0)
    low_stock_alert = Column(Boolean, default=False)
    allow_price_change = Column(Boolean, default=False)

    # info adicional
    group_name = Column(String, nullable=True)   # viene de 'grupo' en Excel
    brand = Column(String, nullable=True)        # 'marca'
    supplier = Column(String, nullable=True)
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


class Sale(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, nullable=False, default="active")
    voided_at = Column(DateTime, nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    void_reason = Column(Text, nullable=True)
    adjustment_reference = Column(String, nullable=True)

    # Número secuencial interno del ticket (visible en POS)
    sale_number = Column(Integer, index=True, nullable=True)

    # Número de documento único tipo "V-000001"
    document_number = Column(String, unique=True, index=True, nullable=True)

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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, nullable=False, default="reserved")
    sale_number = Column(Integer, unique=True, index=True, nullable=False)
    document_number = Column(String, unique=True, index=True, nullable=False)
    pos_name = Column(String, nullable=True)
    station_id = Column(String, nullable=True)
    reserved_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)

    reserved_by = relationship("PosUser")
    sale = relationship("Sale")


class SaleItem(Base):
    __tablename__ = "sale_items"

    id = Column(Integer, primary_key=True, index=True)

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

    sale = relationship("Sale", back_populates="items")


class SalePayment(Base):
    __tablename__ = "sale_payments"

    id = Column(Integer, primary_key=True, index=True)

    # Relación con la venta
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)

    # Método de pago concreto (cash, card, qr, etc.)
    method = Column(String, nullable=False)

    # Monto pagado con este método
    amount = Column(Float, nullable=False, default=0)

    # Para marcar cuál es el principal (ej. el de mayor monto)
    is_primary = Column(Boolean, default=False, nullable=False)

    sale = relationship("Sale", back_populates="payments")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
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

    id = Column(Integer, primary_key=True, default=1)
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


class PosCustomer(Base):
    __tablename__ = "pos_customers"

    id = Column(Integer, primary_key=True, index=True)
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


class HREmployee(Base):
    __tablename__ = "hr_employees"

    id = Column(Integer, primary_key=True, index=True)
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


class PosUser(Base):
    __tablename__ = "pos_users"

    id = Column(Integer, primary_key=True, index=True)
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


class PosSession(Base):
    __tablename__ = "pos_sessions"

    id = Column(Integer, primary_key=True, index=True)
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


class PosUserDocument(Base):
    __tablename__ = "pos_user_documents"

    id = Column(Integer, primary_key=True, index=True)
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
    user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("PosUser")


class PosStation(Base):
    __tablename__ = "pos_stations"

    id = Column(String, primary_key=True)
    label = Column(String, nullable=False)
    pos_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=True)
    station_email = Column(String, nullable=True)
    station_password_hash = Column(String, nullable=True)
    pin_hash = Column(String, nullable=True)
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


class PosStationNotice(Base):
    __tablename__ = "pos_station_notices"

    id = Column(Integer, primary_key=True, index=True)
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
    pos_name = Column(String, nullable=True)
    pos_identifier = Column(String, nullable=True)
    station_id = Column(String, ForeignKey("pos_stations.id"), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    closed_by_user_name = Column(String, nullable=False)
    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    consecutive = Column(String, unique=True, index=True, nullable=True)

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


class SeparatedOrderPayment(Base):
    __tablename__ = "separated_order_payments"

    id = Column(Integer, primary_key=True, index=True)
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
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)

    document_number = Column(String, unique=True, index=True, nullable=True)
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
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)
    closure_id = Column(Integer, ForeignKey("pos_closures.id"), nullable=True)

    document_number = Column(String, unique=True, index=True, nullable=True)
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


class SaleChangeReturnItem(Base):
    __tablename__ = "sale_change_return_items"

    id = Column(Integer, primary_key=True, index=True)
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
    change_id = Column(
        Integer,
        ForeignKey("sale_changes.id"),
        nullable=False,
        index=True,
    )

    method = Column(String, nullable=False)
    amount = Column(Float, nullable=False, default=0)

    change = relationship("SaleChange", back_populates="payments")
