"""Price retained token usage with the per-model prices authored in config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.model_loading import canonical_provider, get_model_instance

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.config.models import ModelPricing
    from mindroom.constants import RuntimePaths
    from mindroom.usage_stats import TokenTotals

logger = get_logger(__name__)

# Provider names that select the same model class and so record the same usage identity.
_PROVIDER_ALIASES = {"gemini": "google", "openai_codex": "codex", "kimi_code": "kimi"}
# These providers report cache reads and writes beside input_tokens; the others count cache reads inside it.
_CACHE_EXCLUDED_PROVIDERS = frozenset({"anthropic", "bedrock_claude", "vertexai_claude"})
# Gemini reports thinking tokens beside output_tokens and bills them as output; the others count them inside it.
_REASONING_EXCLUDED_PROVIDERS = frozenset({"google"})


def provider_identity(provider: str) -> str:
    """Return one name for every configured provider spelling that selects the same model class."""
    canonical = canonical_provider(provider)
    return _PROVIDER_ALIASES.get(canonical, canonical)


@dataclass(frozen=True, slots=True)
class PricedModel:
    """Prices for one recorded model and how its provider counts cached input."""

    pricing: ModelPricing
    input_includes_cache: bool
    output_includes_reasoning: bool = True


def cost_usd(totals: TokenTotals, priced: PricedModel) -> float:
    """Return the USD cost of token counters at the model's per-million prices."""
    pricing = priced.pricing
    uncached_input = totals.input_tokens
    if priced.input_includes_cache:
        uncached_input = max(0, uncached_input - totals.cache_read_tokens - totals.cache_write_tokens)
    output = (
        totals.output_tokens if priced.output_includes_reasoning else totals.output_tokens + totals.reasoning_tokens
    )
    return (
        uncached_input * pricing.input
        + totals.cache_read_tokens * pricing.cache_read_price
        + totals.cache_write_tokens * pricing.cache_write_price
        + output * pricing.output
    ) / 1_000_000


@dataclass(frozen=True, slots=True)
class PriceTable:
    """Prices by recorded (provider, model ID) usage identity, and whether every priced model could be built."""

    prices: Mapping[tuple[str, str], PricedModel]
    complete: bool


def price_table(config: Config, runtime_paths: RuntimePaths) -> PriceTable:
    """Map each priced model's recorded (provider, model ID) usage identity to its prices.

    Usage records the provider name of the live model object, which can differ from the
    configured provider (for example per API transport), so priced models are instantiated.
    """
    table: dict[tuple[str, str], PricedModel] = {}
    complete = True
    for model_name in sorted(config.models):
        model_config = config.models[model_name]
        if model_config.pricing is None:
            continue
        try:
            model = get_model_instance(config, runtime_paths, model_name)
        except Exception as error:
            # Provider SDKs raise arbitrary construction errors; an unusable model stays unpriced.
            logger.warning("budget_pricing_model_unavailable", model=model_name, error=str(error))
            complete = False
            continue
        key = (model.get_provider(), model.id)
        provider = provider_identity(model_config.provider)
        priced = PricedModel(
            pricing=model_config.pricing,
            input_includes_cache=provider not in _CACHE_EXCLUDED_PROVIDERS,
            output_includes_reasoning=provider not in _REASONING_EXCLUDED_PROVIDERS,
        )
        existing = table.get(key)
        if existing is not None and existing.pricing != priced.pricing:
            logger.warning("budget_pricing_conflict", provider=key[0], model_id=key[1])
            if _price_weight(existing.pricing) >= _price_weight(priced.pricing):
                continue
        table[key] = priced
    return PriceTable(prices=table, complete=complete)


def _price_weight(pricing: ModelPricing) -> tuple[float, float, float]:
    return (pricing.input + pricing.output, pricing.cache_read_price, pricing.cache_write_price)
