from datetime import datetime

import crud
import models
from tests.conftest import TestingSessionLocal


def _seed_best_seller_product(db, tenant_id: int, index: int, now: datetime) -> models.Product:
    return models.Product(
        tenant_id=tenant_id,
        sku=f"ROT-{index:03d}",
        name=f"Producto rotacion {index:03d}",
        price=1000.0,
        cost=600.0,
        barcode=f"ROT-{index:03d}",
        label_format="Kensar1",
        unit="UND",
        active=True,
        service=False,
        includes_tax=False,
        is_investment=False,
        web_published=True,
        web_published_at=now,
        web_featured=False,
        web_short_description="Producto publicado para rotacion",
        web_category_key=f"categoria-{index % 4}",
        web_sort_order=index,
        brand=f"Marca {index % 4}",
        updated_at=now,
    )


def test_web_catalog_best_sellers_rotates_between_time_buckets():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="best-sellers-rotation-test",
            name="Best Sellers Rotation Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        now = datetime.utcnow()
        products = [_seed_best_seller_product(db, tenant.id, index, now) for index in range(16)]
        db.add_all(products)
        db.commit()
        for product in products:
            db.refresh(product)

        for product in products:
            db.add(
                models.InventoryMovement(
                    tenant_id=tenant.id,
                    product_id=product.id,
                    qty_delta=3,
                    reason="stock_initial",
                    created_at=now,
                )
            )
            db.add(
                models.Sale(
                    tenant_id=tenant.id,
                    created_at=now,
                    total=1000.0,
                    paid_amount=1000.0,
                    main_payment_method="cash",
                    payment_method="cash",
                    status="active",
                )
            )
        db.commit()

        sales = db.query(models.Sale).order_by(models.Sale.id.asc()).all()
        for sale, product in zip(sales, products, strict=True):
            db.add(
                models.SaleItem(
                    tenant_id=tenant.id,
                    sale_id=sale.id,
                    product_id=product.id,
                    product_sku=product.sku,
                    product_name=product.name,
                    quantity=1,
                    unit_price=1000.0,
                    unit_price_original=1000.0,
                    total=1000.0,
                )
            )
        db.commit()

        original_bucket = crud._web_best_sellers_rotation_bucket
        try:
            crud._WEB_BEST_SELLERS_CACHE.clear()
            crud._web_best_sellers_rotation_bucket = lambda _now: 1
            first_items, _ = crud.get_web_catalog_best_sellers(db, tenant_id=tenant.id, limit=10, days=90)

            crud._WEB_BEST_SELLERS_CACHE.clear()
            crud._web_best_sellers_rotation_bucket = lambda _now: 2
            second_items, _ = crud.get_web_catalog_best_sellers(db, tenant_id=tenant.id, limit=10, days=90)
        finally:
            crud._web_best_sellers_rotation_bucket = original_bucket
            crud._WEB_BEST_SELLERS_CACHE.clear()

        first_ids = [item.id for item in first_items]
        second_ids = [item.id for item in second_items]

        assert len(first_ids) == 10
        assert len(second_ids) == 10
        assert first_ids != second_ids
        assert set(first_ids) != set(second_ids)
    finally:
        db.close()
