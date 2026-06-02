from datetime import datetime

from fastapi.testclient import TestClient

import models
from tests.conftest import TestingSessionLocal


def _auth_headers(client: TestClient):
    resp = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert resp.status_code == 200
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _create_product():
    db = TestingSessionLocal()
    product = models.Product(
        name="Producto separado",
        sku="SEP-001",
        price=50000.0,
        cost=20000.0,
        barcode="123456",
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
    db.close()
    return product


def test_create_and_pay_separated_order(client: TestClient):
    headers = _auth_headers(client)
    product = _create_product()
    payload = {
        "payment_method": "cash",
        "total": 10000.0,
        "paid_amount": 10000.0,
        "change_amount": 0.0,
        "cart_discount_value": 0.0,
        "cart_discount_percent": 0.0,
        "customer_name": "Cliente Separado",
        "notes": "Apartado especial",
        "pos_name": "POS Principal",
        "vendor_name": "Vendedor 1",
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
        "payments": [
            {"method": "cash", "amount": 10000.0},
        ],
        "due_date": datetime.utcnow().isoformat(),
    }

    resp = client.post("/separated-orders", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["total_amount"] == 50000.0
    assert data["initial_payment"] == 10000.0
    assert data["balance"] == 40000.0
    assert data["surcharge_amount"] == 0.0
    assert data["surcharge_label"] is None
    order_id = data["id"]
    sale_id = data["sale_id"]
    barcode = data["sale_document_number"]

    sale_resp = client.get(f"/pos/sales/{sale_id}", headers=headers)
    assert sale_resp.status_code == 200
    sale_data = sale_resp.json()
    assert sale_data["is_separated"] is True
    assert sale_data["initial_payment_method"] == "cash"
    assert sale_data["initial_payment_amount"] == 10000.0
    assert sale_data["total"] == data["total_amount"]
    assert sale_data["paid_amount"] == 10000.0
    assert sale_data["cart_discount_value"] == 0.0
    assert sale_data["cart_discount_percent"] == 0.0
    assert sale_data["balance"] == data["balance"]
    assert sale_data["surcharge_amount"] == 0.0

    list_resp = client.get(f"/separated-orders?barcode={barcode}", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    payment_payload = {
        "method": "card",
        "amount": 20000.0,
        "reference": "ABCD123",
    }
    pay_resp = client.post(
        f"/separated-orders/{order_id}/payments",
        json=payment_payload,
        headers=headers,
    )
    assert pay_resp.status_code == 200
    assert pay_resp.json()["balance"] == 20000.0

    final_payment_resp = client.post(
        f"/separated-orders/{order_id}/payments",
        json={"method": "cash", "amount": 20000.0},
        headers=headers,
    )
    assert final_payment_resp.status_code == 200
    assert final_payment_resp.json()["status"] == "pagado"
    assert final_payment_resp.json()["balance"] == 0.0

    complete_resp = client.patch(
        f"/separated-orders/{order_id}/complete",
        headers=headers,
    )
    assert complete_resp.status_code == 200
    assert complete_resp.json()["completed_at"] is not None


def test_voiding_sale_cancels_linked_separated_order(client: TestClient):
    headers = _auth_headers(client)
    product = _create_product()
    payload = {
        "payment_method": "cash",
        "total": 50000.0,
        "paid_amount": 10000.0,
        "change_amount": 0.0,
        "cart_discount_value": 0.0,
        "cart_discount_percent": 0.0,
        "customer_name": "Cliente Anulación",
        "notes": "Separado a corregir",
        "pos_name": "POS Principal",
        "vendor_name": "Vendedor 1",
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
        "payments": [
            {"method": "cash", "amount": 10000.0},
        ],
        "due_date": datetime.utcnow().isoformat(),
    }

    resp = client.post("/separated-orders", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    order_id = data["id"]
    sale_id = data["sale_id"]
    barcode = data["sale_document_number"]

    void_resp = client.post(
        f"/pos/sales/{sale_id}/void",
        json={"reason": "Venta corregida"},
        headers=headers,
    )
    assert void_resp.status_code == 200

    order_resp = client.get(f"/separated-orders/{order_id}", headers=headers)
    assert order_resp.status_code == 200
    assert order_resp.json()["status"] == "cancelado"
    assert order_resp.json()["cancelled_at"] is not None

    list_resp = client.get(f"/separated-orders?barcode={barcode}", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json() == []
