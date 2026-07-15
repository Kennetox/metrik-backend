from uuid import uuid4
from types import SimpleNamespace
import logging

from fastapi.testclient import TestClient

import crud
import models
from db_migrations import _backfill_hr_employees_from_users
from routers import pos as pos_router
from routers import separated_orders as separated_router
from security import hash_password
from tests.conftest import TestingSessionLocal, engine


def test_request_observability_preserves_safe_client_trace_id(client: TestClient):
    request_id = f"trace-{uuid4().hex}"

    response = client.get("/healthz", headers={"X-Request-ID": request_id})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == request_id
    assert response.headers["Server-Timing"].startswith("app;dur=")


def test_validation_logs_and_response_never_include_request_body(
    client: TestClient,
    caplog,
):
    request_id = f"validation-{uuid4().hex}"
    secret_marker = "never-log-this-password"

    with caplog.at_level(logging.ERROR, logger="kensar.validation"):
        response = client.post(
            "/auth/login",
            json={"email": "cashier@example.com", "password": {"value": secret_marker}},
            headers={"X-Request-ID": request_id},
        )

    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == request_id
    assert response.json()["request_id"] == request_id
    assert secret_marker not in response.text
    assert secret_marker not in caplog.text


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_product(*, tenant_id: int | None = None) -> models.Product:
    unique = uuid4().hex[:12]
    db = TestingSessionLocal()
    try:
        effective_tenant_id = tenant_id or crud.get_default_tenant_id(db)
        product = models.Product(
            tenant_id=effective_tenant_id,
            name=f"Producto seguridad {unique}",
            sku=f"SAFE-{unique}",
            barcode=f"SAFE-{unique}",
            price=50000.0,
            cost=20000.0,
            unit="UND",
            active=True,
            service=False,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        db.expunge(product)
        return product
    finally:
        db.close()


def _sale_payload(
    product: models.Product,
    request_id: str,
    *,
    paid_amount: float = 50000.0,
) -> dict:
    return {
        "client_request_id": request_id,
        "payment_method": "cash",
        "total": 50000.0,
        "paid_amount": paid_amount,
        "change_amount": max(0.0, paid_amount - 50000.0),
        "pos_name": "POS Web",
        "vendor_name": "Prueba seguridad",
        "items": [
            {
                "product_id": product.id,
                "quantity": 1,
                "unit_price": 50000.0,
                "product_sku": product.sku,
                "product_name": product.name,
                "product_barcode": product.barcode,
                "discount": 0.0,
            }
        ],
        "payments": [{"method": "cash", "amount": paid_amount}],
    }


def test_pos_query_caches_are_isolated_by_authenticated_scope():
    pos_router._POS_QUERY_CACHE.clear()
    separated_router._SEPARATED_ORDERS_CACHE.clear()
    calls = {"pos": 0, "separated": 0}

    @pos_router._pos_cached(ttl_seconds=10, fallback_factory=list)
    def cached_pos(*, db, current_user):
        calls["pos"] += 1
        return current_user.tenant_id

    @separated_router._separated_cached(ttl_seconds=10, fallback_factory=list)
    def cached_separated(*, db, current_user):
        calls["separated"] += 1
        return current_user.tenant_id

    first_user = SimpleNamespace(id=101, tenant_id=1, role="Vendedor")
    other_tenant_user = SimpleNamespace(id=202, tenant_id=2, role="Vendedor")

    assert cached_pos(db=object(), current_user=first_user) == 1
    assert cached_pos(db=object(), current_user=first_user) == 1
    assert cached_pos(db=object(), current_user=other_tenant_user) == 2
    assert calls["pos"] == 2

    assert cached_separated(db=object(), current_user=first_user) == 1
    assert cached_separated(db=object(), current_user=first_user) == 1
    assert cached_separated(db=object(), current_user=other_tenant_user) == 2
    assert calls["separated"] == 2


def test_local_schema_backfill_supports_fresh_non_nullable_hr_columns():
    unique = uuid4().hex[:12]
    db = TestingSessionLocal()
    try:
        user = models.PosUser(
            tenant_id=crud.get_default_tenant_id(db),
            name=f"Usuario arranque {unique}",
            email=f"startup-{unique}@local.test",
            role="Vendedor",
            status="Activo",
            is_active=True,
            password_hash=hash_password("safe-test-password"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
    finally:
        db.close()

    with engine.begin() as connection:
        _backfill_hr_employees_from_users(connection, backend="sqlite")

    db = TestingSessionLocal()
    try:
        employee = db.query(models.HREmployee).filter(models.HREmployee.id == user_id).one()
        assert employee.show_in_schedule is True
        assert employee.order_index == 0
        assert employee.tenant_id is not None
        linked_user = db.query(models.PosUser).filter(models.PosUser.id == user_id).one()
        assert linked_user.employee_id == employee.id
    finally:
        db.close()


def test_sale_retry_is_idempotent_and_does_not_duplicate_inventory(
    client: TestClient,
):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"sale-{uuid4().hex}"
    payload = _sale_payload(product, request_id)

    first = client.post("/pos/sales", json=payload, headers=headers)
    retry = client.post("/pos/sales", json=payload, headers=headers)

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["document_number"] == first.json()["document_number"]

    db = TestingSessionLocal()
    try:
        sales = (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .all()
        )
        assert len(sales) == 1
        movements = (
            db.query(models.InventoryMovement)
            .filter(
                models.InventoryMovement.reference_type == "sale",
                models.InventoryMovement.reference_id == sales[0].id,
                models.InventoryMovement.product_id == product.id,
            )
            .all()
        )
        assert len(movements) == 1
        assert movements[0].qty_delta == -1
    finally:
        db.close()


def test_multiple_payment_sale_persists_exact_breakdown(client: TestClient):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"multiple-{uuid4().hex}"
    payload = _sale_payload(product, request_id)
    payload["payment_method"] = "mixed"
    payload["payments"] = [
        {"method": "cash", "amount": 20000.0},
        {"method": "card", "amount": 30000.0},
    ]

    response = client.post("/pos/sales", json=payload, headers=headers)

    assert response.status_code == 201
    data = response.json()
    assert data["paid_amount"] == 50000.0
    assert sorted(
        (payment["method"], payment["amount"])
        for payment in data["payments"]
    ) == [("card", 30000.0), ("cash", 20000.0)]

    db = TestingSessionLocal()
    try:
        sale = (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .one()
        )
        assert sale.main_payment_method == "mixed"
        assert len(sale.payments) == 2
        assert sum(float(payment.amount) for payment in sale.payments) == 50000.0
    finally:
        db.close()


def test_sale_read_ignores_legacy_zero_payment_without_hiding_valid_payments(
    client: TestClient,
    caplog,
):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"legacy-zero-{uuid4().hex}"
    payload = _sale_payload(product, request_id)
    payload["payment_method"] = "mixed"
    payload["payments"] = [
        {"method": "cash", "amount": 20000.0},
        {"method": "card", "amount": 30000.0},
    ]
    created = client.post("/pos/sales", json=payload, headers=headers)
    assert created.status_code == 201
    sale_id = created.json()["id"]

    db = TestingSessionLocal()
    try:
        sale = db.query(models.Sale).filter(models.Sale.id == sale_id).one()
        legacy_payment = models.SalePayment(
            tenant_id=sale.tenant_id,
            sale_id=sale.id,
            method="qr",
            amount=0.0,
            is_primary=False,
        )
        db.add(legacy_payment)
        db.commit()
        db.refresh(legacy_payment)
        legacy_payment_id = legacy_payment.id
    finally:
        db.close()

    with caplog.at_level(logging.WARNING, logger="kensar.pos"):
        response = client.get(f"/pos/sales/{sale_id}", headers=headers)

    assert response.status_code == 200
    assert sorted(
        (payment["method"], payment["amount"])
        for payment in response.json()["payments"]
    ) == [("card", 30000.0), ("cash", 20000.0)]
    assert str(legacy_payment_id) in caplog.text


def test_new_sale_still_rejects_zero_payment_lines(client: TestClient):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"reject-zero-{uuid4().hex}"
    payload = _sale_payload(product, request_id)
    payload["payment_method"] = "mixed"
    payload["payments"] = [
        {"method": "cash", "amount": 50000.0},
        {"method": "qr", "amount": 0.0},
    ]

    response = client.post("/pos/sales", json=payload, headers=headers)

    assert response.status_code == 422
    db = TestingSessionLocal()
    try:
        assert (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_separated_order_successful_retry_returns_same_order(
    client: TestClient,
):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"separated-ok-{uuid4().hex}"
    payload = _sale_payload(product, request_id, paid_amount=20000.0)

    first = client.post("/separated-orders", json=payload, headers=headers)
    retry = client.post("/separated-orders", json=payload, headers=headers)

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["sale_id"] == first.json()["sale_id"]
    assert first.json()["total_amount"] == 50000.0
    assert first.json()["initial_payment"] == 20000.0
    assert first.json()["balance"] == 30000.0

    first_payment = client.post(
        f"/separated-orders/{first.json()['id']}/payments",
        json={"method": "transfer", "amount": 10000.0, "reference": "SAFE-1"},
        headers=headers,
    )
    final_payment = client.post(
        f"/separated-orders/{first.json()['id']}/payments",
        json={"method": "cash", "amount": 20000.0},
        headers=headers,
    )
    completed = client.patch(
        f"/separated-orders/{first.json()['id']}/complete",
        headers=headers,
    )

    assert first_payment.status_code == 200
    assert first_payment.json()["balance"] == 20000.0
    assert final_payment.status_code == 200
    assert final_payment.json()["balance"] == 0.0
    assert final_payment.json()["status"] == "pagado"
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None

    db = TestingSessionLocal()
    try:
        sale = (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .one()
        )
        assert (
            db.query(models.SeparatedOrder)
            .filter(models.SeparatedOrder.sale_id == sale.id)
            .count()
            == 1
        )
        assert (
            db.query(models.SeparatedOrderPayment)
            .join(models.SeparatedOrder)
            .filter(models.SeparatedOrder.sale_id == sale.id)
            .count()
            == 2
        )
        assert (
            db.query(models.InventoryMovement)
            .filter(
                models.InventoryMovement.reference_type == "sale",
                models.InventoryMovement.reference_id == sale.id,
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_idempotency_key_cannot_be_reused_for_a_different_cart(
    client: TestClient,
):
    headers = _auth_headers(client)
    first_product = _create_product()
    second_product = _create_product()
    request_id = f"sale-{uuid4().hex}"

    first = client.post(
        "/pos/sales",
        json=_sale_payload(first_product, request_id),
        headers=headers,
    )
    conflicting_retry = client.post(
        "/pos/sales",
        json=_sale_payload(second_product, request_id),
        headers=headers,
    )

    assert first.status_code == 201
    assert conflicting_retry.status_code == 409
    assert "venta diferente" in conflicting_retry.json()["detail"]

    db = TestingSessionLocal()
    try:
        assert (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .count()
            == 1
        )
        assert (
            db.query(models.InventoryMovement)
            .filter(
                models.InventoryMovement.reference_type == "sale",
                models.InventoryMovement.product_id == second_product.id,
            )
            .count()
            == 0
        )
    finally:
        db.close()


def test_sale_rejects_product_from_another_tenant(client: TestClient):
    headers = _auth_headers(client)
    unique = uuid4().hex[:12]
    db = TestingSessionLocal()
    try:
        foreign_tenant = models.Tenant(
            slug=f"foreign-{unique}",
            name=f"Empresa externa {unique}",
            is_active=True,
        )
        db.add(foreign_tenant)
        db.commit()
        db.refresh(foreign_tenant)
        foreign_tenant_id = foreign_tenant.id
    finally:
        db.close()

    product = _create_product(tenant_id=foreign_tenant_id)
    request_id = f"cross-{uuid4().hex}"
    response = client.post(
        "/pos/sales",
        json=_sale_payload(product, request_id),
        headers=headers,
    )

    assert response.status_code == 400
    assert "no pertenecen" in response.json()["detail"]
    db = TestingSessionLocal()
    try:
        assert (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_sale_without_positive_payment_returns_controlled_validation_error(
    client: TestClient,
):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"no-payment-{uuid4().hex}"
    payload = _sale_payload(product, request_id)
    payload["paid_amount"] = 0
    payload["change_amount"] = 0
    payload.pop("payments")

    response = client.post("/pos/sales", json=payload, headers=headers)

    assert response.status_code == 400
    assert "pago mayor a cero" in response.json()["detail"]
    db = TestingSessionLocal()
    try:
        assert (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_separated_order_failure_rolls_back_sale_and_inventory(
    client: TestClient,
):
    headers = _auth_headers(client)
    product = _create_product()
    request_id = f"separated-{uuid4().hex}"
    payload = _sale_payload(product, request_id, paid_amount=60000.0)

    response = client.post("/separated-orders", json=payload, headers=headers)

    assert response.status_code == 400
    assert "no puede superar" in response.json()["detail"]
    db = TestingSessionLocal()
    try:
        assert (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .count()
            == 0
        )
        assert (
            db.query(models.InventoryMovement)
            .filter(
                models.InventoryMovement.product_id == product.id,
                models.InventoryMovement.reference_type == "sale",
            )
            .count()
            == 0
        )
    finally:
        db.close()
