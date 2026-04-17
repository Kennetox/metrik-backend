from __future__ import annotations

from typing import Dict, Optional

from .base import PaymentProvider


_REGISTRY: Dict[str, PaymentProvider] = {}
_DEFAULTS_READY = False


def _normalize_name(name: str | None) -> str:
    return (name or "").strip().lower()


def register_provider(provider: PaymentProvider) -> None:
    name = _normalize_name(getattr(provider, "name", None))
    if not name:
        raise ValueError("Payment provider must define a valid name")
    _REGISTRY[name] = provider


def get_provider(name: str | None) -> Optional[PaymentProvider]:
    ensure_default_providers()
    return _REGISTRY.get(_normalize_name(name))


def list_providers() -> dict[str, PaymentProvider]:
    ensure_default_providers()
    return dict(_REGISTRY)


def ensure_default_providers() -> None:
    global _DEFAULTS_READY
    if _DEFAULTS_READY:
        return
    # Lazy import to avoid loading router modules at startup when unnecessary.
    from .providers.mercadopago import MercadoPagoPaymentProvider
    from .providers.wompi import WompiPaymentProvider

    register_provider(MercadoPagoPaymentProvider())
    register_provider(WompiPaymentProvider())
    _DEFAULTS_READY = True
