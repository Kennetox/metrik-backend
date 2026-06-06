from datetime import datetime

import crud
import models
from tests.conftest import TestingSessionLocal


def test_web_catalog_categories_hide_empty_roots_and_keep_parents_with_child_products():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="catalog-categories-test",
            name="Catalog Categories Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        now = datetime.utcnow()
        db.add_all(
            [
                models.WebCatalogCategory(
                    tenant_id=tenant.id,
                    key="percusion",
                    parent_key=None,
                    name="Percusion",
                    sort_order=1,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                ),
                models.WebCatalogCategory(
                    tenant_id=tenant.id,
                    key="campanas",
                    parent_key="percusion",
                    name="Campanas",
                    sort_order=2,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                ),
                models.WebCatalogCategory(
                    tenant_id=tenant.id,
                    key="vacia",
                    parent_key=None,
                    name="Vacia",
                    sort_order=3,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db.add(
            models.Product(
                tenant_id=tenant.id,
                sku="CAT-001",
                name="Campana activa",
                price=100.0,
                cost=50.0,
                barcode="CAT-001",
                label_format="Kensar1",
                unit="UND",
                active=True,
                service=False,
                includes_tax=False,
                web_published=True,
                web_category_key="campanas",
                updated_at=now,
            )
        )
        db.commit()

        categories = crud.get_web_catalog_categories(db, tenant_id=tenant.id)
        by_path = {item.path: item for item in categories}

        assert "percusion" in by_path
        assert by_path["percusion"].product_count == 1
        assert "vacia" not in by_path
    finally:
        db.close()
