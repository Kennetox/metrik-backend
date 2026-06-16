from datetime import datetime

import crud
import models
from tests.conftest import TestingSessionLocal


def test_public_home_sections_mode_does_not_create_pos_settings_row():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="public-home-sections-test",
            name="Public Home Sections Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        mode = crud.get_public_web_home_sections_mode(db, tenant_id=tenant.id)

        assert mode == "categories"
        assert (
            db.query(models.PosSettings)
            .filter(models.PosSettings.tenant_id == tenant.id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_public_home_sliders_does_not_seed_or_write_rows():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="public-home-sliders-test",
            name="Public Home Sliders Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        items = crud.list_public_web_home_sliders(db, tenant_id=tenant.id)

        assert items == []
        assert (
            db.query(models.WebCatalogHomeSlider)
            .filter(models.WebCatalogHomeSlider.tenant_id == tenant.id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_public_home_sliders_returns_enabled_configured_rows():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="public-home-sliders-configured-test",
            name="Public Home Sliders Configured Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        now = datetime.utcnow()
        db.add(
            models.WebCatalogHomeSlider(
                tenant_id=tenant.id,
                slot=2,
                enabled=True,
                image_url="https://example.com/banner.jpg",
                mobile_image_url=None,
                alt_text="Banner",
                cta_label="Ver más",
                cta_x_percent=40,
                cta_y_percent=70,
                link_type="catalogo",
                link_value=None,
                sort_order=2,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

        items = crud.list_public_web_home_sliders(db, tenant_id=tenant.id)

        assert len(items) == 1
        assert items[0].slot == 2
        assert items[0].image_url == "https://example.com/banner.jpg"
    finally:
        db.close()
