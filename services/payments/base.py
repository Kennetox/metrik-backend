from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PaymentProvider(Protocol):
    """Minimal provider contract used by web commerce payment flows."""

    name: str

    def map_external_status(self, status: str | None) -> str:
        ...

    def create_checkout(self, db: Any, order: Any, **kwargs: Any) -> Any:
        ...

    def refresh_order_status(self, db: Any, order: Any) -> Any:
        ...

    def process_webhook(self, db: Any, **kwargs: Any) -> dict[str, Any]:
        ...
