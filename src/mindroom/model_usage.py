"""Leaf helpers for normalizing provider usage counters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.models.message import Message


def _provider_reports_cache_tokens_outside_input(
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
    if not _provider_reports_cache_tokens_outside_input(
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    ):
        return input_tokens
    return input_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0)


def response_context_tokens(
    message: Message,
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> int | None:
    """Return the request-context size one response reported, or None when its provider reported no usage.

    A native compaction iteration's final ``context_usage`` describes the active context; billed counters can
    include the transcript the checkpoint replaced.
    """
    metrics = message.metrics
    usage = (metrics.provider_metrics or {}).get("context_usage")
    counts = (
        usage
        if isinstance(usage, dict)
        else {
            "input_tokens": metrics.input_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_write_tokens": metrics.cache_write_tokens,
        }
    )
    tokens = context_input_tokens_from_counts(
        input_tokens=_count(counts, "input_tokens"),
        cache_read_tokens=_count(counts, "cache_read_tokens"),
        cache_write_tokens=_count(counts, "cache_write_tokens"),
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    )
    # Agno starts every counter at zero, so zero means the provider reported nothing.
    return tokens or None


def _count(counts: Mapping[str, object], key: str) -> int | None:
    value = counts.get(key)
    return value if isinstance(value, int) else None
