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


def _seed_product() -> int:
    db = TestingSessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").first()
        assert tenant is not None
        product = models.Product(
            tenant_id=tenant.id,
            sku="CAB-12",
            name="Cabina acústica 12V",
            price=240.0,
            cost=120.0,
            barcode="8901234567890",
            label_format="Kensar1",
            unit="UND",
            stock_min=0,
            active=True,
            service=False,
            includes_tax=False,
            is_investment=False,
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
            allow_price_change=False,
            group_name="Cabinas/Acústicas",
            brand="Elektra",
            supplier="Proveedor Uno",
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product.id
    finally:
        db.close()


def test_duplicate_candidates_detect_near_name_and_catalog_match(client: TestClient):
    headers = _auth_headers(client)
    product_id = _seed_product()

    resp = client.post(
        "/products/duplicate-candidates",
        json={
            "name": "Cabina 12 voltios",
            "sku": "CAB12",
            "barcode": "",
            "group_name": "Cabinas Acusticas",
            "brand": "Elektra",
            "supplier": "Proveedor Uno",
            "limit": 6,
        },
        headers=headers,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_high_risk"] is True
    assert len(data["candidates"]) > 0
    top = data["candidates"][0]
    assert top["product_id"] == product_id
    assert top["similarity_score"] >= 0.7
    assert top["price"] == 240.0
    assert "Mismo grupo." in top["match_reasons"] or "Alta similitud textual en el nombre." in top["match_reasons"]


def test_duplicate_candidates_detect_same_family_with_different_commercial_name(client: TestClient):
    headers = _auth_headers(client)
    db = TestingSessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").first()
        assert tenant is not None
        product = models.Product(
            tenant_id=tenant.id,
            sku="LUX-90",
            name="Luminaria industrial 90W",
            price=310.0,
            cost=185.0,
            barcode="8899001122334",
            label_format="Kensar1",
            unit="UND",
            stock_min=0,
            active=True,
            service=False,
            includes_tax=False,
            is_investment=False,
            preferred_qty=0,
            reorder_point=0,
            low_stock_alert=False,
            allow_price_change=False,
            group_name="Iluminacion/Industrial",
            brand="NovaTech",
            supplier="Distribuidor Central",
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        product_id = product.id
    finally:
        db.close()

    resp = client.post(
        "/products/duplicate-candidates",
        json={
            "name": "Reflector de alta potencia",
            "sku": "",
            "barcode": "",
            "group_name": "Iluminacion Industrial",
            "brand": "NovaTech",
            "supplier": "Distribuidor Central",
            "limit": 6,
        },
        headers=headers,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candidates"]) > 0
    top = data["candidates"][0]
    assert top["product_id"] == product_id
    assert top["similarity_score"] >= 0.34
    assert top["price"] == 310.0
    assert "Coincidencia de familia comercial." in top["match_reasons"] or "Mismo grupo." in top["match_reasons"]
