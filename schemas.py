from datetime import datetime, date
from typing import Any, Dict, Optional, List, Literal, Annotated

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


class ProductAuditLogRead(BaseModel):
    id: int
    product_id: int
    action: str
    actor_user_id: Optional[int] = None
    actor_name: Optional[str] = None
    actor_email: Optional[str] = None
    changes: Optional[Dict[str, object]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class CatalogVersion(BaseModel):
    products_updated_at: Optional[datetime] = None
    groups_updated_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    products_count: Optional[int] = None
    groups_count: Optional[int] = None


# ===================== INVENTORY =====================


InventoryReason = Literal[
    "sale",
    "purchase",
    "adjustment",
    "count",
    "loss",
    "damage",
    "transfer_in",
    "transfer_out",
]


class InventoryMovementBase(BaseModel):
    product_id: int
    qty_delta: float
    reason: InventoryReason
    notes: Optional[str] = None
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None


class InventoryMovementCreate(InventoryMovementBase):
    pass


class InventoryMovementRead(InventoryMovementBase):
    id: int
    product_name: str
    created_at: datetime
    created_by_user_id: Optional[int] = None
    sale_pos_name: Optional[str] = None
    sale_seller_name: Optional[str] = None

    class Config:
        from_attributes = True


class InventorySummary(BaseModel):
    total_qty: float
    low_stock_count: int
    critical_count: int
    anomaly_count: int
    reorder_count: int


class InventoryStatusRow(BaseModel):
    product_id: int
    product_name: str
    qty_on_hand: float
    status: Literal["ok", "low", "critical"]


class InventoryProductRow(BaseModel):
    product_id: int
    product_name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    group_name: Optional[str] = None
    qty_on_hand: float
    status: Literal["ok", "low", "critical"]
    cost: float
    price: float
    last_movement_at: Optional[datetime] = None


class InventoryProductPage(BaseModel):
    items: List[InventoryProductRow]
    total: int
    skip: int
    limit: int
    total_cost_value: float
    total_price_value: float


class InventoryProductMovement(BaseModel):
    id: int
    reason: InventoryReason
    qty_delta: float
    notes: Optional[str] = None
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    reference_label: Optional[str] = None
    created_at: datetime


class InventoryProductHistory(BaseModel):
    product_id: int
    product_name: str
    unit_cost: float = 0.0
    unit_price: float = 0.0
    qty_on_hand: float
    total_in: float
    total_out: float
    net: float
    movements: List[InventoryProductMovement]
    total_movements: int
    skip: int
    limit: int


class InventoryOverview(BaseModel):
    summary: InventorySummary
    recent_movements: List[InventoryMovementRead]
    status_rows: List[InventoryStatusRow]


class InventoryLatestEntryRead(BaseModel):
    id: str
    source: Literal["app", "manual"]
    product_id: int
    product_name: str
    qty_delta: float
    reason: Optional[InventoryReason] = None
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    lot_id: Optional[int] = None
    lot_number: Optional[str] = None
    created_at: datetime


InventoryRecountStatus = Literal["draft", "counting", "closed", "applied", "cancelled"]
InventoryRecountScope = Literal["all", "group", "free"]
InventoryRecountMode = Literal["blind", "visible"]
InventoryRecountSource = Literal["web", "app"]


class InventoryRecountCreate(BaseModel):
    source: InventoryRecountSource = "web"
    stock_device_id: Optional[str] = None
    title: Optional[str] = None
    scope_type: InventoryRecountScope = "all"
    scope_value: Optional[str] = None
    count_mode: InventoryRecountMode = "blind"
    notes: Optional[str] = None


class InventoryRecountLineUpsert(BaseModel):
    product_id: int
    counted_qty: float
    notes: Optional[str] = None


class InventoryRecountLineRead(BaseModel):
    id: int
    product_id: int
    product_name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    group_name: Optional[str] = None
    system_qty: float
    counted_qty: Optional[float] = None
    diff_qty: Optional[float] = None
    notes: Optional[str] = None
    counted_by_user_id: Optional[int] = None
    counted_at: Optional[datetime] = None


class InventoryRecountSummary(BaseModel):
    total_lines: int
    counted_lines: int
    pending_lines: int
    difference_lines: int
    total_system_qty: float
    total_counted_qty: float
    total_diff_qty: float


class InventoryRecountRead(BaseModel):
    id: int
    code: str
    status: InventoryRecountStatus
    source: InventoryRecountSource
    stock_device_id: Optional[str] = None
    stock_device_name: Optional[str] = None
    scope_type: InventoryRecountScope
    scope_value: Optional[str] = None
    count_mode: InventoryRecountMode
    title: Optional[str] = None
    notes: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_user_name: Optional[str] = None
    closed_by_user_id: Optional[int] = None
    closed_by_user_name: Optional[str] = None
    applied_by_user_id: Optional[int] = None
    applied_by_user_name: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    summary: InventoryRecountSummary


class InventoryRecountPage(BaseModel):
    items: List[InventoryRecountRead]
    total: int
    skip: int
    limit: int


class InventoryRecountDetail(BaseModel):
    recount: InventoryRecountRead
    lines: List[InventoryRecountLineRead]


# ===================== RECEIVING =====================


ReceivingLotStatus = Literal["open", "closed", "cancelled"]
PurchaseType = Literal["invoice", "cash"]


class ReceivingLotBase(BaseModel):
    purchase_type: PurchaseType
    origin_name: NonEmptyStr
    stock_device_id: Optional[str] = None
    source_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    invoice_reference: Optional[str] = None
    notes: Optional[str] = None


class ReceivingLotCreate(ReceivingLotBase):
    pass


class ReceivingLotUpdate(BaseModel):
    purchase_type: PurchaseType
    source_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    invoice_reference: Optional[str] = None
    notes: Optional[str] = None


class ReceivingLotRead(ReceivingLotBase):
    id: int
    lot_number: str
    status: ReceivingLotStatus
    stock_device_name: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_user_name: Optional[str] = None
    closed_by_user_id: Optional[int] = None
    closed_by_user_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    support_file_name: Optional[str] = None
    support_file_url: Optional[str] = None
    support_file_size: Optional[int] = None

    class Config:
        from_attributes = True


class ReceivingLotItemRead(BaseModel):
    id: int
    lot_id: int
    product_id: int
    product_name_snapshot: str
    sku_snapshot: Optional[str] = None
    barcode_snapshot: Optional[str] = None
    qty_received: float
    unit_cost_snapshot: float
    unit_price_snapshot: float
    labels_printed_qty: int = 0
    is_new_product: bool
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReceivingProductLookup(BaseModel):
    id: int
    sku: Optional[str] = None
    barcode: Optional[str] = None
    name: str
    price: float
    cost: float

    class Config:
        from_attributes = True


class ReceivingLotItemCreate(BaseModel):
    product_id: int
    qty_received: float = Field(gt=0)
    unit_cost: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = None


class ReceivingLotItemUpdate(BaseModel):
    qty_received: float = Field(gt=0)
    unit_cost: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = None


class ReceivingProductCodePreview(BaseModel):
    sku: str
    barcode: str


class ReceivingProductQuickCreate(BaseModel):
    name: NonEmptyStr
    price: float = Field(gt=0)
    cost: Optional[float] = Field(default=None, ge=0)
    group_name: NonEmptyStr
    brand: Optional[str] = None
    supplier: Optional[str] = None


class ReceivingProductGroupOption(BaseModel):
    path: str
    display_name: str
    parent_path: Optional[str] = None


class ReceivingLabelsSummary(BaseModel):
    pending: int = 0
    printed: int = 0
    error: int = 0


class ApiWarning(BaseModel):
    code: str
    message: str


class ReceivingLotDetail(BaseModel):
    lot: ReceivingLotRead
    items: List[ReceivingLotItemRead]
    labels_summary: ReceivingLabelsSummary
    warnings: List[ApiWarning]


class ReceivingLotPage(BaseModel):
    items: List[ReceivingLotRead]
    total: int
    skip: int
    limit: int


class ReceivingDocumentRead(BaseModel):
    id: int
    lot_number: str
    status: ReceivingLotStatus
    purchase_type: PurchaseType
    origin_name: str
    stock_device_id: Optional[str] = None
    stock_device_name: Optional[str] = None
    lines_count: int
    units_total: float
    created_at: datetime
    closed_at: Optional[datetime] = None
    closed_by_user_name: Optional[str] = None
    supplier_name: Optional[str] = None
    invoice_reference: Optional[str] = None
    notes: Optional[str] = None
    support_file_name: Optional[str] = None
    support_file_url: Optional[str] = None
    support_file_size: Optional[int] = None


class ReceivingDocumentPage(BaseModel):
    items: List[ReceivingDocumentRead]
    total: int
    skip: int
    limit: int


class ReceivingCreatedProductRead(BaseModel):
    audit_id: int
    product_id: int
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    price: float
    cost: float
    group_name: Optional[str] = None
    created_at: datetime
    created_by_user_name: Optional[str] = None


class ReceivingCreatedProductPage(BaseModel):
    items: List[ReceivingCreatedProductRead]
    total: int
    skip: int
    limit: int


ManualMovementKind = Literal["salida_manual", "venta_manual", "ajuste", "perdida_dano"]
ManualMovementStatus = Literal["open", "closed", "cancelled"]


class ManualMovementDocumentBase(BaseModel):
    kind: ManualMovementKind
    origin_name: str = "Metrik web"
    header: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


class ManualMovementDocumentCreate(ManualMovementDocumentBase):
    pass


class ManualMovementDocumentHeaderUpdate(BaseModel):
    header: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


class ManualMovementDocumentLineInput(BaseModel):
    product_id: int
    qty: float = Field(gt=0)
    unit_cost: Optional[float] = Field(default=None, ge=0)
    unit_price: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = None


class ManualMovementDocumentLinesUpdate(BaseModel):
    lines: List[ManualMovementDocumentLineInput] = Field(default_factory=list)


class ManualMovementDocumentClose(BaseModel):
    external_reference_type: Optional[str] = None
    external_reference_id: Optional[int] = None


class ManualMovementDocumentLineRead(BaseModel):
    id: int
    document_id: int
    product_id: int
    product_name_snapshot: str
    sku_snapshot: Optional[str] = None
    barcode_snapshot: Optional[str] = None
    qty: float
    unit_cost_snapshot: Optional[float] = None
    unit_price_snapshot: Optional[float] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ManualMovementDocumentRead(BaseModel):
    id: int
    document_number: str
    kind: ManualMovementKind
    status: ManualMovementStatus
    origin_name: str
    header: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    external_reference_type: Optional[str] = None
    external_reference_id: Optional[int] = None
    created_by_user_id: Optional[int] = None
    created_by_user_name: Optional[str] = None
    closed_by_user_id: Optional[int] = None
    closed_by_user_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    lines_count: int = 0
    units_total: float = 0.0

    class Config:
        from_attributes = True


class ManualMovementDocumentDetail(BaseModel):
    document: ManualMovementDocumentRead
    lines: List[ManualMovementDocumentLineRead]


class ManualMovementDocumentPage(BaseModel):
    items: List[ManualMovementDocumentRead]
    total: int
    skip: int
    limit: int


class LabelExportItem(BaseModel):
    product_id: int
    sku: Optional[str] = None
    barcode: Optional[str] = None
    name: str
    price: float
    quantity: int = Field(ge=1)


class LabelExportRequest(BaseModel):
    items: List[LabelExportItem]


class LabelCloudPrintPayload(BaseModel):
    CODIGO: str
    BARRAS: str
    NOMBRE: str
    PRECIO: str
    format: str
    copies: int = Field(ge=1)


class LabelCloudPrintRequest(BaseModel):
    payload: LabelCloudPrintPayload
    fire_and_forget: bool = False


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
        validation_alias="logoUrl",
    )
    ticket_logo_url: Optional[str] = Field(
        default=None,
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
    web_pos_send_closure_email: bool = True
    station_closure_email_overrides: Dict[str, bool] = Field(
        default_factory=dict
    )

    @field_validator(
        "tax_id",
        "address",
        "contact_email",
        "contact_phone",
        "ticket_footer",
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
    avatar_url: Optional[str] = None
    birth_date: Optional[date] = None
    location: Optional[str] = None
    bio: Optional[str] = None


class PosUserCreate(PosUserBase):
    password: Optional[str] = None
    employee_id: Optional[int] = None
    create_hr_profile: bool = False
    pin_plain: Optional[Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]] = None


class PosUserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[Literal["Administrador", "Supervisor", "Vendedor", "Auditor"]] = None
    status: Optional[Literal["Activo", "Inactivo"]] = None
    password: Optional[str] = None
    pin_plain: Optional[Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]] = None
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None
    avatar_url: Optional[str] = None
    birth_date: Optional[date] = None
    location: Optional[str] = None
    bio: Optional[str] = None
    employee_id: Optional[int] = None


class PosUserRead(PosUserBase):
    id: int
    employee_id: Optional[int] = None
    status: Literal["Activo", "Inactivo"] = "Activo"
    created_at: datetime
    invited_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PosUserProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None
    avatar_url: Optional[str] = None
    birth_date: Optional[date] = None
    location: Optional[str] = None
    bio: Optional[str] = None


class PosUserProfileRead(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"]
    status: Literal["Activo", "Inactivo"]
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None
    avatar_url: Optional[str] = None
    birth_date: Optional[date] = None
    location: Optional[str] = None
    bio: Optional[str] = None

    class Config:
        from_attributes = True


class PosUserDocumentRead(BaseModel):
    id: int
    user_id: int
    file_name: str
    file_url: str
    file_size: int
    note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class HREmployeeBase(BaseModel):
    name: str
    email: Optional[EmailStr] = None
    status: Literal["Activo", "Inactivo"] = "Activo"
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None
    avatar_url: Optional[str] = None
    birth_date: Optional[date] = None
    location: Optional[str] = None
    bio: Optional[str] = None
    payroll_frequency: Optional[Literal["diario", "semanal", "mensual"]] = None
    payroll_amount: Optional[float] = None
    payroll_currency: Optional[str] = None
    payroll_payment_method: Optional[str] = None
    payroll_day_of_week: Optional[str] = None
    payroll_day_of_month: Optional[int] = Field(default=None, ge=1, le=31)
    payroll_last_paid_at: Optional[date] = None
    payroll_next_due_at: Optional[date] = None
    payroll_reference: Optional[str] = None
    payroll_notes: Optional[str] = None


class HREmployeeCreate(HREmployeeBase):
    pass


class HREmployeeUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    status: Optional[Literal["Activo", "Inactivo"]] = None
    phone: Optional[str] = None
    position: Optional[str] = None
    notes: Optional[str] = None
    avatar_url: Optional[str] = None
    birth_date: Optional[date] = None
    location: Optional[str] = None
    bio: Optional[str] = None
    payroll_frequency: Optional[Literal["diario", "semanal", "mensual"]] = None
    payroll_amount: Optional[float] = None
    payroll_currency: Optional[str] = None
    payroll_payment_method: Optional[str] = None
    payroll_day_of_week: Optional[str] = None
    payroll_day_of_month: Optional[int] = Field(default=None, ge=1, le=31)
    payroll_last_paid_at: Optional[date] = None
    payroll_next_due_at: Optional[date] = None
    payroll_reference: Optional[str] = None
    payroll_notes: Optional[str] = None


class HREmployeeSystemUserSummary(BaseModel):
    id: int
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"]
    status: Literal["Activo", "Inactivo"]


class HREmployeeRead(HREmployeeBase):
    id: int
    created_at: datetime
    updated_at: datetime
    system_user: Optional[HREmployeeSystemUserSummary] = None

    class Config:
        from_attributes = True


class HREmployeeDocumentRead(BaseModel):
    id: int
    employee_id: int
    file_name: str
    file_url: str
    file_size: int
    note: Optional[str] = None
    created_at: datetime
    source: Literal["hr", "profile"] = "hr"
    can_delete: bool = True

    class Config:
        from_attributes = True


class HREmployeeCreateSystemUserRequest(BaseModel):
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"] = "Vendedor"
    password: Optional[str] = None
    pin_plain: Optional[Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]] = None


class HREmployeeLinkSystemUserRequest(BaseModel):
    user_id: int


class HRSystemUserOption(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"]
    status: Literal["Activo", "Inactivo"]
    employee_id: Optional[int] = None


ScheduleStatus = Literal["draft", "published"]
ScheduleTimeStr = Annotated[
    str,
    Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
]


class ScheduleTemplateBase(BaseModel):
    name: str
    start_time: ScheduleTimeStr
    end_time: ScheduleTimeStr
    break_minutes: int = Field(default=0, ge=0, le=240)
    color: Optional[str] = None
    position: Optional[str] = None
    is_active: bool = True
    order_index: int = 0


class ScheduleTemplateCreate(ScheduleTemplateBase):
    pass


class ScheduleTemplateUpdate(BaseModel):
    name: Optional[str] = None
    start_time: Optional[ScheduleTimeStr] = None
    end_time: Optional[ScheduleTimeStr] = None
    break_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    color: Optional[str] = None
    position: Optional[str] = None
    is_active: Optional[bool] = None
    order_index: Optional[int] = None


class ScheduleTemplateRead(ScheduleTemplateBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScheduleWeekCreate(BaseModel):
    week_start: date
    notes: Optional[str] = None


class ScheduleWeekRead(BaseModel):
    id: int
    week_start: date
    status: ScheduleStatus
    notes: Optional[str] = None
    published_at: Optional[datetime] = None
    published_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScheduleWeekPublishRequest(BaseModel):
    notes: Optional[str] = None


class ScheduleShiftBase(BaseModel):
    employee_id: int
    shift_date: date
    start_time: Optional[ScheduleTimeStr] = None
    end_time: Optional[ScheduleTimeStr] = None
    break_minutes: int = Field(default=0, ge=0, le=240)
    position: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None
    is_time_off: bool = False
    source_template_id: Optional[int] = None


class ScheduleShiftUpsertRequest(ScheduleShiftBase):
    week_id: Optional[int] = None
    week_start: Optional[date] = None


class ScheduleShiftUpdate(BaseModel):
    start_time: Optional[ScheduleTimeStr] = None
    end_time: Optional[ScheduleTimeStr] = None
    break_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    position: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None
    is_time_off: Optional[bool] = None
    source_template_id: Optional[int] = None


class ScheduleShiftRead(ScheduleShiftBase):
    id: int
    week_id: int
    created_at: datetime
    updated_at: datetime
    total_hours: float = 0.0

    class Config:
        from_attributes = True


class ScheduleEmployeeRow(BaseModel):
    id: int
    name: str
    status: Literal["Activo", "Inactivo"]
    position: Optional[str] = None
    avatar_url: Optional[str] = None


class ScheduleDayTotal(BaseModel):
    shift_date: date
    total_hours: float


class ScheduleWeekView(BaseModel):
    week: ScheduleWeekRead
    employees: List[ScheduleEmployeeRow]
    shifts: List[ScheduleShiftRead]
    day_totals: List[ScheduleDayTotal]
    week_total_hours: float


class PosStationCreate(BaseModel):
    label: str
    station_email: EmailStr
    station_password: Annotated[str, Field(min_length=6)]
    station_type: Literal["desktop", "tablet"] = "desktop"
    parent_station_id: Optional[str] = None


class PosStationUpdate(BaseModel):
    label: Optional[str] = None
    is_active: Optional[bool] = None
    station_type: Optional[Literal["desktop", "tablet"]] = None
    parent_station_id: Optional[str] = None
    reset_pin: bool = False
    pin_plain: Optional[Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]] = None
    station_email: Optional[EmailStr] = None
    station_password: Optional[Annotated[str, Field(min_length=6)]] = None


class PosStationPrinterConfigBase(BaseModel):
    printer_mode: Optional[Literal["browser", "qz-tray"]] = None
    printer_name: Optional[str] = None
    printer_width: Optional[Literal["58mm", "80mm"]] = None
    printer_auto_open_drawer: Optional[bool] = None
    printer_show_drawer_button: Optional[bool] = None


class PosStationPrinterConfigUpdate(PosStationPrinterConfigBase):
    pass


class PosStationPrinterConfigRead(PosStationPrinterConfigBase):
    class Config:
        from_attributes = True


class PosStationRead(BaseModel):
    id: str
    label: str
    station_type: Literal["desktop", "tablet"] = "desktop"
    parent_station_id: Optional[str] = None
    parent_station_label: Optional[str] = None
    station_email: Optional[EmailStr] = None
    is_active: bool
    last_login_at: Optional[datetime] = None
    bound_device_id: Optional[str] = None
    bound_device_label: Optional[str] = None
    bound_at: Optional[datetime] = None
    bound_by_user_id: Optional[int] = None
    bound_by_user_name: Optional[str] = None
    printer_mode: Optional[Literal["browser", "qz-tray"]] = None
    printer_name: Optional[str] = None
    printer_width: Optional[Literal["58mm", "80mm"]] = None
    printer_auto_open_drawer: Optional[bool] = None
    printer_show_drawer_button: Optional[bool] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PosStationResponse(PosStationRead):
    pin_plain: Optional[str] = None


class StockDeviceCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    bound_device_id: Optional[str] = None
    bound_device_label: Optional[str] = None


class StockDeviceUpdate(BaseModel):
    name: Optional[Annotated[str, Field(min_length=1, max_length=120)]] = None
    is_active: Optional[bool] = None
    bound_device_id: Optional[str] = None
    bound_device_label: Optional[str] = None
    touch_seen: bool = False


class StockDeviceRead(BaseModel):
    id: str
    name: str
    is_active: bool
    bound_device_id: Optional[str] = None
    bound_device_label: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_user_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class StockDevicePage(BaseModel):
    items: List[StockDeviceRead]
    total: int
    skip: int
    limit: int


class PosStationNoticeCreate(BaseModel):
    message: Annotated[str, Field(min_length=1, max_length=500)]


class PosStationNoticeRead(BaseModel):
    id: int
    station_id: str
    message: str
    created_at: datetime
    created_by_user_name: Optional[str] = None

    class Config:
        from_attributes = True


class PosClosureStationScopeItem(BaseModel):
    station_id: str
    station_label: str
    station_type: Literal["desktop", "tablet"] = "desktop"
    is_primary: bool = False


class PosClosureStationScopeRead(BaseModel):
    primary_station_id: str
    station_ids: List[str] = Field(default_factory=list)
    stations: List[PosClosureStationScopeItem] = Field(default_factory=list)


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


class PosCustomerFrequentRead(PosCustomerRead):
    sales_count: int


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
    closure_id: Optional[int] = None
    document_number: Optional[str] = None
    sale_document_number: Optional[str] = None
    status: str
    voided_at: Optional[datetime] = None
    voided_by_user_id: Optional[int] = None
    void_reason: Optional[str] = None
    adjustment_reference: Optional[str] = None
    total_refund: float
    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    pos_name: Optional[str] = None
    station_id: Optional[str] = None
    items: List[ReturnItemRead]
    payments: List[ReturnPaymentRead]

    class Config:
        from_attributes = True


class SaleChangeReturnItemCreate(BaseModel):
    sale_item_id: int
    quantity: float
    reason: Optional[str] = None


class SaleChangeNewItemCreate(BaseModel):
    product_id: int
    quantity: float


class SaleChangePaymentCreate(BaseModel):
    method: str
    amount: float


class SaleChangeCreate(BaseModel):
    sale_id: Optional[int] = None
    sale_document_number: Optional[str] = None
    status: Optional[str] = "confirmed"
    notes: Optional[str] = None
    created_by: Optional[str] = None
    return_items: List[SaleChangeReturnItemCreate]
    new_items: List[SaleChangeNewItemCreate]
    payments: Optional[List[SaleChangePaymentCreate]] = None


class SaleChangeReturnItemRead(BaseModel):
    id: int
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
    total_credit: float

    class Config:
        from_attributes = True


class SaleChangeNewItemRead(BaseModel):
    id: int
    product_id: int
    product_name: str
    product_sku: Optional[str] = None
    product_barcode: Optional[str] = None
    quantity: float
    unit_price: float
    total: float

    class Config:
        from_attributes = True


class SaleChangePaymentRead(BaseModel):
    method: str
    amount: float

    class Config:
        from_attributes = True


class SaleChangeRead(BaseModel):
    id: int
    sale_id: int
    closure_id: Optional[int] = None
    document_number: Optional[str] = None
    status: str
    voided_at: Optional[datetime] = None
    voided_by_user_id: Optional[int] = None
    void_reason: Optional[str] = None
    adjustment_reference: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    pos_name: Optional[str] = None
    seller_name: Optional[str] = None
    station_id: Optional[str] = None
    total_credit: float
    total_new: float
    net_total: float
    extra_payment: float
    refund_due: float
    items_returned: List[SaleChangeReturnItemRead]
    items_new: List[SaleChangeNewItemRead]
    payments: List[SaleChangePaymentRead]

    class Config:
        from_attributes = True


class VoidRequest(BaseModel):
    reason: Optional[str] = None


class SeparatedOrderPaymentVoidRequest(BaseModel):
    reason: Optional[str] = None
    note: Optional[str] = None


DocumentAdjustmentType = Literal["payment", "discount", "note"]


class DocumentAdjustmentCreate(BaseModel):
    adjustment_type: DocumentAdjustmentType
    reason: NonEmptyStr
    total_delta: float = 0.0
    payment_delta: float = 0.0
    payload: Dict[str, object] = Field(default_factory=dict)


class DocumentAdjustmentRead(BaseModel):
    id: int
    doc_type: str
    doc_id: int
    adjustment_type: DocumentAdjustmentType
    reason: Optional[str] = None
    payload: Dict[str, object] = Field(default_factory=dict)
    total_delta: float
    payment_delta: float
    is_post_closure: bool = False
    original_closure_id: Optional[int] = None
    created_by_user_id: Optional[int] = None
    created_by_user_name: Optional[str] = None
    created_at: datetime

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
    reservation_id: Optional[int] = None


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


class UploadAvatarResponse(BaseModel):
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


class ReportPdfExportRequest(BaseModel):
    title: Optional[str] = None
    document_html: str
    preset_id: Optional[str] = None


class ReportExportCompanyInfo(BaseModel):
    name: str
    address: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class ReportExportFilterMeta(BaseModel):
    from_date: str
    to_date: str
    pos_filter: str
    method_filter: str
    seller_filter: str


class ReportExportSummaryItem(BaseModel):
    label: str
    value: str


class ReportExportTable(BaseModel):
    columns: List[str]
    rows: List[List[str]] = []
    empty_message: Optional[str] = None


class ReportExportRequest(BaseModel):
    preset_id: str
    title: str
    company: ReportExportCompanyInfo
    filters: ReportExportFilterMeta
    summary: List[ReportExportSummaryItem] = []
    table: ReportExportTable


class DashboardSummary(BaseModel):
    today_sales_total: float
    today_tickets: int
    today_avg_ticket: float

    month_sales_total: float
    month_tickets: int
    month_avg_ticket: float

    payment_methods: List[PaymentMethodSummary]
    last_7_days: List[SalesTrendPoint]
    trend_days: List[SalesTrendPoint] = []


class PaymentMethodsSummary(BaseModel):
    methods: List[PaymentMethodSummary]


class SaleRead(SaleBase):
    id: int
    # número de ticket POS
    sale_number: Optional[int] = None
    # número de documento tipo V-000001
    document_number: Optional[str] = None
    created_at: datetime
    status: str
    voided_at: Optional[datetime] = None
    voided_by_user_id: Optional[int] = None
    void_reason: Optional[str] = None
    adjustment_reference: Optional[str] = None

    refunded_total: float
    refund_count: int
    refunded_balance: float
    closure_id: Optional[int] = None

    items: List[SaleItemRead]
    # lista de pagos asociados
    payments: List[SalePaymentRead] = []
    returns: List[SaleReturnRead] = []
    changes: List[SaleChangeRead] = []
    refunded_payments: List[ReturnPaymentRead] = []
    is_separated: bool = False
    initial_payment_method: Optional[str] = None
    initial_payment_amount: Optional[float] = None
    balance: Optional[float] = None
    has_cash_payment: bool = False

    class Config:
        from_attributes = True


class SaleVoidResponse(BaseModel):
    sale: SaleRead
    return_document: Optional[SaleReturnRead] = None


class SalesHistoryPage(BaseModel):
    total: int
    skip: int
    limit: int
    items: List[SaleRead]


class DocumentExportRow(BaseModel):
    document_number: str
    doc_type: str
    detail: str
    total: float
    method: str
    customer: str
    pos: str
    vendor: str
    reference: str
    status: str
    created_at: str


class DocumentExportRequest(BaseModel):
    items: List[DocumentExportRow]


class NextSaleNumberResponse(BaseModel):
    next_sale_number: int


class SaleNumberReservationRequest(BaseModel):
    pos_name: Optional[str] = None
    station_id: Optional[str] = None
    vendor_name: Optional[str] = None
    min_sale_number: Optional[int] = None


class SaleNumberReservationResponse(BaseModel):
    reservation_id: int
    sale_number: int
    document_number: str
    status: str


class TenantRead(BaseModel):
    id: int
    slug: str
    name: str
    is_active: bool
    lifecycle_stage: Literal["demo", "active", "inactive", "suspended", "archived"] = "active"
    trial_started_at: Optional[datetime] = None
    trial_ends_at: Optional[datetime] = None
    converted_at: Optional[datetime] = None
    enabled_modules: List[str] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PlatformTenantAdminRead(BaseModel):
    id: int
    name: str
    email: EmailStr
    phone: Optional[str] = None
    status: Literal["Activo", "Inactivo"] = "Activo"
    created_at: datetime


class PlatformTenantCompanyRead(BaseModel):
    company_name: str
    tax_id: Optional[str] = None
    address: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None


class TenantModuleCatalogItem(BaseModel):
    id: str
    label: str
    description: str
    required: bool = False
    platform_visible: bool = True
    enabled_by_default: bool = True


class PlatformTenantRead(TenantRead):
    admin_user: Optional[PlatformTenantAdminRead] = None
    company_details: Optional[PlatformTenantCompanyRead] = None
    trial_days_remaining: Optional[int] = None
    module_catalog: List[TenantModuleCatalogItem] = []


class PlatformTenantCreateRequest(BaseModel):
    slug: Annotated[str, Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")]
    name: Annotated[str, Field(min_length=2, max_length=128)]
    admin_name: Annotated[str, Field(min_length=2, max_length=128)]
    admin_email: EmailStr
    admin_password: Annotated[str, Field(min_length=8)]
    admin_phone: Optional[str] = None


class PlatformTenantCreateResponse(BaseModel):
    tenant: PlatformTenantRead
    admin_user: PosUserRead
    detail: str = "Tenant creado correctamente"


class PlatformTenantUpdateRequest(BaseModel):
    name: Optional[Annotated[str, Field(min_length=2, max_length=128)]] = None
    is_active: Optional[bool] = None
    enabled_modules: Optional[List[str]] = None
    lifecycle_stage: Optional[
        Literal["demo", "active", "inactive", "suspended", "archived"]
    ] = None


class PlatformTenantRecoveryResponse(BaseModel):
    detail: str
    recipient: EmailStr
    expires_in: int


class PlatformTenantTrialUpdateRequest(BaseModel):
    extra_days: Annotated[int, Field(ge=1, le=90)]


class TenantSessionRead(BaseModel):
    id: int
    slug: str
    name: str
    lifecycle_stage: Literal["demo", "active", "inactive", "suspended", "archived"] = "active"
    trial_started_at: Optional[datetime] = None
    trial_ends_at: Optional[datetime] = None
    trial_days_remaining: Optional[int] = None
    enabled_modules: List[str] = []


class PlatformUserRead(BaseModel):
    id: int
    email: EmailStr
    name: str
    is_active: bool
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class PlatformLoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_token: Optional[str] = None
    device_label: Optional[str] = None


class PlatformLoginResponse(BaseModel):
    token: str
    user: PlatformUserRead
    expires_at: Optional[datetime] = None
    trusted_device_token: Optional[str] = None


class PlatformLogin2FARequiredResponse(BaseModel):
    requires_2fa: bool = True
    challenge_id: int
    masked_email: str
    expires_in: int
    detail: str = "Te enviamos un código de verificación al correo."


class PlatformVerify2FARequest(BaseModel):
    challenge_id: int
    code: Annotated[str, Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")]
    remember_device: bool = False
    device_token: Optional[str] = None
    device_label: Optional[str] = None


class AuthLoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_slug: Optional[str] = None


class AuthLoginResponse(BaseModel):
    token: str
    user: PosUserRead
    tenant: Optional[TenantSessionRead] = None
    expires_at: Optional[datetime] = None


class DemoStartRequest(BaseModel):
    company_name: Annotated[str, Field(min_length=2, max_length=128)]
    business_type: Optional[Annotated[str, Field(max_length=80)]] = None
    company_phone: Optional[str] = None
    company_city: Optional[str] = None
    admin_name: Annotated[str, Field(min_length=2, max_length=128)]
    admin_email: EmailStr
    admin_phone: Optional[str] = None
    password: Annotated[str, Field(min_length=8)]


class DemoStartResponse(AuthLoginResponse):
    detail: str = "Demo creada correctamente"


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
    pin: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    device_id: Optional[str] = None
    device_label: Optional[str] = None


class AuthTabletLoginRequest(BaseModel):
    station_id: str
    pin: str
    device_id: Optional[str] = None
    device_label: Optional[str] = None


class AuthPosStationLoginRequest(BaseModel):
    station_email: EmailStr
    station_password: str
    device_id: Optional[str] = None
    device_label: Optional[str] = None


class AuthPosStationLoginResponse(BaseModel):
    station_id: str
    station_label: str
    station_email: EmailStr
    tenant_name: Optional[str] = None
    parent_station_id: Optional[str] = None
    parent_station_label: Optional[str] = None


class AuthTabletEmailCheckRequest(BaseModel):
    station_id: str
    email: EmailStr


class AuthTabletEmailCheckResponse(BaseModel):
    exists: bool
    user: Optional[PosUserRead] = None


class AuthMobileStockEmailCheckRequest(BaseModel):
    email: EmailStr


class AuthMobileStockLoginRequest(BaseModel):
    email: EmailStr
    pin: str
    device_id: Optional[str] = None
    device_label: Optional[str] = None


class RolePermissionAction(BaseModel):
    id: str
    label: str
    description: Optional[str] = None
    roles: Dict[str, bool]
    editable: Optional[bool] = None


class RolePermissionModule(BaseModel):
    id: str
    label: str
    description: Optional[str] = None
    roles: Dict[str, bool]
    actions: List[RolePermissionAction] = Field(default_factory=list)
    editable: Optional[bool] = None


class RolePermissionMatrix(BaseModel):
    modules: List[RolePermissionModule]


class EmailSendRequest(BaseModel):
    class HtmlAttachment(BaseModel):
        filename: str
        title: Optional[str] = None
        document_html: str

    recipients: List[EmailStr] = Field(default_factory=list)
    subject: Optional[str] = None
    message: Optional[str] = None
    attach_pdf: bool = False
    document_type: Literal["ticket", "invoice"] = "ticket"
    extra_html_attachments: List[HtmlAttachment] = Field(default_factory=list)


class EmailSendResponse(BaseModel):
    status: str = "sent"
    document_type: Literal["ticket", "invoice"] = "ticket"


class SaleDocumentResponse(BaseModel):
    sale_id: int
    sale_number: Optional[int] = None
    document_number: Optional[str] = None
    document_type: Literal["ticket", "invoice"] = "ticket"
    filename: str
    document_html: str


class SmtpTestEmailRequest(BaseModel):
    recipients: List[EmailStr] = Field(default_factory=list)
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    email_from: Optional[EmailStr] = None
    subject: Optional[str] = None
    message: Optional[str] = None


class ContactRequestCreate(BaseModel):
    query_type: Literal[
        "soporte_tecnico",
        "consulta_comercial",
        "facturacion",
        "implementacion",
        "sugerencia",
        "otro",
    ]
    message: str = Field(min_length=10, max_length=700)
    sender_name: NonEmptyStr = Field(max_length=80)
    sender_email: Optional[EmailStr] = None
    source: Optional[str] = Field(default="web_contacto", max_length=60)


class ContactRequestResponse(BaseModel):
    status: str = "sent"


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
    status: str
    voided_at: Optional[datetime] = None
    voided_by_user_id: Optional[int] = None
    void_reason: Optional[str] = None
    adjustment_reference: Optional[str] = None

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
    change_extra_total: float = 0.0
    change_refund_total: float = 0.0
    change_count: int = 0
    notes: Optional[str] = None
    total_surcharge: float = 0.0


class PosClosureCreate(PosClosureBase):
    closure_date: Optional[date] = None


class PosClosureStationBreakdown(BaseModel):
    station_id: Optional[str] = None
    station_label: str
    station_type: Optional[str] = None
    sales_count: int = 0
    total_amount: float = 0.0
    total_refunds: float = 0.0
    total_cash: float = 0.0
    total_card: float = 0.0
    total_qr: float = 0.0
    total_nequi: float = 0.0
    total_daviplata: float = 0.0
    total_credit: float = 0.0
    change_extra_total: float = 0.0
    change_refund_total: float = 0.0
    net_amount: float = 0.0


class PosClosureRead(PosClosureBase):
    id: int
    consecutive: Optional[str] = None
    closed_by_user_id: int
    closed_by_user_name: str
    sales_count: int
    station_breakdown: List[PosClosureStationBreakdown] = Field(default_factory=list)

    @field_validator("station_breakdown", mode="before")
    @classmethod
    def _normalize_station_breakdown(cls, value):
        if value is None:
            return []
        return value

    class Config:
        from_attributes = True


class PosClosureList(PosClosureRead):
    pass
