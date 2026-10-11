"""Model pricing and per-user budget configuration."""
# ruff: noqa: D103

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing


def _models() -> dict[str, ModelConfig]:
    return {
        "astra": ModelConfig(provider="openai", id="gpt-6-astra", pricing=ModelPricing(input=5.0, output=30.0)),
        "luna": ModelConfig(provider="openai", id="gpt-6-luna", pricing=ModelPricing(input=0.2, output=1.25)),
    }


def test_pricing_cache_prices_default_to_input() -> None:
    pricing = ModelPricing(input=3.0, output=15.0)

    assert (pricing.cache_read_price, pricing.cache_write_price) == (3.0, 3.0)


def test_pricing_keeps_explicit_cache_prices() -> None:
    pricing = ModelPricing(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)

    assert (pricing.cache_read_price, pricing.cache_write_price) == (0.3, 3.75)


def test_pricing_rejects_negative_prices() -> None:
    with pytest.raises(ValidationError):
        ModelPricing(input=-1.0, output=1.0)


def test_pricing_rejects_infinite_prices() -> None:
    with pytest.raises(ValidationError):
        ModelPricing(input=float("inf"), output=1.0)


def test_budgets_parse_default_overrides_and_fallback() -> None:
    config = Config(
        models=_models(),
        budgets={"monthly_limit_usd": 20, "fallback_model": "luna", "users": {"@alice:example.test": 100}},
    )

    assert config.budgets == BudgetsConfig(
        monthly_limit_usd=20.0,
        fallback_model="luna",
        users={"@alice:example.test": 100.0},
    )


def test_budgets_are_disabled_by_default() -> None:
    assert Config(models=_models()).budgets is None


def test_budgets_reject_unknown_fallback_model() -> None:
    with pytest.raises(ValidationError, match="Unknown budgets fallback_model"):
        Config(models=_models(), budgets={"fallback_model": "missing"})


def test_budgets_reject_non_matrix_user_keys() -> None:
    with pytest.raises(ValidationError, match="concrete Matrix user IDs"):
        Config(models=_models(), budgets={"fallback_model": "luna", "users": {"alice": 5}})


def test_budgets_reject_negative_limits() -> None:
    with pytest.raises(ValidationError):
        Config(models=_models(), budgets={"fallback_model": "luna", "users": {"@alice:example.test": -1}})
    with pytest.raises(ValidationError):
        Config(models=_models(), budgets={"fallback_model": "luna", "monthly_limit_usd": -1})


def test_budgets_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Config(models=_models(), budgets={"fallback_model": "luna", "period": "weekly"})
