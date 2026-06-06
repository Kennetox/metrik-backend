from datetime import datetime

from sqlalchemy import text

import models
from tests.conftest import TestingSessionLocal


def test_product_updated_at_changes_on_raw_sql_update():
    db = TestingSessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").first()
        if tenant is None:
            tenant = models.Tenant(
                slug="kensar",
                name="Kensar",
                is_active=True,
                lifecycle_stage="active",
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

        product = models.Product(
            tenant_id=tenant.id,
            sku="TRG-001",
            name="Producto trigger",
            price=100.0,
            cost=50.0,
            barcode="TRG-001",
            label_format="Kensar1",
            unit="UND",
            active=True,
            service=False,
            includes_tax=False,
            is_investment=False,
            updated_at=datetime(2020, 1, 1, 0, 0, 0),
        )
        db.add(product)
        db.commit()
        db.refresh(product)

        before = product.updated_at
        db.execute(
            text("UPDATE products SET price = :price WHERE id = :product_id"),
            {"price": 125.0, "product_id": product.id},
        )
        db.commit()
        db.refresh(product)

        assert product.price == 125.0
        assert product.updated_at != before
    finally:
        db.close()
