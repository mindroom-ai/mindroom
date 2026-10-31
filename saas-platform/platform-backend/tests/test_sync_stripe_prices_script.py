"""The Stripe price synchronizer writes only price IDs that bill the amount the pricing YAML shows."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import dotenv
import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "cluster/scripts/sync-stripe-prices.py"
_PLAN = {"name": "Bring Your Own Keys", "price_monthly": 1200, "price_yearly": 9600}


class _FakePrices:
    """In-memory `stripe.Price` with the list filters and create parameters the script uses."""

    def __init__(self, *prices: SimpleNamespace) -> None:
        self.prices = list(prices)
        self.created: list[dict[str, Any]] = []

    def list(self, *, product: str, active: bool | None = None, limit: int) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(data=[p for p in self.prices if active is None or p.active == active])

    def create(self, **params: Any) -> SimpleNamespace:  # noqa: ANN401
        self.created.append(params)
        price = _price(f"price_new_{len(self.created)}", params["unit_amount"])
        self.prices.append(price)
        return price


def _price(price_id: str, amount: int, *, active: bool = True) -> SimpleNamespace:
    metadata = {"tier": "byok", "billing_cycle": "monthly", "platform": "mindroom"}
    return SimpleNamespace(id=price_id, unit_amount=amount, active=active, metadata=metadata)


def _load_script(monkeypatch: pytest.MonkeyPatch, prices: _FakePrices) -> ModuleType:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
    # Importing the script must not read a developer's real saas-platform/.env.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    spec = importlib.util.spec_from_file_location("sync_stripe_prices", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
