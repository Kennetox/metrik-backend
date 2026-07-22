from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import models
from services.kora_web_opportunities import (
    analyze_web_opportunities,
    dispatch_web_opportunity_notifications,
)
from tests.conftest import TestingSessionLocal


BOGOTA = ZoneInfo("America/Bogota")


def _tenant(db, slug: str) -> models.Tenant:
    tenant = models.Tenant(
        slug=slug,
        name=slug.replace("-", " ").title(),
        is_active=True,
        lifecycle_stage="active",
        enabled_modules=["commerce_web"],
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _user(db, tenant_id: int, *, email: str, role: str) -> models.PosUser:
    user = models.PosUser(
        tenant_id=tenant_id,
        name=role,
        email=email,
        role=role,
        status="Activo",
        is_active=True,
        password_hash="not-used",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _product(
    db,
    tenant_id: int,
    *,
    sku: str,
    name: str,
    web_published: bool = False,
    web_category_key: str | None = None,
    group_name: str = "General",
    price: float = 100_000,
) -> models.Product:
    product = models.Product(
        tenant_id=tenant_id,
        sku=sku,
        name=name,
        price=price,
        cost=50_000,
        label_format="Kensar1",
        active=True,
        service=False,
        includes_tax=False,
        is_investment=False,
        web_published=web_published,
        web_category_key=web_category_key,
        web_short_description=None,
        group_name=group_name,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _web_category(
    db,
    tenant_id: int,
    *,
    key: str = "instrumentos",
    name: str = "Instrumentos",
    is_active: bool = True,
) -> models.WebCatalogCategory:
    category = models.WebCatalogCategory(
        tenant_id=tenant_id,
        key=key,
        name=name,
        is_active=is_active,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


def _stock(db, tenant_id: int, product_id: int, quantity: float) -> None:
    db.add(
        models.InventoryMovement(
            tenant_id=tenant_id,
            product_id=product_id,
            qty_delta=quantity,
            reason="purchase",
            reference_type="purchase",
        )
    )
    db.commit()


def _sale(
    db,
    tenant_id: int,
    product: models.Product,
    *,
    quantity: float,
    created_at: datetime,
) -> None:
    sale = models.Sale(
        tenant_id=tenant_id,
        created_at=created_at,
        status="active",
        main_payment_method="cash",
        payment_method="cash",
        total=quantity * product.price,
        paid_amount=quantity * product.price,
        change_amount=0,
    )
    db.add(sale)
    db.flush()
    db.add(
        models.SaleItem(
            tenant_id=tenant_id,
            sale_id=sale.id,
            product_id=product.id,
            product_sku=product.sku,
            product_name=product.name,
            quantity=quantity,
            unit_price=product.price,
            unit_price_original=product.price,
            discount=0,
            line_discount_value=0,
            total=quantity * product.price,
        )
    )
    db.commit()


def test_web_opportunity_analysis_is_empty_without_real_sales():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "kora-web-no-sales")
        result = analyze_web_opportunities(db, tenant_id=tenant.id)

        assert result.state == "no_sales"
        assert result.items == ()


def test_web_opportunity_analysis_only_selects_rotating_in_stock_unpublished_products():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "kora-web-ranking")
        now = datetime.now(BOGOTA)
        _web_category(db, tenant.id)
        candidate = _product(db, tenant.id, sku="WEB-1", name="Candidato real")
        published = _product(
            db,
            tenant.id,
            sku="WEB-2",
            name="Ya publicado",
            web_published=True,
            web_category_key="instrumentos",
        )
        no_stock = _product(db, tenant.id, sku="WEB-3", name="Sin existencias")
        weak_signal = _product(db, tenant.id, sku="WEB-4", name="Señal insuficiente")
        for product in (candidate, published, weak_signal):
            _stock(db, tenant.id, product.id, 8)
        _sale(db, tenant.id, candidate, quantity=4, created_at=now - timedelta(days=2))
        _sale(db, tenant.id, published, quantity=10, created_at=now - timedelta(days=1))
        _sale(db, tenant.id, no_stock, quantity=6, created_at=now - timedelta(days=1))
        _sale(db, tenant.id, weak_signal, quantity=1, created_at=now - timedelta(days=1))

        result = analyze_web_opportunities(
            db,
            tenant_id=tenant.id,
            reference_time=now,
        )

        assert result.state == "opportunities"
        assert [item.product_id for item in result.items] == [candidate.id]
        assert result.items[0].units_lookback == 4
        assert result.items[0].qty_on_hand == 8
        assert result.items[0].sale_price == 100_000
        assert result.items[0].suggested_category_key == "instrumentos"
        assert result.items[0].suggested_category_name == "Instrumentos"
        assert "imagen" in result.items[0].missing_web_fields


def test_web_opportunity_analysis_excludes_cheap_and_non_web_groups():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "kora-web-commercial-fit")
        now = datetime.now(BOGOTA)
        _web_category(db, tenant.id, key="audio", name="Audio")
        _product(
            db,
            tenant.id,
            sku="WEB-ANCHOR",
            name="Producto web publicado",
            web_published=True,
            web_category_key="audio",
            group_name="Audio profesional",
        )
        meaningful = _product(
            db,
            tenant.id,
            sku="WEB-GOOD",
            name="Interfaz de audio",
            group_name="Audio profesional",
            price=350_000,
        )
        cheap = _product(
            db,
            tenant.id,
            sku="WEB-CHEAP",
            name="Cable Jumper",
            group_name="Audio profesional",
            price=300,
        )
        unrelated = _product(
            db,
            tenant.id,
            sku="WEB-OTHER",
            name="Repuesto interno",
            group_name="Repuestos internos",
            price=500_000,
        )
        for product in (meaningful, cheap, unrelated):
            _stock(db, tenant.id, product.id, 100)
        _sale(db, tenant.id, meaningful, quantity=4, created_at=now - timedelta(days=2))
        _sale(db, tenant.id, cheap, quantity=70, created_at=now - timedelta(days=1))
        _sale(db, tenant.id, unrelated, quantity=20, created_at=now - timedelta(days=1))

        result = analyze_web_opportunities(
            db,
            tenant_id=tenant.id,
            reference_time=now,
        )

        assert [item.product_id for item in result.items] == [meaningful.id]
        assert result.minimum_sale_price == 10_000
        assert result.eligible_group_count == 1


def test_weekly_web_opportunity_dispatch_targets_web_managers_and_deduplicates():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "kora-web-dispatch")
        _web_category(db, tenant.id)
        admin = _user(
            db,
            tenant.id,
            email="kora-web-admin@test.local",
            role="Administrador",
        )
        web_manager = _user(
            db,
            tenant.id,
            email="kora-web-manager@test.local",
            role="Gestor Web",
        )
        seller = _user(
            db,
            tenant.id,
            email="kora-web-seller@test.local",
            role="Vendedor",
        )
        tenant.module_user_access = {
            "commerce_web": [admin.id, web_manager.id, seller.id],
        }
        db.commit()

        _product(
            db,
            tenant.id,
            sku="WEB-ANCHOR-2",
            name="Producto publicado del grupo",
            web_published=True,
            web_category_key="instrumentos",
        )
        product = _product(db, tenant.id, sku="WEB-5", name="Producto destacado")
        _stock(db, tenant.id, product.id, 12)
        now = datetime(2026, 7, 22, 10, 0, tzinfo=BOGOTA)
        _sale(db, tenant.id, product, quantity=5, created_at=now - timedelta(days=2))

        first = dispatch_web_opportunity_notifications(
            db,
            tenant_id=tenant.id,
            trigger="weekly",
            reference_time=now,
        )
        second = dispatch_web_opportunity_notifications(
            db,
            tenant_id=tenant.id,
            trigger="weekly",
            reference_time=now,
        )

        assert first.recipient_count == 2
        assert first.created_count == 2
        assert first.duplicate_count == 0
        assert second.created_count == 0
        assert second.duplicate_count == 2
        notifications = (
            db.query(models.UserNotification)
            .filter(models.UserNotification.tenant_id == tenant.id)
            .all()
        )
        assert {notification.user_id for notification in notifications} == {
            admin.id,
            web_manager.id,
        }
        assert all(notification.source == "kora" for notification in notifications)
        assert all(notification.module_id == "commerce_web" for notification in notifications)
        assert all(notification.payload["radar_version"] == 2 for notification in notifications)
        assert all(notification.payload["opportunities"][0]["product_id"] == product.id for notification in notifications)
        assert all(notification.payload["opportunities"][0]["sale_price"] == 100_000 for notification in notifications)
