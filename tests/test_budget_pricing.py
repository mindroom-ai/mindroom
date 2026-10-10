"""Pricing retained token usage for budgets."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.budgets.pricing import PricedModel, cost_usd, price_table
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.usage_stats import TokenTotals

if TYPE_CHECKING:
    from pathlib import Path


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def test_openai_cache_reads_are_charged_out_of_input() -> None:
    priced = PricedModel(ModelPricing(input=2.0, output=8.0, cache_read=0.5), input_includes_cache=True)
    totals = TokenTotals(input_tokens=1_000_000, cache_read_tokens=400_000, output_tokens=100_000)

    assert cost_usd(totals, priced) == pytest.approx((600_000 * 2.0 + 400_000 * 0.5 + 100_000 * 8.0) / 1e6)


def test_anthropic_cache_tokens_are_charged_beside_input() -> None:
    pricing = ModelPricing(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
    priced = PricedModel(pricing, input_includes_cache=False)
    totals = TokenTotals(input_tokens=10_000, cache_read_tokens=90_000, cache_write_tokens=5_000, output_tokens=1_000)

    assert cost_usd(totals, priced) == pytest.approx((10_000 * 3.0 + 90_000 * 0.3 + 5_000 * 3.75 + 1_000 * 15.0) / 1e6)


def test_unset_cache_price_charges_cache_reads_at_input_price() -> None:
    priced = PricedModel(ModelPricing(input=2.0, output=8.0), input_includes_cache=True)
    totals = TokenTotals(input_tokens=1_000_000, cache_read_tokens=400_000)

    assert cost_usd(totals, priced) == pytest.approx(2.0)


def test_inconsistent_cache_counters_never_make_input_negative() -> None:
    priced = PricedModel(ModelPricing(input=2.0, output=8.0, cache_read=0.5), input_includes_cache=True)

    assert cost_usd(TokenTotals(input_tokens=10, cache_read_tokens=1_000_000), priced) == pytest.approx(0.5)


def test_price_table_keys_models_by_their_recorded_provider_identity(tmp_path: Path) -> None:
    config = Config(
        models={
            "sol": ModelConfig(
                provider="openai",
                id="gpt-6.1-sol",
                api_key="key",
                pricing=ModelPricing(input=1, output=4),
            ),
            "codex": ModelConfig(provider="codex", id="gpt-6.1-sol"),
            "opus": ModelConfig(
                provider="anthropic",
                id="claude-opus-5-5",
                api_key="key",
                pricing=ModelPricing(input=5, output=25),
            ),
        },
    )
    paths = _paths(tmp_path)

    table = price_table(config, paths)

    sol = get_model_instance(config, paths, "sol")
    opus = get_model_instance(config, paths, "opus")
    codex = get_model_instance(config, paths, "codex")
    assert set(table) == {(sol.get_provider(), "gpt-6.1-sol"), (opus.get_provider(), "claude-opus-5-5")}
    assert (codex.get_provider(), "gpt-6.1-sol") not in table
    assert table[(sol.get_provider(), "gpt-6.1-sol")].input_includes_cache is True
    assert table[(opus.get_provider(), "claude-opus-5-5")].input_includes_cache is False


@pytest.mark.parametrize("provider", ["anthropic", "bedrock_claude", "vertexai_claude"])
def test_anthropic_family_providers_report_cache_outside_input(tmp_path: Path, provider: str) -> None:
    extra = {"project_id": "project", "region": "global"} if provider == "vertexai_claude" else None
    if provider == "bedrock_claude":
        extra = {"aws_region": "us-east-1"}
    config = Config(
        models={
            "claude": ModelConfig(
                provider=provider,
                id="claude-sonnet-5-5",
                api_key="key" if provider == "anthropic" else None,
                extra_kwargs=extra,
                pricing=ModelPricing(input=3, output=15),
            ),
        },
    )

    (priced,) = price_table(config, _paths(tmp_path)).values()

    assert priced.input_includes_cache is False


def test_conflicting_prices_for_one_model_keep_the_higher_price(tmp_path: Path) -> None:
    config = Config(
        models={
            "cheap": ModelConfig(
                provider="openai",
                id="gpt-6-luna",
                api_key="key",
                pricing=ModelPricing(input=0.1, output=1),
            ),
            "dear": ModelConfig(
                provider="openai",
                id="gpt-6-luna",
                api_key="key",
                pricing=ModelPricing(input=0.2, output=2),
            ),
        },
    )

    (priced,) = price_table(config, _paths(tmp_path)).values()

    assert priced.pricing == ModelPricing(input=0.2, output=2)


def test_gemini_thinking_tokens_are_charged_at_the_output_price() -> None:
    priced = PricedModel(
        ModelPricing(input=1.0, output=10.0),
        input_includes_cache=True,
        output_includes_reasoning=False,
    )
    totals = TokenTotals(input_tokens=1_000_000, output_tokens=100_000, reasoning_tokens=400_000)

    assert cost_usd(totals, priced) == pytest.approx(1.0 + (100_000 + 400_000) * 10.0 / 1e6)


@pytest.mark.parametrize(("provider", "includes"), [("google", False), ("gemini", False), ("openai", True)])
def test_price_table_marks_providers_that_report_thinking_outside_output(
    tmp_path: Path,
    provider: str,
    includes: bool,
) -> None:
    config = Config(
        models={
            "m": ModelConfig(
                provider=provider,
                id="some-model",
                api_key="key",
                pricing=ModelPricing(input=1, output=2),
            ),
        },
    )

    (priced,) = price_table(config, _paths(tmp_path)).values()

    assert priced.output_includes_reasoning is includes


def test_conflicting_cache_prices_keep_the_higher_cache_price(tmp_path: Path) -> None:
    config = Config(
        models={
            "a": ModelConfig(
                provider="openai",
                id="gpt-6-luna",
                api_key="key",
                pricing=ModelPricing(input=1, output=2, cache_read=0.1),
            ),
            "b": ModelConfig(
                provider="openai",
                id="gpt-6-luna",
                api_key="key",
                pricing=ModelPricing(input=1, output=2, cache_read=0.5),
            ),
        },
    )

    (priced,) = price_table(config, _paths(tmp_path)).values()

    assert priced.pricing.cache_read_price == 0.5
