"""The Stripe price synchronizer writes only price IDs that bill the amount the pricing YAML shows."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.cluster_scripts import load_stripe_script

_PLAN = {"name": "Bring Your Own Keys", "price_monthly": 1200, "price_yearly": 9600}


class _FakePrices:
    """In-memory `stripe.Price` with the list filters and create parameters the script uses."""

    def __init__(self, *prices: SimpleNamespace) -> None:
        self.prices = list(prices)
        self.created: list[dict[str, Any]] = []

    def list(self, *, product: str, active: bool, limit: int) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(data=[p for p in self.prices if p.active == active])

    def create(self, **params: Any) -> SimpleNamespace:  # noqa: ANN401
        self.created.append(params)
        return _price(f"price_new_{len(self.created)}", params["unit_amount"])


def _price(price_id: str, amount: int, *, active: bool = True) -> SimpleNamespace:
    metadata = {"tier": "byok", "billing_cycle": "monthly", "platform": "mindroom"}
    return SimpleNamespace(id=price_id, unit_amount=amount, active=active, metadata=metadata)


def _load_script(monkeypatch: pytest.MonkeyPatch, prices: _FakePrices) -> ModuleType:
    module = load_stripe_script(monkeypatch, "sync-stripe-prices")
    monkeypatch.setattr(module, "stripe", SimpleNamespace(Price=prices))
    return module


def test_existing_price_with_the_configured_amount_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A price that already bills the configured amount is kept, so rerunning the sync changes nothing."""
    prices = _FakePrices(_price("price_current", 1200))
    script = _load_script(monkeypatch, prices)

    assert script.find_or_create_price("prod_1", "byok", _PLAN, "monthly") == "price_current"
    assert prices.created == []


def test_changed_amount_creates_a_price_that_takes_over_the_lookup_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the YAML amount changes, the sync writes a price that bills the new amount, not the old price."""
    prices = _FakePrices(_price("price_old", 1000))
    script = _load_script(monkeypatch, prices)

    price_id = script.find_or_create_price("prod_1", "byok", _PLAN, "monthly")

    assert price_id == "price_new_1"
    [created] = prices.created
    assert created["unit_amount"] == 1200
    assert created["lookup_key"] == "byok_monthly"
    assert created["transfer_lookup_key"] is True


def test_archived_price_with_the_configured_amount_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An archived price cannot be bought, so a matching one is replaced instead of written to the YAML."""
    prices = _FakePrices(_price("price_archived", 1200, active=False))
    script = _load_script(monkeypatch, prices)

    assert script.find_or_create_price("prod_1", "byok", _PLAN, "monthly") == "price_new_1"
