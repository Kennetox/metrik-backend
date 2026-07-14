from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import models
from routers.kora import _build_restock_forecast_response
from tests.conftest import TestingSessionLocal


BOGOTA = ZoneInfo("America/Bogota")


def _ensure_tenant(db, slug: str) -> models.Tenant:
    tenant = db.query(models.Tenant).filter(models.Tenant.slug == slug).first()
    if tenant is None:
        tenant = models.Tenant(
            slug=slug,
            name=slug.replace("-", " ").title(),
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
    return tenant


def _create_product(
    db,
    *,
    tenant_id: int,
    sku: str,
    name: str,
    stock_min: int = 0,
    preferred_qty: int = 0,
    reorder_point: int = 0,
    low_stock_alert: bool = False,
) -> models.Product:
    product = models.Product(
        tenant_id=tenant_id,
        sku=sku,
        name=name,
        price=200.0,
        cost=100.0,
        barcode=None,
        label_format="Kensar1",
        unit="UND",
        stock_min=stock_min,
        active=True,
        service=False,
        includes_tax=False,
        is_investment=False,
        preferred_qty=preferred_qty,
        reorder_point=reorder_point,
        low_stock_alert=low_stock_alert,
        allow_price_change=False,
        group_name="General",
        brand="Marca",
        supplier="Proveedor",
        updated_at=datetime.now(tz=BOGOTA),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _add_inventory_stock(db, *, tenant_id: int, product_id: int, qty: float) -> None:
    db.add(
        models.InventoryMovement(
            tenant_id=tenant_id,
            product_id=product_id,
            qty_delta=qty,
            reason="purchase",
            reference_type="purchase",
            created_at=datetime.now(tz=BOGOTA),
        )
    )
    db.commit()


def _add_sale(db, *, tenant_id: int, product: models.Product, created_at: datetime, quantity: float = 1.0) -> None:
    sale = models.Sale(
        tenant_id=tenant_id,
        created_at=created_at,
        status="active",
        sale_number=None,
        document_number=None,
        main_payment_method="cash",
        payment_method="cash",
        total=quantity * product.price,
        paid_amount=quantity * product.price,
        change_amount=0.0,
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)

    db.add(
        models.SaleItem(
            tenant_id=tenant_id,
            sale_id=sale.id,
            product_id=product.id,
            product_sku=product.sku,
            product_name=product.name,
            product_barcode=product.barcode,
            quantity=quantity,
            unit_price=product.price,
            unit_price_original=product.price,
            discount=0.0,
            line_discount_value=0.0,
            total=quantity * product.price,
        )
    )
    db.commit()


def test_restock_today_skips_healthy_stock_with_light_today_sales():
    db = TestingSessionLocal()
    try:
        tenant = _ensure_tenant(db, "kora-restock-health-check")
        product = _create_product(
            db,
            tenant_id=tenant.id,
            sku="1",
            name="Pilas AA Par",
            preferred_qty=20,
            reorder_point=20,
            low_stock_alert=True,
        )
        _add_inventory_stock(db, tenant_id=tenant.id, product_id=product.id, qty=18)

        now = datetime.now(tz=BOGOTA)
        _add_sale(db, tenant_id=tenant.id, product=product, created_at=now, quantity=1)
        for days_ago in range(8, 29):
            _add_sale(
                db,
                tenant_id=tenant.id,
                product=product,
                created_at=now - timedelta(days=days_ago),
                quantity=1,
            )

        report = _build_restock_forecast_response(
            db=db,
            tenant_id=tenant.id,
            mode="today",
            horizon_days=2,
            lookback_days=30,
        )

        item = next((row for row in report.items if row.product_id == product.id), None)
        assert item is None
        assert report.state == "calm"
    finally:
        db.close()


def test_restock_today_keeps_low_stock_with_real_today_pressure():
    db = TestingSessionLocal()
    try:
        tenant = _ensure_tenant(db, "kora-restock-pressure-check")
        product = _create_product(
            db,
            tenant_id=tenant.id,
            sku="2",
            name="Cable USB",
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
        )
        _add_inventory_stock(db, tenant_id=tenant.id, product_id=product.id, qty=1)

        now = datetime.now(tz=BOGOTA)
        for offset in range(3):
            _add_sale(
                db,
                tenant_id=tenant.id,
                product=product,
                created_at=now if offset == 0 else now - timedelta(days=offset),
                quantity=1,
            )

        report = _build_restock_forecast_response(
            db=db,
            tenant_id=tenant.id,
            mode="today",
            horizon_days=2,
            lookback_days=30,
        )

        item = next((row for row in report.items if row.product_id == product.id), None)
        assert item is not None
        assert item.urgency == "high"
        assert item.units_today >= 1
    finally:
        db.close()


def test_restock_today_does_not_overstate_positive_stock_spikes():
    db = TestingSessionLocal()
    try:
        tenant = _ensure_tenant(db, "kora-restock-positive-stock-check")
        product_a = _create_product(
            db,
            tenant_id=tenant.id,
            sku="1634",
            name="Convertidor de 3.5mm A Plug 1/4 Monofonico Plastico",
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
        )
        product_b = _create_product(
            db,
            tenant_id=tenant.id,
            sku="854",
            name="Broche para Pila 9V",
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
        )
        _add_inventory_stock(db, tenant_id=tenant.id, product_id=product_a.id, qty=2)
        _add_inventory_stock(db, tenant_id=tenant.id, product_id=product_b.id, qty=6)

        now = datetime.now(tz=BOGOTA)
        for offset in range(4):
            _add_sale(db, tenant_id=tenant.id, product=product_a, created_at=now - timedelta(days=offset), quantity=1)
        for offset in range(4, 8):
            _add_sale(db, tenant_id=tenant.id, product=product_a, created_at=now - timedelta(days=offset), quantity=1)

        for offset in range(4):
            _add_sale(db, tenant_id=tenant.id, product=product_b, created_at=now - timedelta(days=offset), quantity=1)
        for offset in range(8, 11):
            _add_sale(db, tenant_id=tenant.id, product=product_b, created_at=now - timedelta(days=offset), quantity=1)

        report = _build_restock_forecast_response(
            db=db,
            tenant_id=tenant.id,
            mode="today",
            horizon_days=2,
            lookback_days=30,
        )

        item_a = next((row for row in report.items if row.product_id == product_a.id), None)
        item_b = next((row for row in report.items if row.product_id == product_b.id), None)
        assert item_a is None
        assert item_b is None
        assert report.state == "calm"
    finally:
        db.close()


def test_restock_today_skips_overstocked_items_with_long_coverage():
    db = TestingSessionLocal()
    try:
        tenant = _ensure_tenant(db, "kora-restock-overstock-check")
        product = _create_product(
            db,
            tenant_id=tenant.id,
            sku="907",
            name="Cables Jumper Colores",
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
        )
        _add_inventory_stock(db, tenant_id=tenant.id, product_id=product.id, qty=355)

        now = datetime.now(tz=BOGOTA)
        for offset in range(6):
            _add_sale(db, tenant_id=tenant.id, product=product, created_at=now, quantity=1)
        for offset in range(6, 49):
            _add_sale(
                db,
                tenant_id=tenant.id,
                product=product,
                created_at=now - timedelta(days=offset % 30),
                quantity=1,
            )

        report = _build_restock_forecast_response(
            db=db,
            tenant_id=tenant.id,
            mode="today",
            horizon_days=2,
            lookback_days=30,
        )

        item = next((row for row in report.items if row.product_id == product.id), None)
        assert item is None
        assert report.state == "calm"
    finally:
        db.close()
