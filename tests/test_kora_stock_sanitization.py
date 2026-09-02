from datetime import datetime, timedelta

import models
from services.kora_stock_sanitization import (
    read_operational_context,
    retrieve_or_create_plan,
    serialize_plan,
)
from services.operational_notifications import dispatch_stock_sanitization_notifications
from tests.conftest import TestingSessionLocal


REFERENCE = datetime(2026, 9, 1, 15, 0, 0)  # 10:00 a. m. America/Bogota


def _tenant(db, slug: str) -> models.Tenant:
    tenant = models.Tenant(slug=slug, name=slug, is_active=True, enabled_modules=[])
    db.add(tenant)
    db.flush()
    return tenant


def _admin(db, tenant_id: int, suffix: str) -> models.PosUser:
    user = models.PosUser(
        tenant_id=tenant_id,
        name="Administrador",
        email=f"admin-{suffix}@test.local",
        role="Administrador",
        status="Activo",
        is_active=True,
        password_hash="not-used",
    )
    db.add(user)
    db.flush()
    return user


def _negative_products(db, tenant_id: int, *, count: int = 8, group: str = "Cables/Sonido"):
    products = []
    for index in range(count):
        product = models.Product(
            tenant_id=tenant_id,
            sku=f"NEG-{tenant_id}-{index}",
            name=f"Producto negativo {index}",
            price=30_000 + index * 1_000,
            cost=15_000 + index * 500,
            group_name=group,
            active=True,
            service=False,
            web_published=index == 0,
        )
        db.add(product)
        db.flush()
        db.add(
            models.InventoryMovement(
                tenant_id=tenant_id,
                product_id=product.id,
                qty_delta=-(index + 1),
                reason="sale",
                created_at=REFERENCE - timedelta(days=index),
            )
        )
        products.append(product)
    db.flush()
    return products


def _published_shift(db, tenant_id: int, *, names: list[str], reference: datetime = REFERENCE):
    week = models.ScheduleWeek(
        tenant_id=tenant_id,
        week_start=reference.date() - timedelta(days=1),
        status="published",
        published_at=reference - timedelta(days=1),
    )
    db.add(week)
    db.flush()
    for index, name in enumerate(names):
        employee = models.HREmployee(
            tenant_id=tenant_id,
            name=name,
            status="Activo",
            order_index=index,
        )
        db.add(employee)
        db.flush()
        db.add(
            models.ScheduleShift(
                tenant_id=tenant_id,
                week_id=week.id,
                employee_id=employee.id,
                shift_date=reference.date(),
                start_time="08:30",
                end_time="18:15",
                is_time_off=False,
            )
        )


def test_manual_plan_is_ranked_persisted_and_retrieved_without_duplicates(client):
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "kora-stock-manual")
        admin = _admin(db, tenant.id, "manual")
        products = _negative_products(db, tenant.id, count=8)
        sale = models.Sale(
            tenant_id=tenant.id,
            status="active",
            total=90_000,
            created_at=REFERENCE - timedelta(days=2),
        )
        db.add(sale)
        db.flush()
        db.add(
            models.SaleItem(
                tenant_id=tenant.id,
                sale_id=sale.id,
                product_id=products[0].id,
                product_name=products[0].name,
                quantity=3,
                unit_price=30_000,
                total=90_000,
            )
        )
        db.commit()

        first = retrieve_or_create_plan(
            db,
            tenant_id=tenant.id,
            requested_count=5,
            trigger="manual",
            created_by_user_id=admin.id,
            reference_time=REFERENCE,
        )
        repeated = retrieve_or_create_plan(
            db,
            tenant_id=tenant.id,
            requested_count=8,
            trigger="manual",
            created_by_user_id=admin.id,
            reference_time=REFERENCE,
        )

        assert first.state == "ready"
        assert first.plan is not None
        assert repeated.state == "existing"
        assert repeated.plan is not None and repeated.plan.id == first.plan.id
        assert first.plan.selected_count == 5
        payload = serialize_plan(first.plan)
        assert len(payload["items"]) == 5
        sold_item = next(item for item in payload["items"] if item["product_id"] == products[0].id)
        assert payload["items"][0]["priority_rank"] == 1
        assert sold_item["units_sold_lookback"] == 3
        assert first.plan.converted_recount_id is None


def test_automatic_notification_accounts_for_schedule_reception_and_sales(client):
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "kora-stock-automatic")
        admin = _admin(db, tenant.id, "automatic")
        _negative_products(db, tenant.id, count=14)
        _published_shift(db, tenant.id, names=["Erika", "Luz Aida", "Adriana", "Ángelo", "Santiago"])
        db.add(
            models.ReceivingLot(
                tenant_id=tenant.id,
                status="open",
                origin_name="Proveedor de prueba",
                purchase_type="cash",
                lot_number=f"RC-{tenant.id:06d}",
                created_at=REFERENCE - timedelta(minutes=20),
            )
        )
        db.commit()

        context = read_operational_context(db, tenant_id=tenant.id, reference_time=REFERENCE)
        notice = dispatch_stock_sanitization_notifications(
            db,
            tenant_id=tenant.id,
            reference_time=REFERENCE,
        )
        repeated = dispatch_stock_sanitization_notifications(
            db,
            tenant_id=tenant.id,
            reference_time=REFERENCE,
        )

        assert context.scheduled_people == 5
        assert context.reserved_for_receiving == 2
        assert context.available_people == 2
        assert context.workload_state == "quiet"
        assert context.automatic_plan_allowed is True
        assert notice is not None and notice.created_count == 1
        assert notice.recipient_ids == (admin.id,)
        assert repeated is not None and repeated.created_count == 0
        notification = (
            db.query(models.UserNotification)
            .filter(
                models.UserNotification.tenant_id == tenant.id,
                models.UserNotification.category == "stock_sanitization",
            )
            .one()
        )
        assert notification.payload["plan"]["selected_count"] == 12
        assert notification.payload["plan"]["context"]["open_receiving_count"] == 1


def test_automatic_plan_waits_when_recent_sales_are_busy(client):
    with TestingSessionLocal() as db:
        busy_reference = REFERENCE + timedelta(days=7)
        tenant = _tenant(db, "kora-stock-busy")
        _admin(db, tenant.id, "busy")
        _negative_products(db, tenant.id, count=8)
        _published_shift(
            db,
            tenant.id,
            names=["Uno", "Dos", "Tres", "Cuatro"],
            reference=busy_reference,
        )
        for index in range(4):
            db.add(
                models.Sale(
                    tenant_id=tenant.id,
                    status="active",
                    total=20_000,
                    created_at=busy_reference - timedelta(minutes=index + 1),
                )
            )
        db.commit()

        context = read_operational_context(db, tenant_id=tenant.id, reference_time=busy_reference)
        notice = dispatch_stock_sanitization_notifications(
            db,
            tenant_id=tenant.id,
            reference_time=busy_reference,
        )

        assert context.workload_state == "busy"
        assert context.automatic_plan_allowed is False
        assert notice is None
        assert (
            db.query(models.KoraStockPlan)
            .filter(models.KoraStockPlan.tenant_id == tenant.id)
            .count()
            == 0
        )


def test_stock_plan_api_retrieves_the_same_persisted_plan(client):
    login = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    with TestingSessionLocal() as db:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").one()
        products = _negative_products(db, tenant.id, count=6, group="Prueba Kora/API")
        db.commit()
        product_ids = [product.id for product in products]

    created = client.post(
        "/kora/stock-sanitization-plans/retrieve",
        headers=headers,
        json={"requested_count": 5, "lookback_days": 30, "group_name": "Prueba Kora"},
    )
    assert created.status_code == 200, created.text
    created_payload = created.json()
    assert created_payload["state"] == "ready"
    assert created_payload["plan"]["selected_count"] == 5

    current = client.get("/kora/stock-sanitization-plans/current", headers=headers)
    assert current.status_code == 200
    assert current.json()["plan"]["id"] == created_payload["plan"]["id"]

    with TestingSessionLocal() as db:
        plan_id = created_payload["plan"]["id"]
        db.query(models.KoraStockPlanItem).filter(
            models.KoraStockPlanItem.plan_id == plan_id
        ).delete(synchronize_session=False)
        db.query(models.KoraStockPlan).filter(models.KoraStockPlan.id == plan_id).delete(
            synchronize_session=False
        )
        db.query(models.InventoryMovement).filter(
            models.InventoryMovement.product_id.in_(product_ids)
        ).delete(synchronize_session=False)
        db.query(models.Product).filter(models.Product.id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db.commit()


def test_stock_plan_can_be_converted_once_into_a_device_recount(client):
    login = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    device_id = "stock-kora-conversion-test"

    with TestingSessionLocal() as db:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").one()
        admin = db.query(models.PosUser).filter(models.PosUser.email == "master@kensar.com").one()
        products = _negative_products(db, tenant.id, count=6, group="Prueba Kora/Conversión")
        device = models.StockDevice(
            id=device_id,
            tenant_id=tenant.id,
            name="Tablet Kora test",
            is_active=True,
        )
        db.add(device)
        db.commit()
        product_ids = [product.id for product in products]
        result = retrieve_or_create_plan(
            db,
            tenant_id=tenant.id,
            requested_count=5,
            group_name="Prueba Kora/Conversión",
            trigger="manual",
            created_by_user_id=admin.id,
        )
        assert result.plan is not None
        plan_id = result.plan.id

    converted = client.post(
        f"/kora/stock-sanitization-plans/{plan_id}/convert",
        headers=headers,
        json={"stock_device_id": device_id, "count_mode": "blind"},
    )
    assert converted.status_code == 200, converted.text
    payload = converted.json()
    assert payload["plan"]["status"] == "converted"
    assert payload["recount"]["source"] == "app"
    assert payload["recount"]["stock_device_id"] == device_id
    assert payload["recount"]["summary"]["total_lines"] == 5

    selected_product_ids = {item["product_id"] for item in payload["plan"]["items"]}
    extra_product_id = next(product_id for product_id in product_ids if product_id not in selected_product_ids)
    rejected_extra = client.post(
        f"/inventory/recounts/{payload['recount']['id']}/lines",
        headers=headers,
        json={"product_id": extra_product_id, "counted_qty": 1},
    )
    assert rejected_extra.status_code == 400
    assert "plan de saneamiento" in rejected_extra.json()["detail"]

    rejected_close = client.post(
        f"/inventory/recounts/{payload['recount']['id']}/close",
        headers=headers,
        json={},
    )
    assert rejected_close.status_code == 400
    assert "productos pendientes" in rejected_close.json()["detail"]

    repeated = client.post(
        f"/kora/stock-sanitization-plans/{plan_id}/convert",
        headers=headers,
        json={"stock_device_id": device_id, "count_mode": "blind"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["recount"]["id"] == payload["recount"]["id"]

    with TestingSessionLocal() as db:
        recount_id = payload["recount"]["id"]
        db.query(models.InventoryRecountLine).filter(
            models.InventoryRecountLine.recount_id == recount_id
        ).delete(synchronize_session=False)
        db.query(models.KoraStockPlanItem).filter(
            models.KoraStockPlanItem.plan_id == plan_id
        ).delete(synchronize_session=False)
        db.query(models.KoraStockPlan).filter(models.KoraStockPlan.id == plan_id).delete(
            synchronize_session=False
        )
        db.query(models.InventoryRecount).filter(models.InventoryRecount.id == recount_id).delete(
            synchronize_session=False
        )
        db.query(models.InventoryMovement).filter(
            models.InventoryMovement.product_id.in_(product_ids)
        ).delete(synchronize_session=False)
        db.query(models.Product).filter(models.Product.id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db.query(models.StockDevice).filter(models.StockDevice.id == device_id).delete(
            synchronize_session=False
        )
        db.commit()
