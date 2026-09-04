from datetime import datetime, timedelta

from sqlalchemy import event

import models
from services.document_search import _summarize_item_rows, search_documents
from tests.conftest import TestingSessionLocal, engine


TENANT_ID = 909_771
START = datetime(2035, 1, 1, 0, 0, 0)


def _search(db, **overrides):
    params = {
        "tenant_id": TENANT_ID,
        "document_type": "all",
        "date_from": START,
        "date_to": START + timedelta(days=2),
        "term": None,
        "payment_method": None,
        "customer": None,
        "pos": None,
        "vendor": None,
        "skip": 0,
        "limit": 50,
    }
    params.update(overrides)
    return search_documents(db, **params)


def _clear_fixture_rows(db):
    db.query(models.SeparatedOrderPayment).filter(
        models.SeparatedOrderPayment.tenant_id == TENANT_ID
    ).delete(synchronize_session=False)
    db.query(models.SeparatedOrder).filter(
        models.SeparatedOrder.tenant_id == TENANT_ID
    ).delete(synchronize_session=False)
    db.query(models.SalePayment).filter(
        models.SalePayment.tenant_id == TENANT_ID
    ).delete(synchronize_session=False)
    db.query(models.SaleItem).filter(
        models.SaleItem.tenant_id == TENANT_ID
    ).delete(synchronize_session=False)
    db.query(models.Sale).filter(models.Sale.tenant_id == TENANT_ID).delete(
        synchronize_session=False
    )
    db.commit()


def test_product_preview_is_compact_and_groups_repeated_lines():
    assert _summarize_item_rows(
        [
            ("Campana mediana", 1),
            ("Campana mediana", 1),
            ("Baqueta", 1),
            ("Correa", 1),
        ]
    ) == "Campana mediana ×2 · Baqueta ×1 · +1 más"


def test_search_documents_merges_sources_and_filters_abonos():
    db = TestingSessionLocal()
    try:
        _clear_fixture_rows(db)
        sale = models.Sale(
            tenant_id=TENANT_ID,
            created_at=START + timedelta(hours=2),
            sale_number=700001,
            document_number="V-TEST-700001",
            main_payment_method="cash",
            payment_method="cash",
            total=180_000,
            customer_name="Cliente prueba documentos",
            pos_name="POS Pruebas",
            vendor_name="Vendedor Pruebas",
        )
        db.add(sale)
        db.flush()
        db.add(models.SaleItem(
            tenant_id=TENANT_ID,
            sale_id=sale.id,
            product_id=999_001,
            product_sku="BUS-001",
            product_name="Producto encontrable",
            quantity=1,
            unit_price=180_000,
            unit_price_original=180_000,
            total=180_000,
        ))
        db.add_all([
            models.SalePayment(
                tenant_id=TENANT_ID,
                sale_id=sale.id,
                method="cash",
                amount=25_000,
                is_primary=True,
            ),
            models.SalePayment(
                tenant_id=TENANT_ID,
                sale_id=sale.id,
                method="transfer",
                amount=25_000,
            ),
        ])
        separated = models.SeparatedOrder(
            tenant_id=TENANT_ID,
            sale_id=sale.id,
            customer_name=sale.customer_name,
            total_amount=180_000,
            initial_payment=50_000,
            balance=90_000,
            sale_document_number=sale.document_number,
            created_at=START + timedelta(hours=2),
        )
        db.add(separated)
        db.flush()
        db.add(models.SeparatedOrderPayment(
            tenant_id=TENANT_ID,
            separated_order_id=separated.id,
            method="transfer",
            amount=40_000,
            paid_at=START + timedelta(hours=3),
            status="active",
        ))
        db.commit()

        all_items, _ = _search(db, term="encontrable")
        assert [item["id"] for item in all_items] == [f"sale-{sale.id}"]
        assert all_items[0]["content_summary"] == "Producto encontrable ×1"

        payments, has_more = _search(db, document_type="abono")
        assert has_more is False
        assert [item["payment_stage"] for item in payments] == ["posterior", "initial"]
        assert payments[0]["payment_method"] == "transfer"
        assert payments[0]["content_summary"] == "Venta: Producto encontrable ×1"

        separated_sales, _ = _search(
            db, document_type="venta", payment_method="separated"
        )
        assert [item["id"] for item in separated_sales] == [f"sale-{sale.id}"]

        mixed_sales, _ = _search(
            db, document_type="venta", payment_method="mixed"
        )
        assert [item["id"] for item in mixed_sales] == [f"sale-{sale.id}"]
    finally:
        _clear_fixture_rows(db)
        db.close()


def test_search_documents_is_paginated_and_query_count_is_bounded():
    db = TestingSessionLocal()
    statements = 0

    def count_selects(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal statements
        if statement.lstrip().upper().startswith("SELECT"):
            statements += 1

    try:
        _clear_fixture_rows(db)
        db.add_all([
            models.Sale(
                tenant_id=TENANT_ID,
                created_at=START + timedelta(minutes=index),
                sale_number=710000 + index,
                document_number=f"V-PAGE-{index:03d}",
                main_payment_method="cash",
                payment_method="cash",
                total=index,
            )
            for index in range(75)
        ])
        db.commit()
        event.listen(engine, "before_cursor_execute", count_selects)

        first, first_has_more = _search(db, document_type="venta", limit=50)
        second, second_has_more = _search(
            db, document_type="venta", skip=50, limit=50
        )

        assert len(first) == 50
        assert first_has_more is True
        assert len(second) == 25
        assert second_has_more is False
        assert set(item["id"] for item in first).isdisjoint(
            item["id"] for item in second
        )
        # Two source queries plus one batched product-preview query per page;
        # the amount does not grow with the number of matching documents.
        assert statements <= 6
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
        _clear_fixture_rows(db)
        db.close()


def test_document_search_endpoint_requires_session_and_returns_page(client):
    unauthenticated = client.get("/documents/search")
    assert unauthenticated.status_code == 401

    login = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert login.status_code == 200
    response = client.get(
        "/documents/search",
        params={
            "type": "venta",
            "date_from": START.isoformat(),
            "date_to": (START + timedelta(days=2)).isoformat(),
            "limit": 25,
        },
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "skip": 0,
        "limit": 25,
        "has_more": False,
    }
