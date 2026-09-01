from datetime import datetime, timedelta

import models
from services.operational_notifications import (
    dispatch_separated_order_notifications,
    dispatch_web_content_renewal_notifications,
)
from tests.conftest import TestingSessionLocal


def _user(tenant_id: int, *, email: str, role: str) -> models.PosUser:
    return models.PosUser(
        tenant_id=tenant_id,
        name=role,
        email=email,
        role=role,
        status="Activo",
        is_active=True,
        password_hash="not-used",
    )


def test_operational_notifications_are_targeted_summarized_and_deduplicated(client):
    reference = datetime(2026, 7, 22, 15, 0, 0)
    with TestingSessionLocal() as db:
        tenant = models.Tenant(
            slug="operational-notifications-test",
            name="Operational notifications test",
            is_active=True,
            enabled_modules=["commerce_web"],
        )
        db.add(tenant)
        db.flush()
        admin = _user(tenant.id, email="operations-admin@test.local", role="Administrador")
        seller = _user(tenant.id, email="operations-seller@test.local", role="Vendedor")
        web_manager = _user(tenant.id, email="operations-web@test.local", role="Gestor Web")
        db.add_all([admin, seller, web_manager])

        sales = [
            models.Sale(
                tenant_id=tenant.id,
                status="active",
                document_number=f"SEP-OPS-{index}",
                total=100_000,
                paid_amount=25_000,
            )
            for index in range(2)
        ]
        db.add_all(sales)
        db.flush()
        db.add_all(
            [
                models.SeparatedOrder(
                    tenant_id=tenant.id,
                    sale_id=sales[0].id,
                    customer_name="Cliente vencido",
                    customer_phone="3000000001",
                    total_amount=100_000,
                    initial_payment=25_000,
                    balance=75_000,
                    due_date=reference - timedelta(days=2),
                    status="reservado",
                    sale_document_number=sales[0].document_number,
                ),
                models.SeparatedOrder(
                    tenant_id=tenant.id,
                    sale_id=sales[1].id,
                    customer_name="Cliente próximo",
                    total_amount=100_000,
                    initial_payment=50_000,
                    balance=50_000,
                    due_date=reference + timedelta(days=2),
                    status="reservado",
                    sale_document_number=sales[1].document_number,
                ),
            ]
        )
        db.add_all(
            [
                models.WebCatalogHomeSlider(
                    tenant_id=tenant.id,
                    slot=1,
                    enabled=True,
                    image_url="/old.webp",
                    content_updated_at=reference - timedelta(days=46),
                ),
                models.WebCatalogHomeSlider(
                    tenant_id=tenant.id,
                    slot=2,
                    enabled=True,
                    image_url="/soon.webp",
                    content_updated_at=reference - timedelta(days=36),
                ),
                models.WebCatalogHomeSlider(
                    tenant_id=tenant.id,
                    slot=3,
                    enabled=True,
                    image_url="/fresh.webp",
                    content_updated_at=reference - timedelta(days=10),
                ),
                models.WebCatalogHomeVideo(
                    tenant_id=tenant.id,
                    slot=1,
                    enabled=True,
                    video_url="/old.mp4",
                    content_updated_at=reference - timedelta(days=29),
                ),
            ]
        )
        db.commit()

        separated = dispatch_separated_order_notifications(
            db, tenant_id=tenant.id, reference_time=reference
        )
        content = dispatch_web_content_renewal_notifications(
            db, tenant_id=tenant.id, reference_time=reference
        )
        repeated_separated = dispatch_separated_order_notifications(
            db, tenant_id=tenant.id, reference_time=reference
        )
        repeated_content = dispatch_web_content_renewal_notifications(
            db, tenant_id=tenant.id, reference_time=reference
        )
        next_day_separated = dispatch_separated_order_notifications(
            db,
            tenant_id=tenant.id,
            reference_time=reference + timedelta(days=1),
        )

        assert separated is not None and separated.created_count == 2
        assert set(separated.recipient_ids) == {admin.id, seller.id}
        assert content is not None and content.created_count == 2
        assert set(content.recipient_ids) == {admin.id, web_manager.id}
        assert repeated_separated is not None and repeated_separated.created_count == 0
        assert repeated_content is not None and repeated_content.created_count == 0
        assert next_day_separated is not None and next_day_separated.created_count == 2

        notifications = (
            db.query(models.UserNotification)
            .filter(models.UserNotification.tenant_id == tenant.id)
            .all()
        )
        separated_notice = next(
            item for item in notifications if item.category == "separated_follow_up"
        )
        assert separated_notice.payload["overdue_count"] == 1
        assert separated_notice.payload["due_soon_count"] == 1
        assert separated_notice.payload["total_balance"] == 125_000
        content_notice = next(
            item for item in notifications if item.category == "web_content_renewal"
        )
        assert content_notice.payload["renew_count"] == 2
        assert content_notice.payload["change_soon_count"] == 1
        assert {item["slot"] for item in content_notice.payload["content"] if item["kind"] == "slider"} == {1, 2}
        visible_separated = [
            item
            for item in notifications
            if item.category == "separated_follow_up" and item.dismissed_at is None
        ]
        assert len(visible_separated) == 2
        assert {
            item.dedupe_key for item in visible_separated
        } == {"operations:separated:2026-07-23"}

        db.query(models.UserNotification).filter(
            models.UserNotification.tenant_id == tenant.id
        ).delete(synchronize_session=False)
        db.query(models.SeparatedOrder).filter(
            models.SeparatedOrder.tenant_id == tenant.id
        ).delete(synchronize_session=False)
        db.query(models.Sale).filter(models.Sale.tenant_id == tenant.id).delete(
            synchronize_session=False
        )
        db.query(models.WebCatalogHomeSlider).filter(
            models.WebCatalogHomeSlider.tenant_id == tenant.id
        ).delete(synchronize_session=False)
        db.query(models.WebCatalogHomeVideo).filter(
            models.WebCatalogHomeVideo.tenant_id == tenant.id
        ).delete(synchronize_session=False)
        db.query(models.PosUser).filter(models.PosUser.tenant_id == tenant.id).delete(
            synchronize_session=False
        )
        db.delete(tenant)
        db.commit()
