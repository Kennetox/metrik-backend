import json
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
    label_format: Optional[str] = None
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
    is_investment: bool = False
    investment_status: Optional[Literal["active", "paused", "archived"]] = "active"
    investment_enabled_at: Optional[datetime] = None
    investment_disabled_at: Optional[datetime] = None
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    web_name: Optional[str] = None
    web_slug: Optional[str] = None
    web_published: bool = False
    web_featured: bool = False
    web_short_description: Optional[str] = None
    web_long_description: Optional[str] = None
    web_compare_price: Optional[float] = None
    web_price_source: Literal["base", "fixed", "discount_percent"] = "base"
    web_price_value: Optional[float] = None
    web_badge_text: Optional[str] = None
    web_category_key: Optional[str] = None
    web_sort_order: int = 0
    web_visible_when_out_of_stock: bool = True
    web_price_mode: Literal["visible", "consultar"] = "visible"
    web_whatsapp_message: Optional[str] = None
    web_warranty_text: Optional[str] = None
    web_gallery_urls: List[str] = Field(default_factory=list)
    web_video_url: Optional[str] = None


class ProductCreate(ProductBase):
    cost_suggestion_meta: Optional[Dict[str, Any]] = None
    auto_generate_codes: bool = False


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    price: Optional[float] = None
    cost: Optional[float] = None
    barcode: Optional[str] = None
    label_format: Optional[str] = None
    unit: Optional[str] = None
    stock_min: Optional[int] = None
    preferred_qty: Optional[int] = None
    reorder_point: Optional[int] = None
    low_stock_alert: Optional[bool] = None
    allow_price_change: Optional[bool] = None
    active: Optional[bool] = None
    service: Optional[bool] = None
    includes_tax: Optional[bool] = None
    is_investment: Optional[bool] = None
    investment_status: Optional[Literal["active", "paused", "archived"]] = None
    investment_enabled_at: Optional[datetime] = None
    investment_disabled_at: Optional[datetime] = None
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    web_name: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    web_slug: Optional[str] = None
    web_published: Optional[bool] = None
    web_featured: Optional[bool] = None
    web_short_description: Optional[str] = None
    web_long_description: Optional[str] = None
    web_compare_price: Optional[float] = None
    web_price_source: Optional[Literal["base", "fixed", "discount_percent"]] = None
    web_price_value: Optional[float] = None
    web_badge_text: Optional[str] = None
    web_category_key: Optional[str] = None
    web_sort_order: Optional[int] = None
    web_visible_when_out_of_stock: Optional[bool] = None
    web_price_mode: Optional[Literal["visible", "consultar"]] = None
    web_whatsapp_message: Optional[str] = None
    web_warranty_text: Optional[str] = None
    web_gallery_urls: Optional[List[str]] = None
    web_video_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )
    cost_suggestion_meta: Optional[Dict[str, Any]] = None

    @field_validator("web_gallery_urls", mode="before")
    @classmethod
    def _parse_gallery_urls(cls, value: Any):
        if value is None:
            return None
        if isinstance(value, str):
            text_value = value.strip()
            if not text_value:
                return []
            try:
                parsed = json.loads(text_value)
            except Exception:
                return []
            value = parsed
        if not isinstance(value, list):
            return []
        clean: List[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip()
            if normalized and normalized not in clean:
                clean.append(normalized)
        return clean[:5]


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
    web_published_at: Optional[datetime] = None
    group_meta: Optional[ProductGroupRead] = None
    qty_on_hand: Optional[float] = None

    @field_validator("web_gallery_urls", mode="before")
    @classmethod
    def _parse_gallery_urls(cls, value: Any):
        if value is None:
            return []
        if isinstance(value, str):
            text_value = value.strip()
            if not text_value:
                return []
            try:
                parsed = json.loads(text_value)
            except Exception:
                return []
            value = parsed
        if not isinstance(value, list):
            return []
        clean: List[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip()
            if normalized and normalized not in clean:
                clean.append(normalized)
        return clean[:5]

    class Config:
        from_attributes = True


class ProductCostSuggestionRequest(BaseModel):
    mode: Literal["balanced", "conservative", "aggressive"] = "balanced"
    price: float = Field(gt=0)
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    exclude_product_id: Optional[int] = Field(default=None, ge=1)


class ProductCostSuggestionResponse(BaseModel):
    mode: Literal["balanced", "conservative", "aggressive"]
    mode_label: Optional[str] = None
    suggested_cost: float
    range_min_cost: float
    range_max_cost: float
    confidence_score: float
    confidence_label: Literal["alta", "media", "baja"]
    method: str
    method_label: Optional[str] = None
    sample_size: int
    markup_used: float
    markup_p25: float
    markup_p50: float
    markup_p75: float
    selected_markup_percent: float
    recency_half_life_days: int
    notes: Optional[str] = None


class ProductDuplicateCandidatesRequest(BaseModel):
    sku: Optional[str] = None
    barcode: Optional[str] = None
    name: str = Field(min_length=1)
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    limit: int = Field(default=8, ge=1, le=20)


class ProductDuplicateCandidate(BaseModel):
    product_id: int
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    price: float
    group_name: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    similarity_score: float = Field(ge=0, le=1)
    risk_level: Literal["alto", "medio", "bajo"]
    match_reasons: List[str] = Field(default_factory=list)


class ProductDuplicateCandidatesResponse(BaseModel):
    candidates: List[ProductDuplicateCandidate] = Field(default_factory=list)
    has_high_risk: bool = False


class ComercioWebCatalogPublicationStats(BaseModel):
    configured: int
    published: int
    featured: int
    discounted: int
    consult: int
    with_stock: int
    without_stock: int
    without_image: int


class ComercioWebCatalogPublicationPage(BaseModel):
    items: List[ProductRead] = Field(default_factory=list)
    total: int
    skip: int
    limit: int
    stats: ComercioWebCatalogPublicationStats


class ComercioWebDiscountCodeBase(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    discount_type: Literal["percent", "fixed_amount"] = "percent"
    discount_value: float = Field(ge=0)
    discount_percent: float = Field(default=0, ge=0, le=100)
    is_active: bool = True
    max_uses: Optional[int] = Field(default=None, ge=1)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class ComercioWebDiscountCodeCreate(ComercioWebDiscountCodeBase):
    pass


class ComercioWebDiscountCodeUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=3, max_length=64)
    discount_type: Optional[Literal["percent", "fixed_amount"]] = None
    discount_value: Optional[float] = Field(default=None, gt=0)
    discount_percent: Optional[float] = Field(default=None, ge=0, le=100)
    is_active: Optional[bool] = None
    max_uses: Optional[int] = Field(default=None, ge=1)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class ComercioWebDiscountCodeRead(ComercioWebDiscountCodeBase):
    id: int
    uses_count: int = 0
    created_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ComercioWebDiscountCodePage(BaseModel):
    items: List[ComercioWebDiscountCodeRead] = Field(default_factory=list)
    total: int
    skip: int
    limit: int


class ComercioWebDiscountCodeUsageRow(BaseModel):
    order_id: int
    document_number: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    total: float = 0
    currency: str = "COP"
    order_status: str
    payment_status: str
    used_at: Optional[datetime] = None
    created_at: datetime


class ComercioWebDiscountCodeUsagePage(BaseModel):
    items: List[ComercioWebDiscountCodeUsageRow] = Field(default_factory=list)
    total: int
    skip: int
    limit: int


class ComercioWebCatalogCategoryBase(BaseModel):
    key: SlugStr
    name: NonEmptyStr
    parent_key: Optional[SlugStr] = None
    image_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )
    home_featured: bool = False
    home_featured_order: int = 0
    sort_order: int = 0
    is_active: bool = True


class ComercioWebCatalogCategoryCreate(ComercioWebCatalogCategoryBase):
    pass


class ComercioWebCatalogCategoryUpdate(BaseModel):
    key: Optional[SlugStr] = None
    name: Optional[NonEmptyStr] = None
    parent_key: Optional[SlugStr] = None
    image_url: Optional[str] = None
    tile_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )
    home_featured: Optional[bool] = None
    home_featured_order: Optional[int] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class ComercioWebCatalogCategoryRead(ComercioWebCatalogCategoryBase):
    id: int
    level: int = 1
    has_children: bool = False
    parent_name: Optional[str] = None
    product_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


ComercioWebHomeSliderLinkType = Literal[
    "sin_link",
    "catalogo",
    "categoria",
    "subcategoria",
    "personalizacion",
    "contacto",
    "url_interna",
]


class ComercioWebHomeSliderBase(BaseModel):
    slot: int = Field(ge=1, le=5)
    enabled: bool = False
    image_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    alt_text: Optional[str] = Field(default=None, max_length=180)
    cta_label: Optional[str] = Field(default=None, max_length=90)
    cta_x_percent: float = Field(default=50, ge=0, le=100)
    cta_y_percent: float = Field(default=80, ge=0, le=100)
    link_type: ComercioWebHomeSliderLinkType = "catalogo"
    link_value: Optional[str] = Field(default=None, max_length=255)
    sort_order: int = 0


class ComercioWebHomeSliderUpdate(BaseModel):
    enabled: Optional[bool] = None
    image_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    alt_text: Optional[str] = Field(default=None, max_length=180)
    cta_label: Optional[str] = Field(default=None, max_length=90)
    cta_x_percent: Optional[float] = Field(default=None, ge=0, le=100)
    cta_y_percent: Optional[float] = Field(default=None, ge=0, le=100)
    link_type: Optional[ComercioWebHomeSliderLinkType] = None
    link_value: Optional[str] = Field(default=None, max_length=255)
    sort_order: Optional[int] = None


class ComercioWebHomeSliderRead(ComercioWebHomeSliderBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ComercioWebDescriptionTemplateBase(BaseModel):
    template_key: SlugStr
    label: NonEmptyStr
    assigned_category_key: Optional[SlugStr] = None
    keywords: List[str] = Field(default_factory=list)
    paragraph1: str = ""
    paragraph2: str = ""
    paragraph3: str = ""
    closing: str = ""
    sort_order: int = 0

    @field_validator("keywords", mode="before")
    @classmethod
    def _sanitize_keywords(cls, value: Any):
        if value is None:
            return []
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",")]
        if not isinstance(value, list):
            return []
        clean: List[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(normalized)
        return clean


class ComercioWebDescriptionTemplateCreate(ComercioWebDescriptionTemplateBase):
    pass


class ComercioWebDescriptionTemplateUpdate(BaseModel):
    template_key: Optional[SlugStr] = None
    label: Optional[NonEmptyStr] = None
    assigned_category_key: Optional[SlugStr] = None
    keywords: Optional[List[str]] = None
    paragraph1: Optional[str] = None
    paragraph2: Optional[str] = None
    paragraph3: Optional[str] = None
    closing: Optional[str] = None
    sort_order: Optional[int] = None

    @field_validator("keywords", mode="before")
    @classmethod
    def _sanitize_keywords(cls, value: Any):
        if value is None:
            return None
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",")]
        if not isinstance(value, list):
            return []
        clean: List[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(normalized)
        return clean


class ComercioWebDescriptionTemplateRead(ComercioWebDescriptionTemplateBase):
    id: int
    created_by_user_id: Optional[int] = None
    updated_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

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


WebCatalogStockStatus = Literal["in_stock", "low_stock", "out_of_stock", "service", "consultar"]
WebCatalogPriceMode = Literal["visible", "consultar"]


class WebCatalogCategory(BaseModel):
    id: str
    path: str
    name: str
    parent_path: Optional[str] = None
    level: int = 1
    has_children: bool = False
    image_url: Optional[str] = None
    tile_color: Optional[str] = None
    home_featured: bool = False
    home_featured_order: int = 0
    product_count: int


class WebCatalogCategoryList(BaseModel):
    items: List[WebCatalogCategory]


class WebCatalogHomeSlider(BaseModel):
    slot: int
    image_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    alt_text: Optional[str] = None
    cta_label: Optional[str] = None
    cta_x_percent: float = 50
    cta_y_percent: float = 80
    link_type: ComercioWebHomeSliderLinkType = "catalogo"
    link_value: Optional[str] = None
    sort_order: int = 0


class WebCatalogHomeSliderList(BaseModel):
    items: List[WebCatalogHomeSlider]


class WebCatalogProductCard(BaseModel):
    id: int
    sku: Optional[str] = None
    slug: str
    name: str
    badge_text: Optional[str] = None
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    brand: Optional[str] = None
    group_name: Optional[str] = None
    category_path: Optional[str] = None
    category_name: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    gallery: List[str] = Field(default_factory=list)
    video_url: Optional[str] = None
    price_mode: WebCatalogPriceMode
    price: Optional[float] = None
    compare_price: Optional[float] = None
    stock_status: WebCatalogStockStatus
    featured: bool


class WebCatalogFilterOption(BaseModel):
    value: str
    label: str
    count: int
    level: int = 1
    parent_value: Optional[str] = None


class WebCatalogFilters(BaseModel):
    categories: List[WebCatalogFilterOption]
    brands: List[WebCatalogFilterOption]
    price_min: Optional[float] = None
    price_max: Optional[float] = None


class WebCatalogProductList(BaseModel):
    items: List[WebCatalogProductCard]
    total: int
    page: int
    page_size: int
    filters: WebCatalogFilters


class WebCatalogBestSellerList(BaseModel):
    items: List[WebCatalogProductCard]
    updated_at: datetime


class WebCatalogProductDetail(BaseModel):
    id: int
    sku: Optional[str] = None
    slug: str
    name: str
    badge_text: Optional[str] = None
    featured: bool
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    brand: Optional[str] = None
    group_name: Optional[str] = None
    category_path: Optional[str] = None
    category_name: Optional[str] = None
    image_url: Optional[str] = None
    image_thumb_url: Optional[str] = None
    gallery: List[str] = Field(default_factory=list)
    video_url: Optional[str] = None
    price_mode: WebCatalogPriceMode
    price: Optional[float] = None
    compare_price: Optional[float] = None
    stock_status: WebCatalogStockStatus
    warranty_text: Optional[str] = None
    specs: Dict[str, str]
    whatsapp_message: Optional[str] = None


class WebCatalogVersion(BaseModel):
    updated_at: Optional[datetime] = None
    products_count: int
    groups_count: int


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
    sku: Optional[str] = None
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


class InvestmentSummaryRead(BaseModel):
    total_products: int
    active_products: int
    stock_units: float
    stock_cost_value: float
    stock_sale_value: float


class InvestmentRecentSaleRow(BaseModel):
    sale_id: int
    sale_document_number: Optional[str] = None
    sold_at: datetime
    product_id: int
    product_name: str
    quantity: float
    unit_price: float
    gross_line_total: float
    line_discount_value: float
    discount_percent: float
    line_cost_total: float = 0.0
    net_total: float
    pos_name: Optional[str] = None
    seller_name: Optional[str] = None


class InvestmentRecentMovementRow(BaseModel):
    movement_id: int
    product_id: int
    product_name: str
    qty_delta: float
    reason: str
    notes: Optional[str] = None
    created_at: datetime


class InvestmentRecentActivityRead(BaseModel):
    recent_sales: List[InvestmentRecentSaleRow] = []
    recent_movements: List[InvestmentRecentMovementRow] = []


class InvestmentSaleLineRow(BaseModel):
    sale_id: int
    sale_document_number: Optional[str] = None
    sold_at: datetime
    product_id: int
    product_name: str
    quantity: float
    unit_price: float
    gross_line_total: float
    line_discount_value: float
    discount_percent: float
    line_cost_total: float = 0.0
    net_total: float
    pos_name: Optional[str] = None
    seller_name: Optional[str] = None


class InvestmentSaleLinePage(BaseModel):
    items: List[InvestmentSaleLineRow]
    total: int
    skip: int
    limit: int
    total_quantity: float = 0.0
    total_discount: float = 0.0
    total_net: float = 0.0


class InvestmentProductRow(BaseModel):
    product_id: int
    product_name: str
    sku: Optional[str] = None
    group_name: Optional[str] = None
    qty_on_hand: float
    status: Literal["ok", "low", "critical"]
    cost: float
    price: float
    investment_status: Literal["active", "paused", "archived"] = "active"
    investment_enabled_at: Optional[datetime] = None
    investment_disabled_at: Optional[datetime] = None
    last_movement_at: Optional[datetime] = None


class InvestmentProductStatusUpdateRequest(BaseModel):
    status: Literal["active", "paused", "archived"]


class InvestmentParticipantBase(BaseModel):
    user_id: Optional[int] = None
    display_name: str
    share_percent: float = 0.0
    profit_share_percent: float = 0.0
    capital_share_percent: float = 0.0
    is_active: bool = True


class InvestmentParticipantCreate(InvestmentParticipantBase):
    pass


class InvestmentParticipantRead(InvestmentParticipantBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InvestmentParticipantsReplaceRequest(BaseModel):
    items: List[InvestmentParticipantCreate]


class InvestmentCutPreviewRequest(BaseModel):
    period_start: datetime
    period_end: datetime


class InvestmentCutAllocationRead(BaseModel):
    participant_id: int
    participant_name: str
    share_percent: float
    profit_share_percent: float = 0.0
    capital_share_percent: float = 0.0
    profit_amount: float = 0.0
    capital_amount: float = 0.0
    amount_due: float


class InvestmentCutRead(BaseModel):
    id: int
    period_start: datetime
    period_end: datetime
    gross_sales: float
    collected_sales: float
    cogs: float
    profit_base: float
    notes: Optional[str] = None
    reconciled: bool = False
    reconciled_at: Optional[datetime] = None
    reconciled_by_user_id: Optional[int] = None
    created_at: datetime
    allocations: List[InvestmentCutAllocationRead] = []


class InvestmentCutCreateRequest(BaseModel):
    period_start: datetime
    period_end: datetime
    notes: Optional[str] = None


class InvestmentPayoutCreateRequest(BaseModel):
    participant_id: int
    cut_id: Optional[int] = None
    amount: float
    paid_at: Optional[datetime] = None
    method: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


class InvestmentPayoutRead(BaseModel):
    id: int
    participant_id: int
    participant_name: str
    cut_id: Optional[int] = None
    amount: float
    paid_at: datetime
    method: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime


class InvestmentLedgerRow(BaseModel):
    participant_id: int
    participant_name: str
    due_total: float
    paid_total: float
    balance: float


class InvestmentLedgerRead(BaseModel):
    rows: List[InvestmentLedgerRow]
    due_total: float
    paid_total: float
    balance_total: float


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
    sku: Optional[str] = None
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
    label_format_snapshot: Optional[str] = None
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
    label_format: Optional[str] = None

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
    label_format: Optional[str] = None
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


class WebPersonalizationBindingEntry(BaseModel):
    product_id: Optional[str] = None
    product_sku: Optional[str] = None
    product_name: Optional[str] = None
    product_slug: Optional[str] = None
    service_id: Optional[str] = None
    service_sku: Optional[str] = None
    service_name: Optional[str] = None


class WebPersonalizationBindings(BaseModel):
    campana_clasica_mediana: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )
    campana_clasica_grande: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )
    campana_cromada_mediana: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )
    campana_cromada_grande: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )
    guiro_mediano: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )
    guiro_grande: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )
    maraca_par: WebPersonalizationBindingEntry = Field(
        default_factory=WebPersonalizationBindingEntry
    )


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
    web_personalization_bindings: WebPersonalizationBindings = Field(
        default_factory=WebPersonalizationBindings
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
    show_in_schedule: bool = True
    row_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )
    active_from: Optional[date] = None
    active_until: Optional[date] = None


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
    show_in_schedule: Optional[bool] = None
    row_color: Optional[str] = Field(
        default=None,
        pattern=r"^#([0-9a-fA-F]{6})$",
        description="Hex color like #112233",
    )
    active_from: Optional[date] = None
    active_until: Optional[date] = None
    order_index: Optional[int] = None


class HREmployeeSystemUserSummary(BaseModel):
    id: int
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"]
    status: Literal["Activo", "Inactivo"]


class HREmployeeRead(HREmployeeBase):
    id: int
    order_index: int = 0
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


class HREmployeeReorderItem(BaseModel):
    id: int
    order_index: int


class HREmployeeReorderRequest(BaseModel):
    items: List[HREmployeeReorderItem]


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
    is_time_off: bool = False
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
    is_time_off: Optional[bool] = None
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
    row_color: Optional[str] = None
    birth_date: Optional[date] = None


class ScheduleDayTotal(BaseModel):
    shift_date: date
    total_hours: float


class ScheduleDayEvent(BaseModel):
    shift_date: date
    kind: Literal["holiday", "birthday", "event"]
    label: str
    employee_id: Optional[int] = None
    employee_name: Optional[str] = None


class ScheduleWeekView(BaseModel):
    week: ScheduleWeekRead
    employees: List[ScheduleEmployeeRow]
    shifts: List[ScheduleShiftRead]
    day_totals: List[ScheduleDayTotal]
    day_events: List[ScheduleDayEvent] = []
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


class WebCustomerRegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: Annotated[str, Field(min_length=8)]
    phone: Optional[str] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None


class WebCustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str


class WebCustomerRead(BaseModel):
    id: int
    pos_customer_id: int
    name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: EmailStr
    phone: Optional[str] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    email_verified: bool = False
    created_at: datetime
    updated_at: datetime


class WebCustomerAuthResponse(BaseModel):
    token: str
    customer: WebCustomerRead
    expires_at: datetime


class WebCustomerProfileUpdateRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None


class WebCartItemMutationRequest(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)


class WebCartItemUpdateRequest(BaseModel):
    quantity: float = Field(ge=0)


class WebCartCouponApplyRequest(BaseModel):
    code: str = Field(min_length=3, max_length=64)


class WebCartItemRead(BaseModel):
    id: int
    product_id: int
    product_name: str
    product_slug: str
    product_sku: Optional[str] = None
    image_url: Optional[str] = None
    brand: Optional[str] = None
    stock_status: WebCatalogStockStatus
    quantity: float
    unit_price: float
    compare_price: Optional[float] = None
    line_total: float


class WebCartRead(BaseModel):
    id: int
    status: str
    currency: str
    items: List[WebCartItemRead]
    items_count: int
    subtotal_base: float = 0
    discount_amount: float = 0
    subtotal: float
    total: float
    coupon_code: Optional[str] = None
    coupon_discount_percent: float = 0
    coupon_discount_type: Optional[Literal["percent", "fixed_amount"]] = None
    coupon_discount_value: float = 0
    updated_at: datetime


WebOrderStatus = Literal[
    "draft",
    "pending_payment",
    "paid",
    "processing",
    "ready",
    "fulfilled",
    "cancelled",
    "payment_failed",
    "refunded",
]

WebOrderPaymentStatus = Literal["pending", "approved", "failed", "cancelled", "refunded"]
WebOrderFulfillmentStatus = Literal["pending", "processing", "ready", "fulfilled", "cancelled"]


class WebOrderCreateFromCartRequest(BaseModel):
    notes: Optional[str] = None


class WebOrderItemRead(BaseModel):
    id: int
    product_id: int
    product_name: str
    product_slug: str
    product_sku: Optional[str] = None
    image_url: Optional[str] = None
    quantity: float
    unit_price: float
    line_discount_value: float
    line_total: float


class WebOrderPaymentRead(BaseModel):
    id: int
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    method: Optional[str] = None
    status: WebOrderPaymentStatus
    amount: float
    currency: str
    approved_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    created_at: datetime
    provider_status: Optional[str] = None
    status_detail: Optional[str] = None

class WebOrderStatusLogRead(BaseModel):
    id: int
    from_status: Optional[str] = None
    to_status: str
    note: Optional[str] = None
    actor_type: str
    actor_user_id: Optional[int] = None
    created_at: datetime


class WebOrderRead(BaseModel):
    id: int
    account_id: int
    pos_customer_id: Optional[int] = None
    web_order_number: Optional[int] = None
    document_number: Optional[str] = None
    status: WebOrderStatus
    payment_status: WebOrderPaymentStatus
    fulfillment_status: WebOrderFulfillmentStatus
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_tax_id: Optional[str] = None
    customer_address: Optional[str] = None
    subtotal: float
    discount_amount: float
    shipping_amount: float
    total: float
    currency: str
    notes: Optional[str] = None
    submitted_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    converted_to_sale_at: Optional[datetime] = None
    sale_id: Optional[int] = None
    sale_document_number: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    items: List[WebOrderItemRead]
    payments: List[WebOrderPaymentRead]
    status_logs: List[WebOrderStatusLogRead]


class WebOrderPaymentRecordRequest(BaseModel):
    method: str
    amount: float
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    status: WebOrderPaymentStatus = "approved"
    note: Optional[str] = None
    raw_payload: Dict[str, object] = Field(default_factory=dict)


class WebOrderCustomerPaymentSubmissionRequest(BaseModel):
    method: str
    amount: float = Field(gt=0)
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    note: Optional[str] = None


class WebOrderStatusUpdateRequest(BaseModel):
    status: WebOrderStatus
    note: Optional[str] = None


class WebOrderConvertToSaleRequest(BaseModel):
    note: Optional[str] = None


class MercadoPagoPayerIdentification(BaseModel):
    type: Optional[str] = None
    number: Optional[str] = None


class MercadoPagoPayerInput(BaseModel):
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    identification: Optional[MercadoPagoPayerIdentification] = None


WebCheckoutPaymentMethod = Literal["card", "pse", "nequi", "wompi"]


class WebCheckoutCreateRequest(BaseModel):
    order_id: int
    payment_method: WebCheckoutPaymentMethod
    payer: Optional[MercadoPagoPayerInput] = None
    payment_method_data: Dict[str, object] = Field(default_factory=dict)
    customer_email: Optional[EmailStr] = None
    customer_phone: Optional[str] = None
    customer_full_name: Optional[str] = None
    acceptance_token: Optional[str] = None
    accept_personal_auth: Optional[str] = None
    checkout_context: Optional[Dict[str, object]] = None


class WebMercadoPagoCheckoutCreateRequest(BaseModel):
    order_id: int
    payer: Optional[MercadoPagoPayerInput] = None
    checkout_context: Optional[Dict[str, object]] = None


class WebGuestCheckoutItemInput(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)


class WebGuestCouponPreviewRequest(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    items: List[WebGuestCheckoutItemInput]


class WebGuestCouponPreviewResponse(BaseModel):
    code: str
    discount_type: Literal["percent", "fixed_amount"] = "percent"
    discount_value: float = 0
    discount_percent: float
    subtotal_base: float
    discount_amount: float
    total: float


class WebGuestMercadoPagoCheckoutCreateRequest(BaseModel):
    items: List[WebGuestCheckoutItemInput]
    customer_email: EmailStr
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_tax_id: Optional[str] = None
    customer_address: Optional[str] = None
    coupon_code: Optional[str] = Field(default=None, min_length=3, max_length=64)
    notes: Optional[str] = None
    payer: Optional[MercadoPagoPayerInput] = None
    checkout_context: Optional[Dict[str, object]] = None


class WebMercadoPagoCheckoutCreateResponse(BaseModel):
    order_id: int
    provider: str
    preference_id: str
    init_point: Optional[str] = None
    sandbox_init_point: Optional[str] = None
    public_key: Optional[str] = None
    order_access_token: Optional[str] = None


class WebMercadoPagoOrderPaymentStatusResponse(BaseModel):
    order_id: int
    web_order_number: Optional[int] = None
    document_number: Optional[str] = None
    status: WebOrderStatus
    payment_status: WebOrderPaymentStatus
    subtotal: float = 0.0
    discount_amount: float = 0.0
    shipping_amount: float = 0.0
    total: float = 0.0
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    sale_id: Optional[int] = None
    sale_document_number: Optional[str] = None
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    payment_record_status: Optional[WebOrderPaymentStatus] = None
    items: List[WebOrderItemRead] = Field(default_factory=list)
    updated_at: datetime


WebWompiPaymentMethod = Literal["pse", "nequi", "wompi"]


class WebWompiCheckoutCreateRequest(BaseModel):
    order_id: int
    payment_method: WebWompiPaymentMethod
    payment_method_data: Dict[str, object] = Field(default_factory=dict)
    customer_email: Optional[EmailStr] = None
    customer_phone: Optional[str] = None
    customer_full_name: Optional[str] = None
    acceptance_token: Optional[str] = None
    accept_personal_auth: Optional[str] = None
    checkout_context: Optional[Dict[str, object]] = None


class WebWompiCheckoutCreateResponse(BaseModel):
    order_id: int
    provider: str
    payment_method: WebWompiPaymentMethod
    transaction_id: str
    status: WebOrderPaymentStatus
    reference: str
    redirect_url: Optional[str] = None
    checkout_url: Optional[str] = None
    async_payment_url: Optional[str] = None
    acceptance_token_permalink: Optional[str] = None
    personal_data_auth_permalink: Optional[str] = None
    order_access_token: Optional[str] = None


class WebWompiPseFinancialInstitutionRead(BaseModel):
    financial_institution_code: str
    financial_institution_name: str


class WebWompiOrderPaymentStatusResponse(BaseModel):
    order_id: int
    web_order_number: Optional[int] = None
    document_number: Optional[str] = None
    status: WebOrderStatus
    payment_status: WebOrderPaymentStatus
    subtotal: float = 0.0
    discount_amount: float = 0.0
    shipping_amount: float = 0.0
    total: float = 0.0
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    sale_id: Optional[int] = None
    sale_document_number: Optional[str] = None
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    payment_record_status: Optional[WebOrderPaymentStatus] = None
    payment_method: Optional[WebWompiPaymentMethod] = None
    checkout_url: Optional[str] = None
    async_payment_url: Optional[str] = None
    items: List[WebOrderItemRead] = Field(default_factory=list)
    updated_at: datetime


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
    pos_name: Optional[str] = None
    station_id: Optional[str] = None
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
    sale_document_number: Optional[str] = None
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


DocumentAdjustmentType = Literal["payment", "discount", "total", "note"]


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


class UploadProductVideoResponse(BaseModel):
    url: str
    duration_seconds: Optional[int] = None
    size_bytes: int


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


class MonthlyQuickReportSendRequest(BaseModel):
    year: Optional[int] = None
    month: Optional[int] = None
    force: bool = True


class MonthlyQuickReportSendResponse(BaseModel):
    status: str
    period_year: int
    period_month: int
    recipients: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class ReportsQuickTopRow(BaseModel):
    name: str
    units: float
    total: float


class ReportsQuickInsightsResponse(BaseModel):
    year: int
    month: int
    min_year: int
    max_year: int
    top_products: List[ReportsQuickTopRow] = Field(default_factory=list)
    top_groups: List[ReportsQuickTopRow] = Field(default_factory=list)


class ReportProductsLastSalesRequest(BaseModel):
    sale_ids: List[int] = Field(default_factory=list)
    product_ids: List[int] = Field(default_factory=list)


class ReportProductLastSaleRow(BaseModel):
    product_id: int
    last_sale_at: datetime


class ReportProductsLastSalesResponse(BaseModel):
    rows: List[ReportProductLastSaleRow] = Field(default_factory=list)


class ReportProductsByTargetRequest(BaseModel):
    date_from: date
    date_to: date
    source: str = "all"
    mode: Literal["product", "group"] = "product"
    result_mode: Literal["detailed", "grouped"] = "detailed"
    product_id: Optional[int] = None
    product_sku: Optional[str] = None
    product_name: Optional[str] = None
    group_path: Optional[str] = None
    group_name: Optional[str] = None


class ReportProductsByTargetRow(BaseModel):
    sku: str
    product: str
    group: str
    units: float
    product_cost: Optional[float] = None
    avg_unit_value: Optional[float] = None
    unit_value: float
    total_value: float
    last_sale_at: Optional[datetime] = None
    sale_at: Optional[datetime] = None
    document: Optional[str] = None
    pos_name: Optional[str] = None


class ReportProductsByTargetResponse(BaseModel):
    rows_count: int = 0
    units: float = 0.0
    total_value: float = 0.0
    documents: int = 0
    rows: List[ReportProductsByTargetRow] = Field(default_factory=list)


class ReportProductsSoldRequest(BaseModel):
    date_from: date
    date_to: date
    source: str = "metrik"
    pos_filter: str = "todos"
    method_filter: str = "todos"
    seller_filter: str = "todos"


class ReportProductsSoldRow(BaseModel):
    date: datetime
    product: str
    sku: str
    unit_price: float
    quantity: float
    line_total: float
    document: str
    sale_id: int
    pos_name: Optional[str] = None
    seller_name: Optional[str] = None
    payment_method: Optional[str] = None
    is_separated: bool = False


class ReportProductsSoldResponse(BaseModel):
    units: float = 0.0
    unique_products: int = 0
    product_value: float = 0.0
    separated_pending: float = 0.0
    collected_value: float = 0.0
    documents: int = 0
    rows_count: int = 0
    rows: List[ReportProductsSoldRow] = Field(default_factory=list)


class ReportFavoritesUpdateRequest(BaseModel):
    preset_ids: List[str] = Field(default_factory=list)
    expected_version: Optional[str] = None


class ReportFavoritesResponse(BaseModel):
    preset_ids: List[str] = Field(default_factory=list)
    version: str = ""


class LegacyImportBatchCreate(BaseModel):
    title: str
    source_system: str = "aronium"
    batch_key: Optional[str] = None
    note: Optional[str] = None


class LegacyImportBatchRead(BaseModel):
    id: int
    tenant_id: Optional[int] = None
    source_system: str
    batch_key: str
    title: str
    status: str
    note: Optional[str] = None
    uploaded_sales_path: Optional[str] = None
    uploaded_items_path: Optional[str] = None
    uploaded_payments_path: Optional[str] = None
    uploaded_refunds_path: Optional[str] = None
    processed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LegacyImportBatchListResponse(BaseModel):
    items: List[LegacyImportBatchRead] = Field(default_factory=list)
    total: int = 0


class LegacyImportProcessResponse(BaseModel):
    batch: LegacyImportBatchRead
    sales_loaded: int
    items_loaded: int
    payments_loaded: int
    warnings: List[str] = Field(default_factory=list)


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
    today_change_count: int = 0
    today_change_extra_total: float = 0.0
    today_change_refund_total: float = 0.0

    month_sales_total: float
    month_tickets: int
    month_avg_ticket: float
    month_change_count: int = 0
    month_change_extra_total: float = 0.0
    month_change_refund_total: float = 0.0

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
    source_system: str = "metrik"
    is_imported: bool = False

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
    module_user_access: Dict[str, List[int]] = {}
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


class PlatformTenantUserRead(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: Literal["Administrador", "Supervisor", "Vendedor", "Auditor"] = "Vendedor"
    status: Literal["Activo", "Inactivo"] = "Activo"
    is_active: bool = True
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
    module_user_access: Optional[Dict[str, List[int]]] = None
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
    module_access: Dict[str, bool] = {}


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
    send_both_documents: bool = False
    extra_html_attachments: List[HtmlAttachment] = Field(default_factory=list)


class EmailSendResponse(BaseModel):
    status: str = "sent"
    document_type: Literal["ticket", "invoice"] = "ticket"
    sent_both_documents: bool = False


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
    pending_total: float = 0.0
    net_amount: float = 0.0
    net_amount_without_separated_pending: float = 0.0


class PosClosureMethodBreakdown(BaseModel):
    key: str
    label: str
    gross: float = 0.0
    refunds: float = 0.0
    net: float = 0.0
    is_standard: bool = False


class PosClosureSeparatedSummary(BaseModel):
    tickets: int = 0
    payments_total: float = 0.0
    reserved_total: float = 0.0
    pending_total: float = 0.0
    day_collected_total: float = 0.0
    day_with_pending_total: float = 0.0


class PosClosureUserBreakdown(BaseModel):
    name: str
    total: float = 0.0


class PosClosureRead(PosClosureBase):
    id: int
    consecutive: Optional[str] = None
    closed_by_user_id: int
    closed_by_user_name: str
    sales_count: int
    station_breakdown: List[PosClosureStationBreakdown] = Field(default_factory=list)
    methods_breakdown: List[PosClosureMethodBreakdown] = Field(default_factory=list)
    separated_summary: Optional[PosClosureSeparatedSummary] = None
    user_breakdown: List[PosClosureUserBreakdown] = Field(default_factory=list)

    @field_validator("station_breakdown", mode="before")
    @classmethod
    def _normalize_station_breakdown(cls, value):
        if value is None:
            return []
        return value

    @field_validator("methods_breakdown", mode="before")
    @classmethod
    def _normalize_methods_breakdown(cls, value):
        if value is None:
            return []
        return value

    @field_validator("user_breakdown", mode="before")
    @classmethod
    def _normalize_user_breakdown(cls, value):
        if value is None:
            return []
        return value

    class Config:
        from_attributes = True


class PosClosureList(PosClosureRead):
    pass


class PosClosurePreviewRead(PosClosureBase):
    sales_count: int = 0
    station_breakdown: List[PosClosureStationBreakdown] = Field(default_factory=list)
    methods_breakdown: List[PosClosureMethodBreakdown] = Field(default_factory=list)
    separated_summary: Optional[PosClosureSeparatedSummary] = None
    user_breakdown: List[PosClosureUserBreakdown] = Field(default_factory=list)

    @field_validator("station_breakdown", mode="before")
    @classmethod
    def _normalize_station_breakdown(cls, value):
        if value is None:
            return []
        return value

    @field_validator("methods_breakdown", mode="before")
    @classmethod
    def _normalize_methods_breakdown(cls, value):
        if value is None:
            return []
        return value

    @field_validator("user_breakdown", mode="before")
    @classmethod
    def _normalize_user_breakdown(cls, value):
        if value is None:
            return []
        return value
