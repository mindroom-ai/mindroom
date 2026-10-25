"""Leaf helpers for normalizing provider usage counters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.messages import Message
    from mindroom.llm.usage import MessageMetrics


@dataclass(frozen=True, slots=True)
class ContextTokens:
    """A request context's size in tokens and its cache reads and writes: one response's report, or an estimate.

    A cache counter is None when the provider did not report it, and always for an estimate. A zero cache counter
    among a response's billed counters reads as unreported; Claude's ``context_usage`` reports each one, a zero one too.
    """

    tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @property
    def cache_read_input_tokens(self) -> int:
        """Return the context's tokens read from cache, at most its size.

        An OpenAI-compatible server can report more cached tokens than prompt tokens.
        """
        return min(self.cache_read_tokens or 0, self.tokens)

    @property
    def uncached_input_tokens(self) -> int:
        """Return the context's tokens not read from cache; tokens written to cache were not read from it."""
        return self.tokens - self.cache_read_input_tokens


def provider_reports_cache_tokens_outside_input(
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> bool:
    """Return whether cache tokens must be added to input tokens for context occupancy."""
    provider_key = (provider or configured_provider or "").lower()
    configured_provider_key = (configured_provider or "").lower()
    model_key = (model_id or "").lower()
    if "anthropic" in provider_key or "bedrock" in provider_key:
        return True
    if configured_provider_key == "vertexai_claude":
        return True
    return "vertex" in provider_key and "claude" in model_key


def context_input_tokens_from_counts(
    *,
    input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> int | None:
    """Return full request-context tokens from provider usage counters."""
    if input_tokens is None:
        return None
    if not provider_reports_cache_tokens_outside_input(
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    ):
        return input_tokens
    return input_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0)


def uncached_input_tokens_from_counts(
    *,
    input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> int | None:
    """Return input tokens not read from cache; tokens written to cache are new input."""
    if input_tokens is None:
        return None
    if provider_reports_cache_tokens_outside_input(
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    ):
        return input_tokens + (cache_write_tokens or 0)
    return max(0, input_tokens - (cache_read_tokens or 0))


def message_context_tokens(
    message: Message,
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> ContextTokens | None:
    """Return the request context one response reported, or None when its usage is absent or sums retried attempts."""
    if isinstance(message.provider_data, dict) and message.provider_data.get("mindroom_aggregate_usage") is True:
        # The counters sum several retried attempts, so they describe no single request's context.
        return None
    return metrics_context_tokens(
        message.metrics,
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    )


def metrics_context_tokens(
    metrics: MessageMetrics,
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> ContextTokens | None:
    """Return the request context one request's usage reports, or None when it reported no usage.

    A Claude response's ``context_usage`` holds its final iteration's counters, each one reported, a zero one too:
    after a native compaction that iteration alone describes the active context, while the billed counters can
    include the transcript the checkpoint replaced.
    """
    usage = (metrics.provider_metrics or {}).get("context_usage")
    if isinstance(usage, dict):
        input_tokens = _count(usage, "input_tokens")
        cache_read_tokens = _count(usage, "cache_read_tokens")
        cache_write_tokens = _count(usage, "cache_write_tokens")
    else:
        # A counter the provider did not report stays zero, so a zero cache counter counts as unreported.
        input_tokens = metrics.tokens.input_tokens
        cache_read_tokens = metrics.tokens.cache_read_tokens or None
        cache_write_tokens = metrics.tokens.cache_write_tokens or None
    tokens = context_input_tokens_from_counts(
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    )
    # A zero size means the provider reported nothing.
    if not tokens:
        return None
    return ContextTokens(tokens, cache_read_tokens, cache_write_tokens)


def _count(counts: Mapping[str, object], key: str) -> int | None:
    value = counts.get(key)
    return value if isinstance(value, int) else None
