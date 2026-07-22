from datetime import datetime, timedelta

import crud
import models
import schemas
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
        original_content_updated_at = now - timedelta(days=20)
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
                content_updated_at=original_content_updated_at,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

        items = crud.list_public_web_home_sliders(db, tenant_id=tenant.id)

        assert len(items) == 1
        assert items[0].slot == 2
        assert items[0].image_url == "https://example.com/banner.jpg"

        metadata_update = crud.update_comercio_web_home_slider(
            db,
            tenant_id=tenant.id,
            slot=2,
            payload=schemas.ComercioWebHomeSliderUpdate(
                image_url="https://example.com/banner.jpg",
                mobile_image_url="https://example.com/banner-mobile.jpg",
                cta_label="Comprar ahora",
            ),
        )
        assert metadata_update.content_updated_at == original_content_updated_at

        replacement = crud.update_comercio_web_home_slider(
            db,
            tenant_id=tenant.id,
            slot=2,
            payload=schemas.ComercioWebHomeSliderUpdate(
                image_url="https://example.com/banner-renovado.jpg",
            ),
        )
        assert replacement.content_updated_at is not None
        assert replacement.content_updated_at > original_content_updated_at
    finally:
        db.close()


def test_public_home_videos_does_not_seed_or_write_rows():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="public-home-videos-test",
            name="Public Home Videos Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        items = crud.list_public_web_home_videos(db, tenant_id=tenant.id)

        assert items == []
        assert (
            db.query(models.WebCatalogHomeVideo)
            .filter(models.WebCatalogHomeVideo.tenant_id == tenant.id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_home_videos_are_seeded_for_admin_and_only_enabled_rows_are_public():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug="configured-home-videos-test",
            name="Configured Home Videos Test",
            is_active=True,
            lifecycle_stage="active",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        admin_items = crud.list_comercio_web_home_videos(db, tenant_id=tenant.id)
        assert [item.slot for item in admin_items] == [1, 2, 3, 4, 5]

        saved = crud.update_comercio_web_home_video(
            db,
            tenant_id=tenant.id,
            slot=2,
            payload=schemas.ComercioWebHomeVideoUpdate(
                video_url="/uploads/product-videos/2/reel.mp4",
                enabled=True,
            ),
        )
        assert saved.enabled is True
        assert saved.content_updated_at is not None

        original_content_updated_at = saved.content_updated_at
        toggled = crud.update_comercio_web_home_video(
            db,
            tenant_id=tenant.id,
            slot=2,
            payload=schemas.ComercioWebHomeVideoUpdate(enabled=False),
        )
        assert toggled.content_updated_at == original_content_updated_at
        saved = crud.update_comercio_web_home_video(
            db,
            tenant_id=tenant.id,
            slot=2,
            payload=schemas.ComercioWebHomeVideoUpdate(enabled=True),
        )
        assert saved.content_updated_at == original_content_updated_at

        public_items = crud.list_public_web_home_videos(db, tenant_id=tenant.id)
        assert len(public_items) == 1
        assert public_items[0].slot == 2
        assert public_items[0].video_url.endswith("reel.mp4")
        assert public_items[0].is_new is True
    finally:
        db.close()
