from uuid import uuid4

from fastapi.testclient import TestClient

import crud
import models
import schemas
from tests.conftest import TestingSessionLocal


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_sale_for_station(station_id: str) -> int:
    db = TestingSessionLocal()
    try:
        tenant_id = crud.get_default_tenant_id(db)
        product = models.Product(
            tenant_id=tenant_id,
            name="Producto ticket remoto",
            price=12500,
            cost=6000,
            unit="UND",
            stock_min=0,
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
            allow_price_change=False,
            active=True,
            service=False,
            includes_tax=False,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        reservation = crud.reserve_sale_number(
            db,
            pos_name="Tablet de prueba",
            station_id=station_id,
            tenant_id=tenant_id,
        )
        sale = crud.create_sale(
            db,
            schemas.SaleCreate(
                station_id=station_id,
                payment_method="cash",
                total=12500,
                paid_amount=12500,
                change_amount=0,
                cart_discount_value=0,
                cart_discount_percent=0,
                surcharge_amount=0,
                customer_name="Cliente tablet",
                pos_name="Tablet de prueba",
                vendor_name="Vendedor",
                reservation_id=reservation.id,
                items=[
                    schemas.SaleItemCreate(
                        product_id=product.id,
                        quantity=1,
                        unit_price=12500,
                        product_name=product.name,
                        product_sku=product.sku,
                        product_barcode=product.barcode,
                        discount=0,
                    )
                ],
                payments=[schemas.SalePaymentCreate(method="cash", amount=12500)],
            ),
            tenant_id=tenant_id,
        )
        return sale.id
    finally:
        db.close()


def _create_station(
    client: TestClient,
    headers: dict[str, str],
    *,
    station_type: str,
    parent_station_id: str | None = None,
) -> dict:
    unique = uuid4().hex[:10]
    response = client.post(
        "/pos/stations",
        headers=headers,
        json={
            "label": f"{station_type}-{unique}",
            "station_email": f"{station_type}-{unique}@example.com",
            "station_password": "secret123",
            "station_type": station_type,
            "parent_station_id": parent_station_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_tablet_print_job_is_explicit_idempotent_and_consumed_once(client: TestClient):
    headers = _auth_headers(client)
    desktop = _create_station(client, headers, station_type="desktop")
    other_desktop = _create_station(client, headers, station_type="desktop")
    tablet = _create_station(
        client,
        headers,
        station_type="tablet",
        parent_station_id=desktop["id"],
    )
    device_id = f"desktop-device-{uuid4().hex}"
    db = TestingSessionLocal()
    try:
        desktop_model = db.query(models.PosStation).filter(models.PosStation.id == desktop["id"]).one()
        desktop_model.bound_device_id = device_id
        db.commit()
    finally:
        db.close()
    agent_headers = {"X-Metrik-Device-Id": device_id}
    sale_id = _create_sale_for_station(tablet["id"])

    db = TestingSessionLocal()
    try:
        assert db.query(models.PosPrintJob).filter(models.PosPrintJob.sale_id == sale_id).count() == 0
    finally:
        db.close()

    payload = {
        "sale_id": sale_id,
        "station_id": tablet["id"],
        "request_id": f"tablet-{uuid4().hex}",
        "document_type": "ticket",
    }
    created = client.post("/pos/print-jobs", headers=headers, json=payload)
    retried = client.post("/pos/print-jobs", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert retried.status_code == 201, retried.text
    assert retried.json()["id"] == created.json()["id"]
    assert created.json()["status"] == "queued"

    unrelated = client.get(
        "/pos/print-agent/jobs/next",
        headers=agent_headers,
        params={"station_id": other_desktop["id"]},
    )
    assert unrelated.status_code == 401

    unauthorized = client.get(
        "/pos/print-agent/jobs/next",
        headers={"X-Metrik-Device-Id": f"wrong-device-{uuid4().hex}"},
        params={"station_id": desktop["id"]},
    )
    assert unauthorized.status_code == 401

    agent_config = client.get(
        "/pos/print-agent/config",
        headers=agent_headers,
        params={"station_id": desktop["id"]},
    )
    assert agent_config.status_code == 200

    claimed = client.get(
        "/pos/print-agent/jobs/next",
        headers=agent_headers,
        params={"station_id": desktop["id"]},
    )
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    assert claim["id"] == created.json()["id"]
    assert claim["status"] == "processing"
    assert claim["attempt_count"] == 1
    assert "Producto ticket remoto" in claim["document_html"]

    second_claim = client.get(
        "/pos/print-agent/jobs/next",
        headers=agent_headers,
        params={"station_id": desktop["id"]},
    )
    assert second_claim.status_code == 200
    assert second_claim.json() is None

    accepted = client.post(
        f"/pos/print-jobs/{claim['id']}/result",
        headers=agent_headers,
        json={
            "station_id": desktop["id"],
            "lease_token": claim["lease_token"],
            "status": "accepted",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"

    duplicate_result = client.post(
        f"/pos/print-jobs/{claim['id']}/result",
        headers=agent_headers,
        json={
            "station_id": desktop["id"],
            "lease_token": claim["lease_token"],
            "status": "accepted",
        },
    )
    assert duplicate_result.status_code == 409

    desktop_sale_id = _create_sale_for_station(desktop["id"])
    desktop_sale_reprint = client.post(
        "/pos/print-jobs",
        headers=headers,
        json={
            "sale_id": desktop_sale_id,
            "station_id": tablet["id"],
            "request_id": f"tablet-reprint-{uuid4().hex}",
            "document_type": "ticket",
        },
    )
    assert desktop_sale_reprint.status_code == 201, desktop_sale_reprint.text
    assert desktop_sale_reprint.json()["target_station_id"] == desktop["id"]
