"""Leaf helpers for normalizing provider usage counters."""

from __future__ import annotations


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
