"""Price token usage with the per-model prices authored in config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.usage import TokenCounts
from mindroom.logging_config import get_logger
from mindroom.model_usage import provider_reports_cache_tokens_outside_input
from mindroom.providers import registry as provider_registry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.config.models import ModelPricing
    from mindroom.constants import RuntimePaths
    from mindroom.llm.usage import RunMetrics

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PricedModel:
    """Prices for one recorded model and how its provider counts cached input."""

    pricing: ModelPricing
    input_includes_cache: bool


def cost_usd(totals: TokenCounts, priced: PricedModel) -> float:
    """Return the USD cost of token counters at the model's per-million prices."""
    pricing = priced.pricing
    uncached_input = totals.input_tokens
    if priced.input_includes_cache:
        uncached_input = max(0, uncached_input - totals.cache_read_tokens - totals.cache_write_tokens)
    # Recorded output already includes reasoning and thinking tokens for every provider.
    return (
        uncached_input * pricing.input
        + totals.cache_read_tokens * pricing.cache_read_price
        + totals.cache_write_tokens * pricing.cache_write_price
        + totals.output_tokens * pricing.output
    ) / 1_000_000


@dataclass(frozen=True, slots=True)
class _PriceTable:
    """Prices by recorded (provider, model ID) usage identity, and whether every priced model could be built."""

    prices: Mapping[tuple[str, str], PricedModel]
    complete: bool


def _price_table(config: Config, runtime_paths: RuntimePaths) -> _PriceTable:
    """Map each priced model's recorded (provider, model ID) usage identity to its prices.

    Usage records the metrics provider name of the model's provider, which can differ from the
    configured provider (for example per API transport), so each priced model's provider is built.
    """
    table: dict[tuple[str, str], PricedModel] = {}
    complete = True
    for model_name in sorted(config.models):
        model_config = config.models[model_name]
        if model_config.pricing is None:
            continue
        try:
            identity = provider_registry.build_model_provider(config, runtime_paths, model_name).identity
        except Exception as error:
            # Provider SDKs raise arbitrary construction errors; an unusable model stays unpriced.
            logger.warning("budget_pricing_model_unavailable", model=model_name, error=str(error))
            complete = False
            continue
        key = (identity.provider_name, identity.id)
        priced = PricedModel(
            pricing=model_config.pricing,
            input_includes_cache=not provider_reports_cache_tokens_outside_input(
                provider=key[0],
                configured_provider=model_config.provider,
                model_id=identity.id,
            ),
        )
        existing = table.get(key)
        if existing is not None and existing.pricing != priced.pricing:
            logger.warning("budget_pricing_conflict", provider=key[0], model_id=key[1])
            if _price_weight(existing.pricing) >= _price_weight(priced.pricing):
                continue
        table[key] = priced
    return _PriceTable(prices=table, complete=complete)


def _price_weight(pricing: ModelPricing) -> tuple[float, float, float]:
    return (pricing.input + pricing.output, pricing.cache_read_price, pricing.cache_write_price)


# Each priced model's provider is built to learn its recorded identity, so the table is kept for one config object.
_cached_prices: tuple[Config, Mapping[tuple[str, str], PricedModel]] | None = None


def model_prices(config: Config, runtime_paths: RuntimePaths) -> Mapping[tuple[str, str], PricedModel]:
    """Return the price table for ``config``, rebuilding it until every priced model could be built."""
    global _cached_prices
    cached = _cached_prices
    if cached is not None and cached[0] is config:
        return cached[1]
    table = _price_table(config, runtime_paths)
    # A priced model that failed to build is retried on the next call instead of staying unpriced.
    _cached_prices = (config, table.prices) if table.complete else None
    return table.prices


def _priced_counts(counts: TokenCounts) -> TokenCounts:
    """Return only the counters prices apply to, so usage with none of them stays unpriced."""
    return TokenCounts(
        input_tokens=counts.input_tokens,
        output_tokens=counts.output_tokens,
        cache_read_tokens=counts.cache_read_tokens,
        cache_write_tokens=counts.cache_write_tokens,
    )


def _whole_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def run_cost_usd(
    metrics: RunMetrics | None,
    usage: Mapping[str, object] | None,
    *,
    model_provider: str | None,
    model_id: str | None,
    prices: Mapping[tuple[str, str], PricedModel],
) -> float | None:
    """Return the USD cost of one run's usage, or None when none of it used a priced model.

    Per-model details price every model the run used, such as team members on other models,
    as budgets do; without them the run's usage counters are priced at the run's own model.
    """
    details = metrics.details if metrics is not None else None
    entries = [
        ((entry.provider, entry.id), _priced_counts(entry.tokens))
        for model_type_entries in (details or {}).values()
        for entry in model_type_entries
    ]
    if not entries and usage is not None and model_provider is not None and model_id is not None:
        entries = [((model_provider, model_id), _priced_counts(TokenCounts.from_mapping(usage, _whole_count)))]
    cost: float | None = None
    for key, totals in entries:
        priced = prices.get(key)
        if priced is not None and totals != TokenCounts():
            cost = (cost or 0.0) + cost_usd(totals, priced)
    return cost


def format_cost_usd(value: float) -> str:
    """Return a USD amount as a micro-dollar decimal string, because Matrix event JSON forbids floats."""
    return f"{value:.6f}".rstrip("0").rstrip(".")
