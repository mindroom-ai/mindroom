"""Pricing retained token usage for budgets."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.budgets import pricing as pricing_module
from mindroom.budgets.pricing import (
    PricedModel,
    _price_table,
    _PriceTable,
    cost_usd,
    format_cost_usd,
    model_prices,
    run_cost_usd,
)
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.llm.usage import ModelMetrics, RunMetrics
from mindroom.providers.registry import build_model_provider
from mindroom.usage.stats import TokenTotals

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

    table = _price_table(config, paths).prices

    sol = build_model_provider(config, paths, "sol").identity
    opus = build_model_provider(config, paths, "opus").identity
    codex = build_model_provider(config, paths, "codex").identity
    assert set(table) == {(sol.provider_name, "gpt-6.1-sol"), (opus.provider_name, "claude-opus-5-5")}
    assert (codex.provider_name, "gpt-6.1-sol") not in table
    assert table[(sol.provider_name, "gpt-6.1-sol")].input_includes_cache is True
    assert table[(opus.provider_name, "claude-opus-5-5")].input_includes_cache is False


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

    (priced,) = _price_table(config, _paths(tmp_path)).prices.values()

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

    (priced,) = _price_table(config, _paths(tmp_path)).prices.values()

    assert priced.pricing == ModelPricing(input=0.2, output=2)


@pytest.mark.parametrize("provider", ["google", "gemini", "openai"])
def test_reasoning_tokens_are_charged_as_part_of_output(tmp_path: Path, provider: str) -> None:
    """Recorded output already includes reasoning and Gemini thinking tokens, so they are not added again."""
    config = Config(
        models={
            "m": ModelConfig(
                provider=provider,
                id="some-model",
                api_key="key",
                pricing=ModelPricing(input=1, output=10),
            ),
        },
    )
    (priced,) = _price_table(config, _paths(tmp_path)).prices.values()
    totals = TokenTotals(input_tokens=1_000_000, output_tokens=500_000, reasoning_tokens=400_000)

    assert cost_usd(totals, priced) == pytest.approx(1.0 + 500_000 * 10.0 / 1e6)


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

    (priced,) = _price_table(config, _paths(tmp_path)).prices.values()

    assert priced.pricing.cache_read_price == 0.5


def test_price_table_reports_a_priced_model_it_could_not_build(tmp_path: Path) -> None:
    config = Config(
        models={
            "m": ModelConfig(
                provider="openai",
                id="gpt-6-luna",
                api_key="key",
                pricing=ModelPricing(input=1, output=2),
            ),
        },
    )

    with patch("mindroom.budgets.pricing.build_model_provider", side_effect=RuntimeError("missing SDK")):
        table = _price_table(config, _paths(tmp_path))

    assert table == _PriceTable(prices={}, complete=False)


_GPT_X = PricedModel(ModelPricing(input=2.0, output=8.0), input_includes_cache=True)


def test_single_model_run_is_priced_from_usage() -> None:
    cost = run_cost_usd(
        None,
        {"input_tokens": 1_000_000, "output_tokens": 100_000, "total_tokens": 1_100_000},
        model_provider="OpenAI",
        model_id="gpt-x",
        prices={("OpenAI", "gpt-x"): _GPT_X},
    )

    assert cost == pytest.approx(2.8)


def test_team_details_are_priced_per_model() -> None:
    """A team reply whose member runs another model is charged that model's prices, not the leader's."""
    metrics = RunMetrics(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        details={
            "model": [
                ModelMetrics(id="a", provider="P", input_tokens=1_000_000),
                ModelMetrics(id="b", provider="P", output_tokens=1_000_000),
            ],
        },
    )
    prices = {
        ("P", "a"): PricedModel(ModelPricing(input=1, output=1), input_includes_cache=True),
        ("P", "b"): PricedModel(ModelPricing(input=10, output=10), input_includes_cache=True),
    }

    cost = run_cost_usd(metrics, metrics.to_dict(), model_provider="P", model_id="a", prices=prices)

    assert cost == pytest.approx(11.0)


def test_summed_team_metrics_keep_each_member_model_priced() -> None:
    """Team replies add member metrics to the leader's, which must keep per-model details apart."""
    leader = RunMetrics(
        input_tokens=1_000_000,
        details={"model": [ModelMetrics(id="a", provider="P", input_tokens=1_000_000)]},
    )
    member = RunMetrics(
        output_tokens=1_000_000,
        details={"model": [ModelMetrics(id="b", provider="P", output_tokens=1_000_000)]},
    )
    prices = {
        ("P", "a"): PricedModel(ModelPricing(input=1, output=1), input_includes_cache=True),
        ("P", "b"): PricedModel(ModelPricing(input=10, output=10), input_includes_cache=True),
    }

    assert run_cost_usd(leader + member, None, model_provider="P", model_id="a", prices=prices) == pytest.approx(11.0)


def test_partly_priced_details_charge_only_priced_models() -> None:
    metrics = RunMetrics(
        details={
            "model": [
                ModelMetrics(id="a", provider="P", input_tokens=1_000_000),
                ModelMetrics(id="local", provider="Ollama", input_tokens=5_000_000),
            ],
        },
    )
    prices = {("P", "a"): PricedModel(ModelPricing(input=1, output=1), input_includes_cache=True)}

    assert run_cost_usd(metrics, None, model_provider="P", model_id="a", prices=prices) == pytest.approx(1.0)


def test_unpriced_run_has_no_cost() -> None:
    usage = {"input_tokens": 1_000, "output_tokens": 10}

    assert run_cost_usd(None, usage, model_provider="Ollama", model_id="qwen", prices={}) is None


def test_run_without_usage_has_no_cost() -> None:
    prices = {("OpenAI", "gpt-x"): _GPT_X}

    assert run_cost_usd(None, None, model_provider="OpenAI", model_id="gpt-x", prices=prices) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0421, "0.0421"), (0.0000324, "0.000032"), (1.5, "1.5"), (12.0, "12"), (0.0, "0"), (0.00000004, "0")],
)
def test_format_cost_usd(value: float, expected: str) -> None:
    assert format_cost_usd(value) == expected


def test_incomplete_model_prices_are_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A priced model that failed to build once is retried instead of staying unpriced until the next reload."""
    builds: list[Config] = []

    def table(config: Config, _paths: RuntimePaths) -> _PriceTable:
        builds.append(config)
        return _PriceTable(prices={("P", "a"): _GPT_X}, complete=len(builds) > 1)

    monkeypatch.setattr(pricing_module, "_price_table", table)
    config = Config()
    paths = _paths(tmp_path)

    for _ in range(3):
        assert model_prices(config, paths) == {("P", "a"): _GPT_X}
    reloaded = Config()
    model_prices(reloaded, paths)

    assert builds == [config, config, reloaded]
