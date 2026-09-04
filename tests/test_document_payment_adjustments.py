import pytest
from fastapi import HTTPException

from routers.pos import _validate_payment_adjustment_with_change


def test_rejects_reassigning_tendered_cash_and_change_to_nequi():
    with pytest.raises(HTTPException) as exc_info:
        _validate_payment_adjustment_with_change(
            adjusted_total=50_000,
            effective_sale_total=32_000,
            recorded_change=18_000,
            is_separated=False,
        )

    assert exc_info.value.status_code == 400
    assert "cambio entregado" in exc_info.value.detail


def test_accepts_reassigning_only_the_amount_applied_to_the_sale():
    _validate_payment_adjustment_with_change(
        adjusted_total=32_000,
        effective_sale_total=32_000,
        recorded_change=18_000,
        is_separated=False,
    )


def test_does_not_apply_cash_change_rule_to_separated_sales():
    _validate_payment_adjustment_with_change(
        adjusted_total=50_000,
        effective_sale_total=32_000,
        recorded_change=18_000,
        is_separated=True,
    )
