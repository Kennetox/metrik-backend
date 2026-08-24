import pytest

import crud
import models
import schemas
from tests.conftest import TestingSessionLocal


def _tenant(db, slug: str) -> models.Tenant:
    tenant = models.Tenant(
        slug=slug,
        name=slug,
        is_active=True,
        lifecycle_stage="active",
        enabled_modules=["commerce_web"],
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _product(db, tenant_id: int, sku: str) -> models.Product:
    product = models.Product(
        tenant_id=tenant_id,
        sku=sku,
        name=f"Producto {sku}",
        price=100_000,
        cost=50_000,
        label_format="Kensar1",
        active=True,
        service=False,
        includes_tax=False,
        is_investment=False,
        web_published=True,
        web_visible_when_out_of_stock=True,
        group_name="General",
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _move_stock(db, tenant_id: int, product_id: int, quantity: float) -> None:
    db.add(
        models.InventoryMovement(
            tenant_id=tenant_id,
            product_id=product_id,
            qty_delta=quantity,
            reason="purchase" if quantity > 0 else "adjustment",
            reference_type="test",
        )
    )
    db.commit()


def test_cart_rejects_quantity_above_available_stock_and_exposes_limit():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "web-stock-cart")
        product = _product(db, tenant.id, "STOCK-CART")
        _move_stock(db, tenant.id, product.id, 1)
        account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant.id)

        with pytest.raises(ValueError, match="Solo queda 1 unidad disponible"):
            crud.add_item_to_web_cart(
                db,
                account,
                schemas.WebCartItemMutationRequest(product_id=product.id, quantity=2),
            )

        cart = crud.add_item_to_web_cart(
            db,
            account,
            schemas.WebCartItemMutationRequest(product_id=product.id, quantity=1),
        )
        assert cart.items[0].quantity == 1
        assert cart.items[0].available_quantity == 1

        with pytest.raises(ValueError, match="Solo queda 1 unidad disponible"):
            crud.update_web_cart_item_quantity(db, account, product.id, 2)


def test_order_creation_revalidates_stock_after_cart_was_built():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "web-stock-checkout")
        product = _product(db, tenant.id, "STOCK-CHECKOUT")
        _move_stock(db, tenant.id, product.id, 2)
        account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant.id)
        crud.add_item_to_web_cart(
            db,
            account,
            schemas.WebCartItemMutationRequest(product_id=product.id, quantity=2),
        )

        _move_stock(db, tenant.id, product.id, -1)

        with pytest.raises(ValueError, match="Solo queda 1 unidad disponible"):
            crud.create_web_order_from_cart(
                db,
                account,
                schemas.WebOrderCreateFromCartRequest(),
            )


def test_available_quantity_uses_whole_units_and_services_are_unlimited():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "web-stock-quantity")
        product = _product(db, tenant.id, "STOCK-FLOOR")
        assert crud.resolve_web_product_available_quantity(product, 2.9) == 2
        assert crud.resolve_web_product_available_quantity(product, -3) == 0

        product.service = True
        assert crud.resolve_web_product_available_quantity(product, 0) is None


def test_pending_order_is_checked_again_before_starting_payment():
    with TestingSessionLocal() as db:
        tenant = _tenant(db, "web-stock-payment")
        product = _product(db, tenant.id, "STOCK-PAYMENT")
        _move_stock(db, tenant.id, product.id, 1)
        account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant.id)
        crud.add_item_to_web_cart(
            db,
            account,
            schemas.WebCartItemMutationRequest(product_id=product.id, quantity=1),
        )
        created = crud.create_web_order_from_cart(
            db,
            account,
            schemas.WebOrderCreateFromCartRequest(),
        )

        _move_stock(db, tenant.id, product.id, -1)
        order = crud.get_web_order(db, created.id, account.id, tenant_id=tenant.id)
        assert order is not None
        with pytest.raises(ValueError, match="0 unidades disponibles"):
            crud.validate_web_order_stock(db, order)
