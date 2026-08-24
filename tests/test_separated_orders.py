from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

import models
import crud
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
    unique_suffix = uuid4().hex[:10]
    product = models.Product(
        tenant_id=crud.get_default_tenant_id(db),
        name="Producto separado",
        sku=f"SEP-{unique_suffix}",
        price=50000.0,
        cost=20000.0,
        barcode=f"SEP{unique_suffix}",
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


def _create_test_separated(
    client: TestClient,
    headers: dict[str, str],
    *,
    paid: float = 10000.0,
    quantity: float = 1,
    due_days: int = 2,
):
    product = _create_product()
    total = 50000.0 * quantity
    payload = {
        "payment_method": "cash",
        "total": total,
        "paid_amount": paid,
        "change_amount": 0.0,
        "cart_discount_value": 0.0,
        "cart_discount_percent": 0.0,
        "customer_name": "Cliente Resolución",
        "notes": "Caso administrativo",
        "pos_name": "POS Principal",
        "vendor_name": "Vendedor 1",
        "items": [
            {
                "product_id": product.id,
                "quantity": quantity,
                "unit_price": 50000.0,
                "product_sku": product.sku,
                "product_name": product.name,
                "product_barcode": product.barcode,
                "discount": 0.0,
            }
        ],
        "payments": [{"method": "cash", "amount": paid}],
        "due_date": (datetime.utcnow() + timedelta(days=due_days)).isoformat(),
    }
    response = client.post("/separated-orders", json=payload, headers=headers)
    assert response.status_code == 201
    return response.json()


def test_create_and_pay_separated_order(client: TestClient):
    headers = _auth_headers(client)
    product = _create_product()
    payload = {
        "payment_method": "cash",
        "total": 584000.0,
        "paid_amount": 400000.0,
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
                "unit_price": 584000.0,
                "product_sku": product.sku,
                "product_name": product.name,
                "product_barcode": product.barcode,
                "discount": 0.0,
            }
        ],
        "payments": [
            {"method": "cash", "amount": 150000.0},
            {"method": "transfer", "amount": 250000.0, "reference": "BANCOL-001"},
        ],
        "due_date": datetime.utcnow().isoformat(),
    }

    resp = client.post("/separated-orders", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["total_amount"] == 584000.0
    assert data["initial_payment"] == 400000.0
    assert data["balance"] == 184000.0
    assert len(data["items"]) == 1
    assert data["items"][0]["product_name"] == "Producto separado"
    assert data["items"][0]["quantity"] == 1
    assert len(data["initial_payments"]) == 2
    assert data["initial_payments"][0]["method"] == "cash"
    assert data["initial_payments"][0]["amount"] == 150000.0
    assert data["initial_payments"][1]["method"] == "transfer"
    assert data["initial_payments"][1]["amount"] == 250000.0
    assert data["surcharge_amount"] == 0.0
    assert data["surcharge_label"] is None
    order_id = data["id"]
    sale_id = data["sale_id"]
    barcode = data["sale_document_number"]

    sale_resp = client.get(f"/pos/sales/{sale_id}", headers=headers)
    assert sale_resp.status_code == 200
    sale_data = sale_resp.json()
    assert sale_data["is_separated"] is True
    assert sale_data["initial_payment_amount"] == 400000.0
    assert sale_data["total"] == data["total_amount"]
    assert sale_data["paid_amount"] == 400000.0
    assert sale_data["cart_discount_value"] == 0.0
    assert sale_data["cart_discount_percent"] == 0.0
    assert sale_data["balance"] == data["balance"]
    assert sale_data["surcharge_amount"] == 0.0

    list_resp = client.get(f"/separated-orders?barcode={barcode}", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    payment_payload = {
        "method": "card",
        "amount": 164000.0,
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


def test_reconcile_separated_order_does_not_create_payment(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers)

    partial = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "reconcile",
            "amount": 25000,
            "reference": "V-EXTERNA-01",
            "reason": "Pago registrado por otra venta",
        },
        headers=headers,
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "reservado"
    assert partial.json()["recorded_paid_total"] == 10000
    assert partial.json()["reconciled_amount"] == 25000
    assert partial.json()["balance"] == 15000
    assert partial.json()["payments"] == []

    completed = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "reconcile",
            "amount": 15000,
            "reference": "V-EXTERNA-01",
        },
        headers=headers,
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "conciliado"
    assert completed.json()["balance"] == 0
    assert completed.json()["recorded_paid_total"] == 10000
    assert completed.json()["reconciled_amount"] == 40000
    assert len(completed.json()["resolution_history"]) == 2


def test_cancel_without_refund_retains_paid_amount_and_releases_inventory(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers)

    response = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "cancel",
            "reason": "Cliente desistió",
            "refund_amount": 0,
            "remainder_disposition": "retained",
        },
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelado"
    assert data["balance"] == 0
    assert data["balance_before_resolution"] == 40000
    assert data["waived_amount"] == 40000
    assert data["retained_amount"] == 10000
    assert data["refunded_total"] == 0
    assert data["inventory_released_at"] is not None


def test_cancel_with_partial_refund_records_only_real_outflow(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers)

    response = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "cancel",
            "reason": "Acuerdo cancelado",
            "refund_amount": 6000,
            "refund_method": "cash",
            "remainder_disposition": "retained",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "cancelado"
    assert data["recorded_paid_total"] == 10000
    assert data["refunded_total"] == 6000
    assert data["net_paid_total"] == 4000
    assert data["retained_amount"] == 4000
    assert data["resolution_type"] == "cancel_partial_refund"

    db = TestingSessionLocal()
    try:
        sale_return = (
            db.query(models.SaleReturn)
            .filter(models.SaleReturn.sale_id == order["sale_id"])
            .one()
        )
        assert sale_return.total_refund == 6000
        assert sum(payment.amount for payment in sale_return.payments) == 6000
    finally:
        db.close()


def test_cancel_and_later_pay_pending_refund(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers)
    cancelled = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "cancel",
            "reason": "Reembolso programado",
            "refund_amount": 0,
            "remainder_disposition": "pending_refund",
        },
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["pending_refund_amount"] == 10000

    refunded = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "refund_pending",
            "amount": 10000,
            "refund_method": "cash",
            "reason": "Entrega posterior al cliente",
        },
        headers=headers,
    )
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["pending_refund_amount"] == 0
    assert refunded.json()["refunded_total"] == 10000
    assert refunded.json()["net_paid_total"] == 0


def test_reschedule_keeps_order_active(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers)
    new_due_date = datetime.utcnow() + timedelta(days=15)
    response = client.post(
        f"/separated-orders/{order['id']}/resolve",
        json={
            "action": "reschedule",
            "due_date": new_due_date.isoformat(),
            "reason": "Acuerdo con el cliente",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "reservado"
    assert response.json()["balance"] == 40000
    assert response.json()["resolution_type"] == "rescheduled"


def test_full_pos_return_automatically_closes_separated_order(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers)

    response = client.post(
        "/pos/returns",
        json={
            "sale_id": order["sale_id"],
            "items": [
                {
                    "sale_item_id": order["items"][0]["id"],
                    "quantity": 1,
                    "reason": "Cliente desistió",
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    refreshed = client.get(
        f"/separated-orders/{order['id']}", headers=headers
    )
    assert refreshed.status_code == 200
    data = refreshed.json()
    assert data["status"] == "cancelado"
    assert data["balance"] == 0
    assert data["active_total_amount"] == 0
    assert data["refunded_total"] == 10000
    assert data["pending_refund_amount"] == 0
    assert data["balance_before_resolution"] == 40000
    assert data["resolution_reference"] == response.json()["document_number"]
    assert data["resolution_history"][-1]["action"] == "pos_return"


def test_partial_pos_return_recalculates_active_total_and_balance(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers, quantity=2)

    response = client.post(
        "/pos/returns",
        json={
            "sale_id": order["sale_id"],
            "items": [
                {
                    "sale_item_id": order["items"][0]["id"],
                    "quantity": 1,
                    "reason": "Devolución parcial",
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    refreshed = client.get(
        f"/separated-orders/{order['id']}", headers=headers
    )
    assert refreshed.status_code == 200
    data = refreshed.json()
    assert data["status"] == "reservado"
    assert data["active_total_amount"] == 50000
    assert data["recorded_paid_total"] == 10000
    assert data["refunded_total"] == 10000
    assert data["net_paid_total"] == 0
    assert data["balance"] == 50000
    assert data["resolution_history"][-1]["action"] == "pos_partial_return"

    final_return = client.post(
        "/pos/returns",
        json={
            "sale_id": order["sale_id"],
            "items": [
                {
                    "sale_item_id": order["items"][0]["id"],
                    "quantity": 1,
                    "reason": "Devolución del producto restante",
                }
            ],
        },
        headers=headers,
    )
    assert final_return.status_code == 201, final_return.text
    assert final_return.json()["total_refund"] == 0

    closed = client.get(f"/separated-orders/{order['id']}", headers=headers)
    assert closed.status_code == 200
    assert closed.json()["status"] == "cancelado"
    assert closed.json()["active_total_amount"] == 0
    assert closed.json()["balance"] == 0


def test_overdue_payment_requires_and_records_acknowledgement(client: TestClient):
    headers = _auth_headers(client)
    order = _create_test_separated(client, headers, due_days=-2)
    payment = {
        "method": "cash",
        "amount": 5000,
        "note": "Cliente decidió continuar fuera del plazo",
    }

    rejected = client.post(
        f"/separated-orders/{order['id']}/payments",
        json=payment,
        headers=headers,
    )
    assert rejected.status_code == 400
    assert "vencido" in rejected.json()["detail"].lower()

    accepted = client.post(
        f"/separated-orders/{order['id']}/payments",
        json={**payment, "expired_acknowledged": True},
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text
    data = accepted.json()
    assert data["balance"] == 35000
    event = data["resolution_history"][-1]
    assert event["action"] == "overdue_payment_acknowledged"
    assert event["amount"] == 5000
    assert event["days_overdue"] >= 1
    assert event["created_by_user_id"] is not None
