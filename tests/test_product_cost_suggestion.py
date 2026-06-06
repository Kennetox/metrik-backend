from datetime import datetime
import math

import crud
import models
from tests.conftest import TestingSessionLocal


def _create_tenant_and_products():
    db = TestingSessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").first()
        if tenant is None:
            tenant = models.Tenant(
                slug="kensar",
                name="Kensar",
                is_active=True,
                lifecycle_stage="active",
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

        rows = [
            models.Product(
                tenant_id=tenant.id,
                sku="A-001",
                name="Producto 1",
                price=200.0,
                cost=100.0,
                barcode="1001",
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
                group_name="Cables",
                brand="Marca X",
                supplier="Proveedor Y",
                updated_at=datetime.utcnow(),
            ),
            models.Product(
                tenant_id=tenant.id,
                sku="A-002",
                name="Producto 2",
                price=200.0,
                cost=120.0,
                barcode="1002",
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
                group_name="Cables",
                brand="Marca X",
                supplier="Proveedor Y",
                updated_at=datetime.utcnow(),
            ),
            models.Product(
                tenant_id=tenant.id,
                sku="A-003",
                name="Producto 3",
                price=200.0,
                cost=150.0,
                barcode="1003",
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
                group_name="Cables",
                brand="Marca X",
                supplier="Proveedor Y",
                updated_at=datetime.utcnow(),
            ),
        ]
        db.add_all(rows)
        db.commit()
        return tenant.id
    finally:
        db.close()


def test_cost_suggestion_modes_shift_the_result():
    tenant_id = _create_tenant_and_products()

    db = TestingSessionLocal()
    try:
        balanced = crud.suggest_product_cost(
            db,
            tenant_id=tenant_id,
            price=200.0,
            group_name="Cables",
            brand="Marca X",
            supplier="Proveedor Y",
            mode="balanced",
        )
        conservative = crud.suggest_product_cost(
            db,
            tenant_id=tenant_id,
            price=200.0,
            group_name="Cables",
            brand="Marca X",
            supplier="Proveedor Y",
            mode="conservative",
        )
        aggressive = crud.suggest_product_cost(
            db,
            tenant_id=tenant_id,
            price=200.0,
            group_name="Cables",
            brand="Marca X",
            supplier="Proveedor Y",
            mode="aggressive",
        )

        assert balanced.mode == "balanced"
        assert balanced.mode_label == "balanceado"
        assert balanced.selected_markup_percent == balanced.markup_p50

        assert conservative.mode == "conservative"
        assert conservative.selected_markup_percent == conservative.markup_p25
        assert aggressive.mode == "aggressive"
        assert aggressive.selected_markup_percent == aggressive.markup_p75

        assert conservative.suggested_cost >= balanced.suggested_cost >= aggressive.suggested_cost
        assert "Modo" in balanced.notes
    finally:
        db.close()


def test_cost_suggestion_prefers_same_product_history():
    db = TestingSessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.slug == "kensar").first()
        if tenant is None:
            tenant = models.Tenant(
                slug="kensar",
                name="Kensar",
                is_active=True,
                lifecycle_stage="active",
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

        product = models.Product(
            tenant_id=tenant.id,
            sku="H-001",
            name="Producto con historia",
            price=260.0,
            cost=130.0,
            barcode="2001",
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
            group_name="Cables",
            brand="Marca Z",
            supplier="Proveedor Z",
            updated_at=datetime.utcnow(),
        )
        db.add(product)
        db.commit()
        db.refresh(product)

        crud.create_product_audit_log(
            db,
            product_id=product.id,
            action="create",
            changes={
                "after": {
                    "id": product.id,
                    "sku": product.sku,
                    "name": product.name,
                    "price": 200.0,
                    "cost": 100.0,
                    "group_name": product.group_name,
                    "brand": product.brand,
                    "supplier": product.supplier,
                }
            },
        )
        crud.create_product_audit_log(
            db,
            product_id=product.id,
            action="update",
            changes={
                "price": {"before": 200.0, "after": 210.0},
                "cost": {"before": 100.0, "after": 105.0},
            },
        )
        crud.create_product_audit_log(
            db,
            product_id=product.id,
            action="update",
            changes={
                "price": {"before": 210.0, "after": 220.0},
                "cost": {"before": 105.0, "after": 110.0},
            },
        )

        db.add_all(
            [
                models.Product(
                    tenant_id=tenant.id,
                    sku="G-001",
                    name="Producto grupo 1",
                    price=200.0,
                    cost=160.0,
                    barcode="3001",
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
                    group_name="Cables",
                    brand="Marca Z",
                    supplier="Proveedor Z",
                    updated_at=datetime.utcnow(),
                ),
                models.Product(
                    tenant_id=tenant.id,
                    sku="G-002",
                    name="Producto grupo 2",
                    price=200.0,
                    cost=150.0,
                    barcode="3002",
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
                    group_name="Cables",
                    brand="Marca Z",
                    supplier="Proveedor Z",
                    updated_at=datetime.utcnow(),
                ),
            ]
        )
        db.commit()

        suggestion = crud.suggest_product_cost(
            db,
            tenant_id=tenant.id,
            price=200.0,
            group_name="Cables",
            brand="Marca Z",
            supplier="Proveedor Z",
            exclude_product_id=product.id,
            mode="balanced",
        )

        assert suggestion.method == "self_history"
        assert suggestion.method_label == "historial del producto"
        assert suggestion.suggested_cost == 100.0
        assert math.isclose(suggestion.selected_markup_percent, 100.0, abs_tol=0.01)
        assert suggestion.confidence_label in {"media", "alta"}
        assert "historial" in (suggestion.notes or "").lower()
    finally:
        db.close()
