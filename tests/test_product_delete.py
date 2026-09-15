from sqlalchemy.exc import IntegrityError

import models
import routers.products as products_router
from tests.conftest import TestingSessionLocal


def _auth_headers(client):
    response = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _seed_product() -> int:
    db = TestingSessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").one()
        product = models.Product(
            tenant_id=tenant.id,
            sku="DELETE-FK-TEST",
            name="Producto con historial",
            price=100.0,
            cost=50.0,
            active=True,
            service=False,
            includes_tax=False,
        )
        db.add(product)
        db.commit()
        return product.id
    finally:
        db.close()


def test_delete_product_with_history_returns_clear_conflict_and_keeps_product(client, monkeypatch):
    product_id = _seed_product()

    def delete_with_foreign_key_error(db, product):
        raise IntegrityError("DELETE FROM products", {}, Exception("foreign key constraint"))

    monkeypatch.setattr(products_router.crud, "delete_product", delete_with_foreign_key_error)

    response = client.delete(f"/products/{product_id}", headers=_auth_headers(client))

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "No se puede eliminar este producto porque tiene movimientos o "
        "registros históricos asociados. Desactívalo para conservar el historial."
    )

    db = TestingSessionLocal()
    try:
        assert db.get(models.Product, product_id) is not None
        assert db.query(models.ProductAuditLog).filter_by(product_id=product_id, action="delete").count() == 0
    finally:
        db.close()


def test_internal_notes_are_saved_and_audited(client):
    headers = _auth_headers(client)
    created = client.post(
        "/products/",
        headers=headers,
        json={
            "name": "Producto con nota interna",
            "price": 100.0,
            "cost": 50.0,
            "internal_notes": "  Motivo inicial de uso interno.  ",
        },
    )

    assert created.status_code == 200
    product_id = created.json()["id"]
    assert created.json()["internal_notes"] == "Motivo inicial de uso interno."

    updated = client.put(
        f"/products/{product_id}",
        headers=headers,
        json={"internal_notes": "Desactivado por cambio de proveedor."},
    )

    assert updated.status_code == 200
    assert updated.json()["internal_notes"] == "Desactivado por cambio de proveedor."

    history = client.get(f"/products/{product_id}/audit", headers=headers)
    assert history.status_code == 200
    update_entry = next(entry for entry in history.json() if entry["action"] == "update")
    assert update_entry["changes"]["internal_notes"] == {
        "before": "Motivo inicial de uso interno.",
        "after": "Desactivado por cambio de proveedor.",
    }
