from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import crud
import db_migrations
import models
import schemas
from tests.conftest import TestingSessionLocal


def _default_tenant_id(db) -> int:
    tenant_id = crud.get_default_tenant_id(db)
    assert tenant_id is not None
    return int(tenant_id)


def _create_sale(db, *, tenant_id: int) -> models.Sale:
    sale = models.Sale(
        tenant_id=tenant_id,
        total=150000.0,
        paid_amount=150000.0,
        change_amount=0.0,
        main_payment_method="cash",
        payment_method="cash",
        pos_name="POS Web",
        sale_number=uuid4().int % 10**9,
        document_number=f"V-LOY-{uuid4().hex[:10]}",
        status="active",
    )
    db.add(sale)
    db.flush()
    return sale


def _create_discount_code(db, *, tenant_id: int, code: str | None = None) -> models.WebDiscountCode:
    row = models.WebDiscountCode(
        tenant_id=tenant_id,
        code=code or f"KSR-{uuid4().hex[:8].upper()}",
        discount_type="fixed_amount",
        discount_value=10000.0,
        discount_percent=0.0,
        minimum_purchase=100000.0,
        source_type=crud.LOYALTY_REWARD_DISCOUNT_SOURCE_TYPE,
        is_active=True,
        max_uses=1,
        uses_count=0,
    )
    db.add(row)
    db.flush()
    return row


def _create_product(db, *, tenant_id: int, price: float) -> models.Product:
    unique = uuid4().hex[:12]
    product = models.Product(
        tenant_id=tenant_id,
        name=f"Producto loyalty {unique}",
        sku=f"LOY-{unique}",
        barcode=f"LOY-{unique}",
        price=price,
        cost=max(0.0, price / 2),
        unit="UND",
        active=True,
        service=False,
    )
    db.add(product)
    db.flush()
    return product


def _create_web_product(db, *, tenant_id: int, price: float) -> models.Product:
    product = _create_product(db, tenant_id=tenant_id, price=price)
    product.service = True
    product.web_published = True
    product.web_visible_when_out_of_stock = True
    product.includes_tax = False
    db.flush()
    return product


def _sale_create_payload(product: models.Product, *, amount: float) -> schemas.SaleCreate:
    return schemas.SaleCreate(
        client_request_id=f"loyalty-{uuid4().hex}",
        payment_method="cash",
        total=amount,
        paid_amount=amount,
        change_amount=0.0,
        pos_name="POS Web",
        vendor_name="Prueba loyalty",
        items=[
            schemas.SaleItemCreate(
                product_id=product.id,
                quantity=1,
                unit_price=amount,
                product_sku=product.sku,
                product_name=product.name,
                product_barcode=product.barcode,
                discount=0.0,
            )
        ],
        payments=[schemas.SalePaymentCreate(method="cash", amount=amount)],
    )


def _api_sale_payload(product: models.Product, *, amount: float) -> dict:
    return {
        "client_request_id": f"loyalty-{uuid4().hex}",
        "payment_method": "cash",
        "total": amount,
        "paid_amount": amount,
        "change_amount": 0.0,
        "pos_name": "POS Web",
        "vendor_name": "Prueba loyalty",
        "items": [
            {
                "product_id": product.id,
                "quantity": 1,
                "unit_price": amount,
                "product_sku": product.sku,
                "product_name": product.name,
                "product_barcode": product.barcode,
                "discount": 0.0,
            }
        ],
        "payments": [{"method": "cash", "amount": amount}],
    }


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_reward(
    db,
    *,
    tenant_id: int,
    sale_id: int,
    token_hash: str | None = None,
    discount_code_id: int | None = None,
    reward_amount: float = 10000.0,
) -> models.LoyaltyReward:
    raw_token = uuid4().hex
    reward = models.LoyaltyReward(
        tenant_id=tenant_id,
        sale_id=sale_id,
        token_hash=token_hash or crud.hash_loyalty_reward_token(raw_token),
        token_encrypted=crud.encrypt_loyalty_reward_token(raw_token),
        discount_code_id=discount_code_id,
        reward_amount=reward_amount,
        minimum_purchase=100000.0,
        issued_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=30),
        status=crud.LOYALTY_REWARD_STATUS_ISSUED,
    )
    db.add(reward)
    db.flush()
    reward._raw_token = raw_token
    return reward


def test_loyalty_reward_rules_seed_universal_v1_rule():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        rows = (
            db.query(models.LoyaltyRewardRule)
            .filter(models.LoyaltyRewardRule.tenant_id == tenant_id)
            .order_by(models.LoyaltyRewardRule.sort_order.asc())
            .all()
        )

        assert [(row.min_purchase, row.max_purchase, row.reward_amount, row.minimum_purchase, row.validity_days, row.is_active) for row in rows] == [
            (0.0, None, 100000.0, 0.0, 30, True),
        ]
    finally:
        db.close()


def test_loyalty_reward_rules_seed_adds_universal_rule_for_existing_old_rules():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug=f"loyalty-old-rules-{uuid4().hex[:8]}",
            name="Tenant loyalty reglas antiguas",
            is_active=True,
        )
        db.add(tenant)
        db.flush()
        old_rule = models.LoyaltyRewardRule(
            tenant_id=tenant.id,
            min_purchase=100000.0,
            max_purchase=249999.0,
            reward_amount=10000.0,
            minimum_purchase=100000.0,
            validity_days=30,
            is_active=True,
            sort_order=10,
        )
        db.add(old_rule)
        db.flush()

        db_migrations._seed_default_loyalty_reward_rules(db.connection())
        db.flush()

        universal_rule = (
            db.query(models.LoyaltyRewardRule)
            .filter(
                models.LoyaltyRewardRule.tenant_id == tenant.id,
                models.LoyaltyRewardRule.min_purchase == 0.0,
                models.LoyaltyRewardRule.max_purchase.is_(None),
            )
            .one()
        )
        db.refresh(old_rule)

        assert universal_rule.is_active is True
        assert universal_rule.reward_amount == 100000.0
        assert universal_rule.minimum_purchase == 0.0
        assert old_rule.is_active is False
    finally:
        db.rollback()
        db.close()


def test_loyalty_redemption_rules_seed_default_v1_tiers():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        rows = (
            db.query(models.LoyaltyRedemptionRule)
            .filter(models.LoyaltyRedemptionRule.tenant_id == tenant_id)
            .order_by(models.LoyaltyRedemptionRule.sort_order.asc())
            .all()
        )

        assert [(row.min_purchase, row.max_purchase, row.discount_amount) for row in rows] == [
            (50000.0, 99999.0, 5000.0),
            (100000.0, 199999.0, 10000.0),
            (200000.0, 299999.0, 20000.0),
            (300000.0, 499999.0, 30000.0),
            (500000.0, 699999.0, 50000.0),
            (700000.0, 999999.0, 70000.0),
            (1000000.0, None, 100000.0),
        ]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("purchase_amount", "expected_reward", "expected_minimum"),
    [
        (0.0, None, None),
        (3000.0, 100000.0, 0.0),
        (49999.0, 100000.0, 0.0),
        (50000.0, 100000.0, 0.0),
        (700000.0, 100000.0, 0.0),
        (2000000.0, 100000.0, 0.0),
    ],
)
def test_resolve_loyalty_reward_rule_for_purchase_uses_configured_rules(
    purchase_amount: float,
    expected_reward: float | None,
    expected_minimum: float | None,
):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)

        rule = crud.resolve_loyalty_reward_rule_for_purchase(
            db,
            tenant_id=tenant_id,
            purchase_amount=purchase_amount,
        )

        if expected_reward is None:
            assert rule is None
        else:
            assert rule is not None
            assert rule.reward_amount == expected_reward
            assert rule.minimum_purchase == expected_minimum
    finally:
        db.close()


def test_resolve_loyalty_reward_rule_is_tenant_scoped():
    db = TestingSessionLocal()
    try:
        other_tenant = models.Tenant(
            slug=f"loyalty-{uuid4().hex[:8]}",
            name="Tenant loyalty aislado",
            is_active=True,
        )
        db.add(other_tenant)
        db.flush()

        assert (
            crud.resolve_loyalty_reward_rule_for_purchase(
                db,
                tenant_id=other_tenant.id,
                purchase_amount=2000000.0,
            )
            is None
        )

        custom_rule = models.LoyaltyRewardRule(
            tenant_id=other_tenant.id,
            min_purchase=1000.0,
            max_purchase=None,
            reward_amount=123.0,
            minimum_purchase=456.0,
            validity_days=15,
            is_active=True,
            sort_order=1,
        )
        db.add(custom_rule)
        db.flush()

        rule = crud.resolve_loyalty_reward_rule_for_purchase(
            db,
            tenant_id=other_tenant.id,
            purchase_amount=2000000.0,
        )

        assert rule is not None
        assert rule.id == custom_rule.id
        assert rule.reward_amount == 123.0
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize(
    ("reward_amount", "purchase_amount", "expected_discount"),
    [
        (30000.0, 3000.0, 0.0),
        (30000.0, 49999.0, 0.0),
        (30000.0, 50000.0, 5000.0),
        (30000.0, 99999.0, 5000.0),
        (30000.0, 100000.0, 10000.0),
        (30000.0, 199999.0, 10000.0),
        (30000.0, 200000.0, 20000.0),
        (30000.0, 299999.0, 20000.0),
        (30000.0, 300000.0, 30000.0),
        (30000.0, 700000.0, 70000.0),
        (100000.0, 100000.0, 10000.0),
        (100000.0, 200000.0, 20000.0),
        (100000.0, 300000.0, 30000.0),
        (100000.0, 500000.0, 50000.0),
        (100000.0, 700000.0, 70000.0),
        (100000.0, 1000000.0, 100000.0),
        (100000.0, 2000000.0, 100000.0),
    ],
)
def test_compute_loyalty_effective_discount_uses_redemption_tiers(
    reward_amount: float,
    purchase_amount: float,
    expected_discount: float,
):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=sale.id,
            reward_amount=reward_amount,
        )

        discount, rule, message = crud.compute_loyalty_effective_discount_amount(
            db,
            tenant_id=tenant_id,
            reward=reward,
            purchase_amount=purchase_amount,
        )

        assert discount == expected_discount
        if expected_discount == 0:
            assert rule is None
            assert "desde $50.000" in message
        else:
            assert rule is not None
    finally:
        db.rollback()
        db.close()


def test_loyalty_redemption_discount_is_independent_from_origin_sale_amount():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        low_origin = _create_sale(db, tenant_id=tenant_id)
        low_origin.total = 3000.0
        high_origin = _create_sale(db, tenant_id=tenant_id)
        high_origin.total = 2000000.0
        low_reward = _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=low_origin.id,
            reward_amount=5000.0,
        )
        high_reward = _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=high_origin.id,
            reward_amount=100000.0,
        )

        low_discount, _, _ = crud.compute_loyalty_effective_discount_amount(
            db,
            tenant_id=tenant_id,
            reward=low_reward,
            purchase_amount=300000.0,
        )
        high_discount, _, _ = crud.compute_loyalty_effective_discount_amount(
            db,
            tenant_id=tenant_id,
            reward=high_reward,
            purchase_amount=300000.0,
        )

        assert low_discount == 30000.0
        assert high_discount == 30000.0
    finally:
        db.rollback()
        db.close()


def test_loyalty_redemption_rule_validation_rejects_overlaps():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)

        with pytest.raises(ValueError, match="solapa"):
            crud.create_loyalty_redemption_rule(
                db,
                tenant_id=tenant_id,
                payload=schemas.LoyaltyRedemptionRuleCreate(
                    min_purchase=150000.0,
                    max_purchase=250000.0,
                    discount_amount=15000.0,
                    is_active=True,
                    sort_order=99,
                ),
            )
    finally:
        db.rollback()
        db.close()


def test_loyalty_reward_constraints_keep_sale_token_and_discount_code_unique():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        sale = _create_sale(db, tenant_id=tenant_id)
        discount_code = _create_discount_code(db, tenant_id=tenant_id)
        token_hash = uuid4().hex

        _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=sale.id,
            token_hash=token_hash,
            discount_code_id=discount_code.id,
        )
        db.commit()

        with pytest.raises(IntegrityError):
            _create_reward(db, tenant_id=tenant_id, sale_id=sale.id)
        db.rollback()

        sale_for_duplicate_token = _create_sale(db, tenant_id=tenant_id)
        with pytest.raises(IntegrityError):
            _create_reward(
                db,
                tenant_id=tenant_id,
                sale_id=sale_for_duplicate_token.id,
                token_hash=token_hash,
            )
        db.rollback()

        sale_for_duplicate_code = _create_sale(db, tenant_id=tenant_id)
        with pytest.raises(IntegrityError):
            _create_reward(
                db,
                tenant_id=tenant_id,
                sale_id=sale_for_duplicate_code.id,
                discount_code_id=discount_code.id,
            )
    finally:
        db.rollback()
        db.close()


def test_loyalty_reward_tracks_scans_without_changing_main_status():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(db, tenant_id=tenant_id, sale_id=sale.id)

        first_scan = datetime.utcnow()
        second_scan = first_scan + timedelta(minutes=5)
        reward.first_scanned_at = first_scan
        reward.last_scanned_at = second_scan
        reward.scan_count = 2
        db.flush()

        assert reward.status == crud.LOYALTY_REWARD_STATUS_ISSUED
        assert reward.first_scanned_at == first_scan
        assert reward.last_scanned_at == second_scan
        assert reward.scan_count == 2
    finally:
        db.rollback()
        db.close()


def test_discount_code_crud_preserves_loyalty_metadata():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        created = crud.create_comercio_web_discount_code(
            db,
            tenant_id=tenant_id,
            payload=schemas.ComercioWebDiscountCodeCreate(
                code=f"KSR-{uuid4().hex[:8]}",
                discount_type="fixed_amount",
                discount_value=10000.0,
                minimum_purchase=100000.0,
                source_type="LOYALTY_REWARD",
                is_active=True,
                max_uses=1,
            ),
        )

        assert created.minimum_purchase == 100000.0
        assert created.source_type == crud.LOYALTY_REWARD_DISCOUNT_SOURCE_TYPE

        updated = crud.update_comercio_web_discount_code(
            db,
            tenant_id=tenant_id,
            discount_code_id=created.id,
            payload=schemas.ComercioWebDiscountCodeUpdate(
                minimum_purchase=200000.0,
                source_type=None,
            ),
        )

        assert updated.minimum_purchase == 200000.0
        assert updated.source_type is None
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize(
    ("amount", "expected_reward", "expected_minimum"),
    [
        (3000.0, 100000.0, 0.0),
        (49999.0, 100000.0, 0.0),
        (50000.0, 100000.0, 0.0),
        (700000.0, 100000.0, 0.0),
        (2000000.0, 100000.0, 0.0),
    ],
)
def test_create_sale_issues_loyalty_reward_for_any_positive_pos_total(
    amount: float,
    expected_reward: float | None,
    expected_minimum: float | None,
):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        product = _create_product(db, tenant_id=tenant_id, price=amount)
        sale = crud.create_sale(
            db,
            _sale_create_payload(product, amount=amount),
            tenant_id=tenant_id,
        )

        reward = (
            db.query(models.LoyaltyReward)
            .filter(models.LoyaltyReward.sale_id == sale.id)
            .first()
        )
        assert reward is not None
        assert reward.tenant_id == tenant_id
        assert reward.reward_amount == expected_reward
        assert reward.minimum_purchase == expected_minimum
        assert reward.status == crud.LOYALTY_REWARD_STATUS_ISSUED
    finally:
        db.rollback()
        db.close()


def test_create_sale_loyalty_reward_uses_only_tenant_rules():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug=f"loyalty-sale-{uuid4().hex[:8]}",
            name="Tenant reglas venta loyalty",
            is_active=True,
        )
        db.add(tenant)
        db.flush()
        rule = models.LoyaltyRewardRule(
            tenant_id=tenant.id,
            min_purchase=100000.0,
            max_purchase=None,
            reward_amount=7777.0,
            minimum_purchase=8888.0,
            validity_days=9,
            is_active=True,
            sort_order=1,
        )
        db.add(rule)
        product = _create_product(db, tenant_id=tenant.id, price=100000.0)

        sale = crud.create_sale(
            db,
            _sale_create_payload(product, amount=100000.0),
            tenant_id=tenant.id,
        )

        reward = db.query(models.LoyaltyReward).filter_by(sale_id=sale.id).one()
        assert reward.rule_id == rule.id
        assert reward.reward_amount == 7777.0
        assert reward.minimum_purchase == 8888.0
    finally:
        db.rollback()
        db.close()


def test_create_sale_does_not_issue_reward_for_inactive_rule():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug=f"loyalty-inactive-{uuid4().hex[:8]}",
            name="Tenant regla inactiva",
            is_active=True,
        )
        db.add(tenant)
        db.flush()
        db.add(
            models.LoyaltyRewardRule(
                tenant_id=tenant.id,
                min_purchase=100000.0,
                max_purchase=None,
                reward_amount=7777.0,
                minimum_purchase=8888.0,
                validity_days=9,
                is_active=False,
                sort_order=1,
            )
        )
        product = _create_product(db, tenant_id=tenant.id, price=100000.0)

        sale = crud.create_sale(
            db,
            _sale_create_payload(product, amount=100000.0),
            tenant_id=tenant.id,
        )

        assert db.query(models.LoyaltyReward).filter_by(sale_id=sale.id).first() is None
    finally:
        db.rollback()
        db.close()


def test_create_sale_retry_does_not_create_duplicate_reward():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        payload = _sale_create_payload(product, amount=100000.0)

        first = crud.create_sale(db, payload, tenant_id=tenant_id)
        retry = crud.create_sale(db, payload, tenant_id=tenant_id)

        rewards = db.query(models.LoyaltyReward).filter_by(sale_id=first.id).all()
        assert retry.id == first.id
        assert len(rewards) == 1
    finally:
        db.rollback()
        db.close()


def test_reward_snapshot_survives_rule_changes_and_validity_days():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        rule = (
            db.query(models.LoyaltyRewardRule)
            .filter(
                models.LoyaltyRewardRule.tenant_id == tenant_id,
                models.LoyaltyRewardRule.min_purchase == 0.0,
            )
            .one()
        )
        rule.validity_days = 12
        db.flush()
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)

        sale = crud.create_sale(
            db,
            _sale_create_payload(product, amount=100000.0),
            tenant_id=tenant_id,
        )
        reward = db.query(models.LoyaltyReward).filter_by(sale_id=sale.id).one()
        issued_at = reward.issued_at

        rule.reward_amount = 99999.0
        rule.minimum_purchase = 99999.0
        rule.validity_days = 99
        db.flush()
        db.refresh(reward)

        assert reward.reward_amount == 100000.0
        assert reward.minimum_purchase == 0.0
        assert reward.expires_at == issued_at + timedelta(days=12)
    finally:
        db.rollback()
        db.close()


def test_reward_token_is_unique_and_does_not_reveal_sale_id():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        first_product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        second_product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        first_sale = crud.create_sale(
            db,
            _sale_create_payload(first_product, amount=100000.0),
            tenant_id=tenant_id,
        )
        second_sale = crud.create_sale(
            db,
            _sale_create_payload(second_product, amount=100000.0),
            tenant_id=tenant_id,
        )
        first_reward = db.query(models.LoyaltyReward).filter_by(sale_id=first_sale.id).one()
        second_reward = db.query(models.LoyaltyReward).filter_by(sale_id=second_sale.id).one()
        first_token = crud.decrypt_loyalty_reward_token(first_reward.token_encrypted)
        second_token = crud.decrypt_loyalty_reward_token(second_reward.token_encrypted)

        assert first_token
        assert second_token
        assert first_token != second_token
        assert first_reward.token_hash != second_reward.token_hash
        assert str(first_sale.id) not in first_token
        assert first_reward.token_hash == crud.hash_loyalty_reward_token(first_token)
    finally:
        db.rollback()
        db.close()


def test_reward_issue_failure_rolls_back_sale_without_orphan_reward(monkeypatch):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        request_id = f"loyalty-fail-{uuid4().hex}"

        def fail_encrypt(_token: str) -> str:
            raise RuntimeError("token encryption failed")

        monkeypatch.setattr(crud, "encrypt_loyalty_reward_token", fail_encrypt)

        with pytest.raises(RuntimeError):
            crud.create_sale(
                db,
                _sale_create_payload(product, amount=100000.0).model_copy(
                    update={"client_request_id": request_id}
                ),
                tenant_id=tenant_id,
            )
        db.rollback()

        assert (
            db.query(models.Sale)
            .filter(models.Sale.client_request_id == request_id)
            .first()
            is None
        )
        assert db.query(models.LoyaltyReward).count() >= 0
        assert (
            db.query(models.LoyaltyReward)
            .join(models.Sale, models.Sale.id == models.LoyaltyReward.sale_id)
            .filter(models.Sale.client_request_id == request_id)
            .first()
            is None
        )
    finally:
        db.rollback()
        db.close()


def test_void_sale_cancels_unredeemed_origin_reward():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        user = (
            db.query(models.PosUser)
            .filter(models.PosUser.email == "master@kensar.com")
            .one()
        )
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        sale = crud.create_sale(
            db,
            _sale_create_payload(product, amount=100000.0),
            tenant_id=tenant_id,
        )
        reward = db.query(models.LoyaltyReward).filter_by(sale_id=sale.id).one()

        crud.void_sale(db, sale, user, reason="Prueba anulacion loyalty")
        db.refresh(reward)

        assert reward.status == crud.LOYALTY_REWARD_STATUS_CANCELLED
        assert reward.cancelled_at is not None
        assert reward.cancellation_reason == "Prueba anulacion loyalty"
    finally:
        db.rollback()
        db.close()


def test_void_sale_does_not_reverse_redeemed_origin_reward():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        user = (
            db.query(models.PosUser)
            .filter(models.PosUser.email == "master@kensar.com")
            .one()
        )
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        sale = crud.create_sale(
            db,
            _sale_create_payload(product, amount=100000.0),
            tenant_id=tenant_id,
        )
        reward = db.query(models.LoyaltyReward).filter_by(sale_id=sale.id).one()
        redeemed_at = datetime.utcnow()
        reward.status = crud.LOYALTY_REWARD_STATUS_REDEEMED
        reward.redeemed_at = redeemed_at
        db.flush()

        crud.void_sale(db, sale, user, reason="Anulacion posterior")
        db.refresh(reward)

        assert reward.status == crud.LOYALTY_REWARD_STATUS_REDEEMED
        assert reward.redeemed_at == redeemed_at
        assert reward.cancelled_at is None
    finally:
        db.rollback()
        db.close()


def test_pos_sale_response_contains_reward_when_applicable(client: TestClient):
    headers = _auth_headers(client)
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        db.commit()
        product_id = product.id
        product_sku = product.sku
        product_name = product.name
        product_barcode = product.barcode
    finally:
        db.close()

    payload = {
        "client_request_id": f"loyalty-api-{uuid4().hex}",
        "payment_method": "cash",
        "total": 100000.0,
        "paid_amount": 100000.0,
        "change_amount": 0.0,
        "pos_name": "POS Web",
        "vendor_name": "Prueba loyalty",
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "unit_price": 100000.0,
                "product_sku": product_sku,
                "product_name": product_name,
                "product_barcode": product_barcode,
                "discount": 0.0,
            }
        ],
        "payments": [{"method": "cash", "amount": 100000.0}],
    }

    response = client.post("/pos/sales", json=payload, headers=headers)

    assert response.status_code == 201
    data = response.json()
    assert data["reward"]["amount"] == 100000.0
    assert data["reward"]["minimum_purchase"] == 0.0
    assert data["reward"]["expires_at"]
    assert data["reward"]["public_url"].startswith(
        "https://www.kensarelectronic.com/beneficio/"
    )


def test_pos_sale_response_contains_reward_for_sale_below_redemption_minimum(client: TestClient):
    headers = _auth_headers(client)
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        product = _create_product(db, tenant_id=tenant_id, price=99999.0)
        db.commit()
        product_id = product.id
        product_sku = product.sku
        product_name = product.name
        product_barcode = product.barcode
    finally:
        db.close()

    response = client.post(
        "/pos/sales",
        json={
            "client_request_id": f"loyalty-api-null-{uuid4().hex}",
            "payment_method": "cash",
            "total": 99999.0,
            "paid_amount": 99999.0,
            "change_amount": 0.0,
            "pos_name": "POS Web",
            "vendor_name": "Prueba loyalty",
            "items": [
                {
                    "product_id": product_id,
                    "quantity": 1,
                    "unit_price": 99999.0,
                    "product_sku": product_sku,
                    "product_name": product_name,
                    "product_barcode": product_barcode,
                    "discount": 0.0,
                }
            ],
            "payments": [{"method": "cash", "amount": 99999.0}],
        },
        headers=headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["reward"]["amount"] == 100000.0
    assert data["reward"]["minimum_purchase"] == 0.0
    assert data["reward"]["public_url"].startswith(
        "https://www.kensarelectronic.com/beneficio/"
    )


def test_public_reward_get_tracks_scan_and_hides_internal_ids(client: TestClient):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(db, tenant_id=tenant_id, sale_id=sale.id)
        raw_token = reward._raw_token
        db.commit()
    finally:
        db.close()

    response = client.get(f"/rewards/public/{raw_token}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "issued"
    assert data["amount"] == 10000.0
    assert data["redemption_options"] == [
        {"min_purchase": 50000.0, "max_purchase": 99999.0, "discount_amount": 5000.0},
        {"min_purchase": 100000.0, "max_purchase": 199999.0, "discount_amount": 10000.0},
        {"min_purchase": 200000.0, "max_purchase": 299999.0, "discount_amount": 20000.0},
        {"min_purchase": 300000.0, "max_purchase": 499999.0, "discount_amount": 30000.0},
        {"min_purchase": 500000.0, "max_purchase": 699999.0, "discount_amount": 50000.0},
        {"min_purchase": 700000.0, "max_purchase": 999999.0, "discount_amount": 70000.0},
        {"min_purchase": 1000000.0, "max_purchase": None, "discount_amount": 100000.0},
    ]
    assert "sale_id" not in data
    assert "tenant_id" not in data

    db = TestingSessionLocal()
    try:
        stored = db.query(models.LoyaltyReward).filter_by(token_hash=crud.hash_loyalty_reward_token(raw_token)).one()
        assert stored.first_scanned_at is not None
        assert stored.last_scanned_at is not None
        assert stored.scan_count == 1
    finally:
        db.close()


def test_public_reward_redemption_options_do_not_stop_at_origin_reward_amount(client: TestClient):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=sale.id,
            reward_amount=20000.0,
        )
        raw_token = reward._raw_token
        db.commit()
    finally:
        db.close()

    response = client.get(f"/rewards/public/{raw_token}")

    assert response.status_code == 200
    assert [option["discount_amount"] for option in response.json()["redemption_options"]] == [
        5000.0,
        10000.0,
        20000.0,
        30000.0,
        50000.0,
        70000.0,
        100000.0,
    ]


def test_public_activation_is_idempotent_and_creates_loyalty_code(client: TestClient):
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(db, tenant_id=tenant_id, sale_id=sale.id)
        raw_token = reward._raw_token
        db.commit()
    finally:
        db.close()

    first = client.post(f"/rewards/public/{raw_token}/activate")
    second = client.post(f"/rewards/public/{raw_token}/activate")

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()
    second_data = second.json()
    assert first_data["status"] == "activated"
    assert len(first_data["code"]) == 7
    assert "-" not in first_data["code"]
    assert first_data["code"].isalnum()
    assert second_data["code"] == first_data["code"]

    db = TestingSessionLocal()
    try:
        codes = (
            db.query(models.WebDiscountCode)
            .filter(models.WebDiscountCode.source_type == crud.LOYALTY_REWARD_DISCOUNT_SOURCE_TYPE)
            .all()
        )
        assert len([code for code in codes if code.code == first_data["code"]]) == 1
        code = next(code for code in codes if code.code == first_data["code"])
        assert code.discount_type == "fixed_amount"
        assert code.discount_value == 100000.0
        assert code.minimum_purchase == 0.0
        assert code.max_uses == 1
        assert code.uses_count == 0
        assert code.ends_at is not None
    finally:
        db.close()


def test_legacy_loyalty_code_lookup_accepts_omitted_hyphen():
    assert crud._discount_code_lookup_candidates("KSR-ABCD1234") == ["KSR-ABCD1234"]
    assert crud._discount_code_lookup_candidates("ksrabcd1234") == [
        "KSRABCD1234",
        "KSR-ABCD1234",
    ]


def test_pos_loyalty_code_validation_and_atomic_redemption(client: TestClient):
    headers = _auth_headers(client)
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        origin_sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(db, tenant_id=tenant_id, sale_id=origin_sale.id)
        raw_token = reward._raw_token
        product = _create_product(db, tenant_id=tenant_id, price=300000.0)
        db.commit()
        product_payload = {
            "product_id": product.id,
            "quantity": 1,
            "unit_price": 300000.0,
            "product_sku": product.sku,
            "product_name": product.name,
            "product_barcode": product.barcode,
            "discount": 0.0,
        }
    finally:
        db.close()

    activated = client.post(f"/rewards/public/{raw_token}/activate")
    code = activated.json()["code"]
    assert len(code) == 7
    assert "-" not in code
    assert code.isalnum()

    preview = client.post(
        "/pos/discount-codes/validate",
        headers=headers,
        json={"code": code, "purchase_amount": 300000.0},
    )
    assert preview.status_code == 200
    assert preview.json()["discount_amount"] == 30000.0

    sale_response = client.post(
        "/pos/sales",
        headers=headers,
        json={
            "client_request_id": f"redeem-pos-{uuid4().hex}",
            "payment_method": "cash",
            "total": 270000.0,
            "paid_amount": 270000.0,
            "change_amount": 0.0,
            "pos_name": "POS Web",
            "vendor_name": "Prueba loyalty",
            "loyalty_discount_code": code,
            "items": [product_payload],
            "payments": [{"method": "cash", "amount": 270000.0}],
        },
    )
    assert sale_response.status_code == 201
    assert sale_response.json()["loyalty_discount_amount"] == 30000.0
    assert sale_response.json()["total"] == 270000.0

    reused = client.post(
        "/pos/sales",
        headers=headers,
        json={
            "client_request_id": f"redeem-pos-again-{uuid4().hex}",
            "payment_method": "cash",
            "total": 270000.0,
            "paid_amount": 270000.0,
            "change_amount": 0.0,
            "pos_name": "POS Web",
            "vendor_name": "Prueba loyalty",
            "loyalty_discount_code": code,
            "items": [product_payload],
            "payments": [{"method": "cash", "amount": 270000.0}],
        },
    )
    assert reused.status_code == 400

    db = TestingSessionLocal()
    try:
        discount_code = db.query(models.WebDiscountCode).filter_by(code=code).one()
        reward = db.query(models.LoyaltyReward).filter_by(discount_code_id=discount_code.id).one()
        redemptions = db.query(models.DiscountCodeRedemption).filter_by(discount_code_id=discount_code.id).all()
        assert discount_code.uses_count == 1
        assert reward.status == crud.LOYALTY_REWARD_STATUS_REDEEMED
        assert reward.redeemed_sale_id == sale_response.json()["id"]
        assert len(redemptions) == 1
    finally:
        db.close()


def test_loyalty_reward_partial_effective_discount_is_single_use(client: TestClient):
    headers = _auth_headers(client)
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        origin_sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=origin_sale.id,
            reward_amount=30000.0,
        )
        raw_token = reward._raw_token
        product = _create_product(db, tenant_id=tenant_id, price=100000.0)
        db.commit()
        product_payload = {
            "product_id": product.id,
            "quantity": 1,
            "unit_price": 100000.0,
            "product_sku": product.sku,
            "product_name": product.name,
            "product_barcode": product.barcode,
            "discount": 0.0,
        }
    finally:
        db.close()

    code = client.post(f"/rewards/public/{raw_token}/activate").json()["code"]

    under_minimum = client.post(
        "/pos/discount-codes/validate",
        headers=headers,
        json={"code": code, "purchase_amount": 99999.0},
    )
    assert under_minimum.status_code == 200
    assert under_minimum.json()["discount_amount"] == 5000.0

    too_low = client.post(
        "/pos/discount-codes/validate",
        headers=headers,
        json={"code": code, "purchase_amount": 49999.0},
    )
    assert too_low.status_code == 200
    assert too_low.json()["discount_amount"] == 0.0
    assert "desde $50.000" in too_low.json()["message"]

    db = TestingSessionLocal()
    try:
        stored_code = db.query(models.WebDiscountCode).filter_by(code=code).one()
        stored_reward = db.query(models.LoyaltyReward).filter_by(discount_code_id=stored_code.id).one()
        assert stored_code.uses_count == 0
        assert stored_reward.status == crud.LOYALTY_REWARD_STATUS_ACTIVATED
    finally:
        db.close()

    preview = client.post(
        "/pos/discount-codes/validate",
        headers=headers,
        json={"code": code, "purchase_amount": 100000.0},
    )
    assert preview.status_code == 200
    assert preview.json()["reward_max_amount"] == 30000.0
    assert preview.json()["effective_discount_amount"] == 10000.0

    sale_payload = {
        "client_request_id": f"redeem-partial-{uuid4().hex}",
        "payment_method": "cash",
        "total": 90000.0,
        "paid_amount": 90000.0,
        "change_amount": 0.0,
        "pos_name": "POS Web",
        "vendor_name": "Prueba loyalty",
        "loyalty_discount_code": code,
        "items": [product_payload],
        "payments": [{"method": "cash", "amount": 90000.0}],
    }
    sale_response = client.post(
        "/pos/sales",
        headers=headers,
        json=sale_payload,
    )
    assert sale_response.status_code == 201
    assert sale_response.json()["loyalty_discount_amount"] == 10000.0

    retry = client.post("/pos/sales", headers=headers, json=sale_payload)
    assert retry.status_code == 201
    assert retry.json()["id"] == sale_response.json()["id"]

    reused = client.post(
        "/pos/discount-codes/validate",
        headers=headers,
        json={"code": code, "purchase_amount": 300000.0},
    )
    assert reused.status_code == 400

    db = TestingSessionLocal()
    try:
        stored_code = db.query(models.WebDiscountCode).filter_by(code=code).one()
        stored_reward = db.query(models.LoyaltyReward).filter_by(discount_code_id=stored_code.id).one()
        redemption = db.query(models.DiscountCodeRedemption).filter_by(discount_code_id=stored_code.id).one()
        assert stored_code.uses_count == 1
        assert stored_reward.status == crud.LOYALTY_REWARD_STATUS_REDEEMED
        assert redemption.discount_amount == 10000.0
    finally:
        db.close()


def test_loyalty_metrics_separate_reward_max_from_effective_discount():
    db = TestingSessionLocal()
    try:
        tenant = models.Tenant(
            slug=f"loyalty-metrics-{uuid4().hex[:8]}",
            name="Tenant métricas loyalty",
            is_active=True,
        )
        db.add(tenant)
        db.flush()
        db.add(
            models.LoyaltyRedemptionRule(
                tenant_id=tenant.id,
                min_purchase=100000.0,
                max_purchase=199999.0,
                discount_amount=10000.0,
                is_active=True,
                sort_order=10,
            )
        )
        origin_sale = _create_sale(db, tenant_id=tenant.id)
        reward = _create_reward(
            db,
            tenant_id=tenant.id,
            sale_id=origin_sale.id,
            reward_amount=30000.0,
        )
        activated = crud.activate_public_loyalty_reward(db, reward._raw_token)
        product = _create_product(db, tenant_id=tenant.id, price=100000.0)
        sale_payload = _sale_create_payload(product, amount=100000.0).model_copy(
            update={
                "client_request_id": f"metrics-redeem-{uuid4().hex}",
                "total": 90000.0,
                "paid_amount": 90000.0,
                "loyalty_discount_code": activated.code,
                "payments": [schemas.SalePaymentCreate(method="cash", amount=90000.0)],
            }
        )

        crud.create_sale(db, sale_payload, tenant_id=tenant.id)
        metrics = crud.get_loyalty_reward_metrics(db, tenant_id=tenant.id)

        assert metrics.issued_amount_total == 30000.0
        assert metrics.redeemed_amount_total == 30000.0
        assert metrics.redeemed_discount_amount_total == 10000.0
    finally:
        db.rollback()
        db.close()


def test_web_order_payment_redeems_loyalty_code_atomically():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        origin_sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(
            db,
            tenant_id=tenant_id,
            sale_id=origin_sale.id,
            reward_amount=30000.0,
        )
        raw_token = reward._raw_token

        activated = crud.activate_public_loyalty_reward(db, raw_token)
        code = activated.code
        assert code
        pos_validation = crud.validate_discount_code_for_purchase(
            db,
            tenant_id=tenant_id,
            code=code,
            purchase_amount=200000.0,
        )
        assert pos_validation.effective_discount_amount == 20000.0

        account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant_id)
        product = _create_web_product(db, tenant_id=tenant_id, price=200000.0)
        crud.add_item_to_web_cart(
            db,
            account,
            schemas.WebCartItemMutationRequest(product_id=product.id, quantity=1),
        )
        cart = crud.apply_coupon_to_web_cart(db, account, code)
        assert cart.discount_amount == 20000.0

        order_read = crud.create_web_order_from_cart(
            db,
            account,
            schemas.WebOrderCreateFromCartRequest(),
        )
        order = crud.get_web_order(db, order_read.id, account.id, tenant_id=tenant_id)
        assert order is not None

        paid = crud.record_web_order_payment(
            db,
            order,
            schemas.WebOrderPaymentRecordRequest(
                method="manual",
                amount=order_read.total,
                provider="test",
                provider_reference=f"loyalty-web-{uuid4().hex}",
                status="approved",
            ),
        )
        assert paid.payment_status == "approved"

        discount_code = db.query(models.WebDiscountCode).filter_by(code=code).one()
        db.refresh(reward)
        redemptions = db.query(models.DiscountCodeRedemption).filter_by(discount_code_id=discount_code.id).all()

        assert discount_code.uses_count == 1
        assert reward.status == crud.LOYALTY_REWARD_STATUS_REDEEMED
        assert reward.redeemed_order_id == order_read.id
        assert len(redemptions) == 1
        assert redemptions[0].web_order_id == order_read.id
        assert redemptions[0].discount_amount == 20000.0

        second_account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant_id)
        second_product = _create_web_product(db, tenant_id=tenant_id, price=200000.0)
        crud.add_item_to_web_cart(
            db,
            second_account,
            schemas.WebCartItemMutationRequest(product_id=second_product.id, quantity=1),
        )
        with pytest.raises(ValueError, match="no está disponible"):
            crud.apply_coupon_to_web_cart(db, second_account, code)
    finally:
        db.rollback()
        db.close()


def test_web_order_loyalty_code_under_redemption_minimum_does_not_consume():
    db = TestingSessionLocal()
    try:
        tenant_id = _default_tenant_id(db)
        origin_sale = _create_sale(db, tenant_id=tenant_id)
        reward = _create_reward(db, tenant_id=tenant_id, sale_id=origin_sale.id)
        activated = crud.activate_public_loyalty_reward(db, reward._raw_token)
        code = activated.code
        assert code

        account = crud.get_or_create_guest_web_customer_account(db, tenant_id=tenant_id)
        crud.clear_web_cart(db, account)
        product = _create_web_product(db, tenant_id=tenant_id, price=30000.0)
        crud.add_item_to_web_cart(
            db,
            account,
            schemas.WebCartItemMutationRequest(product_id=product.id, quantity=1),
        )

        cart = crud.apply_coupon_to_web_cart(db, account, code)
        assert cart.coupon_code == code
        assert cart.discount_amount == 0.0

        order_read = crud.create_web_order_from_cart(
            db,
            account,
            schemas.WebOrderCreateFromCartRequest(),
        )
        order = crud.get_web_order(db, order_read.id, account.id, tenant_id=tenant_id)
        assert order is not None
        assert order.discount_amount == 0.0

        paid = crud.record_web_order_payment(
            db,
            order,
            schemas.WebOrderPaymentRecordRequest(
                method="manual",
                amount=order_read.total,
                provider="test",
                provider_reference=f"loyalty-web-under-{uuid4().hex}",
                status="approved",
            ),
        )
        assert paid.payment_status == "approved"

        discount_code = db.query(models.WebDiscountCode).filter_by(code=code).one()
        db.refresh(reward)
        redemptions = db.query(models.DiscountCodeRedemption).filter_by(discount_code_id=discount_code.id).all()
        assert discount_code.uses_count == 0
        assert reward.status == crud.LOYALTY_REWARD_STATUS_ACTIVATED
        assert redemptions == []
    finally:
        db.rollback()
        db.close()
