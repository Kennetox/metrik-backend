from datetime import datetime, date
from typing import Dict, Optional, List, Literal, Annotated

from pydantic import BaseModel, EmailStr, constr, Field, field_validator, ConfigDict


NonEmptyStr = Annotated[str, Field(strip_whitespace=True, min_length=1)]
SlugStr = Annotated[str, Field(strip_whitespace=True, pattern=r"^[a-z0-9_-]+$")]


# ===================== PRODUCTS =====================


class ProductBase(BaseModel):
    sku: Optional[str] = None
    name: str
    price: float
    cost: float
    barcode: Optional[str] = None
    unit: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )
    stock_min: int = 0
    preferred_qty: int = 0
    reorder_point: int = 0
    low_stock_alert: bool = False
    allow_price_change: bool = False

    active: bool = True
    service: bool = False
    includes_tax: bool = False
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    price: Optional[float] = None
    cost: Optional[float] = None
    barcode: Optional[str] = None
    unit: Optional[str] = None
    stock_min: Optional[int] = None
    preferred_qty: Optional[int] = None
    reorder_point: Optional[int] = None
    low_stock_alert: Optional[bool] = None
    allow_price_change: Optional[bool] = None
    active: Optional[bool] = None
    service: Optional[bool] = None
    includes_tax: Optional[bool] = None
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )


class ProductGroupBase(BaseModel):
    path: str
    display_name: str
    parent_path: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )


class ProductGroupCreate(ProductGroupBase):
    pass


class ProductGroupUpdate(BaseModel):
    path: Optional[str] = None
    display_name: Optional[str] = None
    parent_path: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )


class ProductGroupRead(ProductGroupBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductRead(ProductBase):
    id: int
    group_meta: Optional[ProductGroupRead] = None

    class Config:
        from_attributes = True


class LabelExportItem(BaseModel):
    product_id: int
    sku: Optional[str] = None
    barcode: Optional[str] = None
    name: str
    price: float
    quantity: int = Field(ge=1)


class LabelExportRequest(BaseModel):
    items: List[LabelExportItem]


# ===================== SALES / ITEMS / PAYMENTS =====================


class SaleItemBase(BaseModel):
    product_id: int
    quantity: float
    unit_price: float
    unit_price_original: Optional[float] = None
    product_sku: Optional[str] = None
    product_name: str
    product_barcode: Optional[str] = None

    # 🔵 Descuento específico de esta línea
    discount: float = 0.0  # compatibilidad legacy
    line_discount_value: Optional[float] = None


class SaleItemCreate(SaleItemBase):
    pass


class SaleItemRead(SaleItemBase):
    id: int
    total: float
    unit_price_original: float
    line_discount_value: float

    class Config:
        from_attributes = True


class SalePaymentBase(BaseModel):
    method: str
    amount: float


class SalePaymentCreate(SalePaymentBase):
    pass


class SalePaymentRead(SalePaymentBase):
    id: int

    class Config:
        from_attributes = True


class NotificationSettings(BaseModel):
    daily_summary_email: bool = False
    cash_alert_email: bool = False
    cash_alert_sms: bool = False
    monthly_report_email: bool = False


class PosSettingsBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    theme_mode: Literal["dark", "midnight", "light"] = "light"
    accent_color: str = Field("#0A84FF", pattern=r"^#[0-9A-Fa-f]{6}$")
    company_name: str = "Mi Negocio"
    tax_id: Optional[str] = None
    address: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    ticket_footer: Optional[str] = None
    auto_close_ticket: bool = False
    low_stock_alert: bool = True
    require_seller_pin: bool = False
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    logo_url: Optional[str] = Field(
        default=None,
        serialization_alias="logoUrl",
        validation_alias="logoUrl",
    )
    ticket_logo_url: Optional[str] = Field(
        default=None,
        serialization_alias="ticketLogoUrl",
        validation_alias="ticketLogoUrl",
    )
    closure_email_recipients: List[EmailStr] = Field(
        default_factory=list,
        validation_alias="closureEmailRecipients",
    )
    ticket_email_cc: List[EmailStr] = Field(
        default_factory=list,
        validation_alias="ticketEmailCc",
    )
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    email_from: Optional[EmailStr] = None

    @field_validator(
        "tax_id",
        "address",
        "contact_email",
        "contact_phone",
        "ticket_footer",
        "smtp_host",
        "smtp_user",
        "smtp_password",
        "email_from",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class PosSettingsUpdate(PosSettingsBase):
    pass


class PosSettingsRead(PosSettingsBase):
    id: int

    class Config:
        from_attributes = True
        populate_by_name = True


class PosUserBase(BaseModel):
    name: str
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"] = "Vendedor"
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None


class PosUserCreate(PosUserBase):
    password: Optional[str] = None


class PosUserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[Literal["Administrador", "Supervisor", "Vendedor", "Auditor"]] = None
    status: Optional[Literal["Activo", "Inactivo"]] = None
    password: Optional[str] = None
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None


class PosUserRead(PosUserBase):
    id: int
    status: Literal["Activo", "Inactivo"] = "Activo"
    created_at: datetime
    invited_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PosStationCreate(BaseModel):
    label: str
    pos_user_email: EmailStr
    pin_plain: Optional[Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]] = None


class PosStationUpdate(BaseModel):
    label: Optional[str] = None
    is_active: Optional[bool] = None
    reset_pin: bool = False
    pin_plain: Optional[Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]] = None


class PosStationRead(BaseModel):
    id: str
    label: str
    pos_user_email: EmailStr
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PosStationResponse(PosStationRead):
    pin_plain: Optional[str] = None


class PosCustomerBase(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None

    @field_validator("name", "phone", "email", "tax_id", "address", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None if value.strip() == "" else value
        return value


class PosCustomerCreate(PosCustomerBase):
    pass


class PosCustomerUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    is_active: Optional[bool] = None


class PosCustomerRead(PosCustomerBase):
    id: int
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReturnPaymentBase(BaseModel):
    method: str
    amount: float


class ReturnPaymentCreate(ReturnPaymentBase):
    pass


class ReturnPaymentRead(ReturnPaymentBase):
    id: int

    class Config:
        from_attributes = True


class ReturnItemCreate(BaseModel):
    sale_item_id: int
    quantity: float
    reason: Optional[str] = None


class ReturnItemRead(BaseModel):
    id: int
    sale_item_id: int
    product_id: int
    product_name: str
    product_sku: Optional[str] = None
    product_barcode: Optional[str] = None
    reason: Optional[str] = None
    quantity: float
    unit_price_original: float
    unit_price_net: float
    line_discount_value: float
    cart_discount_share: float
    total_refund: float

    class Config:
        from_attributes = True


class SaleReturnCreate(BaseModel):
    sale_id: Optional[int] = None
    sale_document_number: Optional[str] = None
    status: Optional[str] = "confirmed"
    notes: Optional[str] = None
    created_by: Optional[str] = None
    items: List[ReturnItemCreate]
    payments: Optional[List[ReturnPaymentCreate]] = None


class SaleReturnRead(BaseModel):
    id: int
    sale_id: int
    document_number: Optional[str] = None
    status: str
    total_refund: float
    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    items: List[ReturnItemRead]
    payments: List[ReturnPaymentRead]

    class Config:
        from_attributes = True


class SaleBase(BaseModel):
    payment_method: str = "cash"
    total: float
    paid_amount: float
    change_amount: float
    cart_discount_value: float = 0.0
    cart_discount_percent: float = 0.0
    surcharge_amount: float = 0.0
    surcharge_label: Optional[str] = None
    customer_name: Optional[str] = None
    customer_id: Optional[int] = None
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    customer_tax_id: Optional[str] = None
    customer_address: Optional[str] = None
    notes: Optional[str] = None
    pos_name: Optional[str] = None
    station_id: Optional[str] = None
    vendor_name: Optional[str] = None


class SaleCreate(SaleBase):
    items: List[SaleItemCreate]
    payments: Optional[List[SalePaymentCreate]] = None
    sale_number_preassigned: Optional[int] = None


class PaymentMethodSummary(BaseModel):
    method: str
    total: float
    tickets: int


class PaymentMethodBase(BaseModel):
    name: NonEmptyStr
    slug: SlugStr
    description: Optional[str] = None
    is_active: bool = True
    allow_change: bool = False
    order_index: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class PaymentMethodCreate(PaymentMethodBase):
    pass


class PaymentMethodUpdate(BaseModel):
    name: Optional[NonEmptyStr] = None
    slug: Optional[SlugStr] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    allow_change: Optional[bool] = None
    order_index: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class PaymentMethodRead(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    is_active: bool
    allow_change: bool
    order_index: int
    color: Optional[str] = None
    icon: Optional[str] = None
    deleted_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PaymentMethodToggleRequest(BaseModel):
    is_active: bool


class PaymentMethodReorderItem(BaseModel):
    id: int
    order_index: int


class PaymentMethodReorderRequest(BaseModel):
    items: List[PaymentMethodReorderItem]


class SalesTrendPoint(BaseModel):
    # solo usaremos la fecha, pero va como datetime
    date: datetime
    total: float
    tickets: int


class MonthlySalesPoint(BaseModel):
    month: int
    total: float
    tickets: int


class UploadProductImageResponse(BaseModel):
    url: str
    thumb_url: str


class UploadLogoResponse(BaseModel):
    url: str


class QzSignRequest(BaseModel):
    data: str


class QzSignResponse(BaseModel):
    signature: str


class ReportEmailRequest(BaseModel):
    recipients: List[EmailStr]
    subject: Optional[str] = None
    message: Optional[str] = None
    document_html: str
    preset_id: Optional[str] = None
    filters: Optional[dict] = None
    attach_pdf: bool = True


class DashboardSummary(BaseModel):
    today_sales_total: float
    today_tickets: int
    today_avg_ticket: float

    month_sales_total: float
    month_tickets: int
    month_avg_ticket: float

    payment_methods: List[PaymentMethodSummary]
    last_7_days: List[SalesTrendPoint]


class SaleRead(SaleBase):
    id: int
    # número de ticket POS
    sale_number: Optional[int] = None
    # número de documento tipo V-000001
    document_number: Optional[str] = None
    created_at: datetime

    refunded_total: float
    refund_count: int
    refunded_balance: float
    closure_id: Optional[int] = None

    items: List[SaleItemRead]
    # lista de pagos asociados
    payments: List[SalePaymentRead] = []
    returns: List[SaleReturnRead] = []
    refunded_payments: List[ReturnPaymentRead] = []
    is_separated: bool = False
    initial_payment_method: Optional[str] = None
    initial_payment_amount: Optional[float] = None
    balance: Optional[float] = None

    class Config:
        from_attributes = True


class NextSaleNumberResponse(BaseModel):
    next_sale_number: int


class AuthLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthLoginResponse(BaseModel):
    token: str
    user: PosUserRead


class AuthForgotPasswordRequest(BaseModel):
    email: EmailStr


class AuthResetPasswordRequest(BaseModel):
    token: str
    password: Annotated[str, Field(min_length=8)]


class AuthValidateResetTokenRequest(BaseModel):
    token: str


class AuthValidateResetTokenResponse(BaseModel):
    valid: bool
    expires_at: Optional[datetime] = None


class AuthPosLoginRequest(BaseModel):
    station_id: str
    pin: str


class RolePermissionAction(BaseModel):
    id: str
    label: str
    description: Optional[str] = None
    roles: Dict[str, bool]


class RolePermissionModule(BaseModel):
    id: str
    label: str
    description: Optional[str] = None
    roles: Dict[str, bool]
    actions: List[RolePermissionAction] = Field(default_factory=list)


class RolePermissionMatrix(BaseModel):
    modules: List[RolePermissionModule]


class EmailSendRequest(BaseModel):
    recipients: List[EmailStr] = Field(default_factory=list)
    subject: Optional[str] = None
    message: Optional[str] = None
    attach_pdf: bool = False
    document_type: Literal["ticket", "invoice"] = "ticket"


class EmailSendResponse(BaseModel):
    status: str = "sent"
    document_type: Literal["ticket", "invoice"] = "ticket"


class SeparatedOrderPaymentBase(BaseModel):
    method: str
    amount: float
    reference: Optional[str] = None
    note: Optional[str] = None
    station_id: Optional[str] = None


class SeparatedOrderPaymentCreate(SeparatedOrderPaymentBase):
    pass


class SeparatedOrderPaymentRead(SeparatedOrderPaymentBase):
    id: int
    paid_at: datetime
    closure_id: Optional[int] = None

    class Config:
        from_attributes = True


class SeparatedOrderCreate(SaleCreate):
    due_date: Optional[datetime] = None


class SeparatedOrderRead(BaseModel):
    id: int
    sale_id: int
    sale_number: Optional[int] = None
    sale_document_number: str
    barcode: Optional[str] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    total_amount: float
    initial_payment: float
    balance: float
    due_date: Optional[datetime] = None
    status: str
    notes: Optional[str] = None
    surcharge_amount: float = 0.0
    surcharge_label: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    payments: List[SeparatedOrderPaymentRead] = []

    class Config:
        from_attributes = True


class SeparatedOrderStatusUpdate(BaseModel):
    notes: Optional[str] = None


class PosClosureBase(BaseModel):
    pos_name: Optional[str] = None
    pos_identifier: Optional[str] = None
    station_id: Optional[str] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    total_amount: float = 0.0
    total_cash: float = 0.0
    total_card: float = 0.0
    total_qr: float = 0.0
    total_nequi: float = 0.0
    total_daviplata: float = 0.0
    total_credit: float = 0.0
    total_refunds: float = 0.0
    net_amount: float = 0.0
    counted_cash: float = 0.0
    difference: float = 0.0
    notes: Optional[str] = None
    total_surcharge: float = 0.0


class PosClosureCreate(PosClosureBase):
    closure_date: Optional[date] = None


class PosClosureRead(PosClosureBase):
    id: int
    consecutive: Optional[str] = None
    closed_by_user_id: int
    closed_by_user_name: str
    sales_count: int

    class Config:
        from_attributes = True


class PosClosureList(PosClosureRead):
    pass
