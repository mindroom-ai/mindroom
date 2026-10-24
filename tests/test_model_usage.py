"""Request-context sizing from one response's provider usage counters."""
# ruff: noqa: D103

from __future__ import annotations

from agno.metrics import MessageMetrics
from agno.models.message import Message

from mindroom.model_usage import response_context_tokens, uncached_input_tokens_from_counts


def _assistant(metrics: MessageMetrics) -> Message:
    return Message(role="assistant", content="ok", metrics=metrics)


def test_response_context_tokens_adds_cache_tokens_only_where_reported_outside_input() -> None:
    anthropic = _assistant(MessageMetrics(input_tokens=10, cache_read_tokens=90, cache_write_tokens=5))
    openai = _assistant(MessageMetrics(input_tokens=100, cache_read_tokens=90))

    assert (
        response_context_tokens(anthropic, provider="Anthropic", configured_provider="anthropic", model_id="m") == 105
    )
    assert response_context_tokens(openai, provider="OpenAI", configured_provider="openai", model_id="m") == 100


def test_native_final_iteration_context_wins_over_billed_counters() -> None:
    message = _assistant(
        MessageMetrics(
            input_tokens=900,
            cache_read_tokens=0,
            provider_metrics={"context_usage": {"input_tokens": 40, "cache_read_tokens": 60, "cache_write_tokens": 0}},
        ),
    )

    assert response_context_tokens(message, provider="Anthropic", configured_provider="anthropic", model_id="m") == 100


def test_response_without_reported_usage_has_no_context_size() -> None:
    assert (
        response_context_tokens(
            _assistant(MessageMetrics()),
            provider="OpenAI",
            configured_provider="openai",
            model_id="m",
        )
        is None
    )


def test_uncached_input_subtracts_cache_reads_where_input_includes_them() -> None:
    assert (
        uncached_input_tokens_from_counts(
            input_tokens=1_000,
            cache_read_tokens=400,
            cache_write_tokens=None,
            provider="OpenAI",
            configured_provider="openai",
            model_id="gpt-6-luna",
        )
        == 600
    )


def test_uncached_input_counts_cache_writes_where_cache_is_reported_outside_input() -> None:
    assert (
        uncached_input_tokens_from_counts(
            input_tokens=1_000,
            cache_read_tokens=9_000,
            cache_write_tokens=50,
            provider="Anthropic",
            configured_provider="anthropic",
            model_id="claude-sonnet-5-5",
        )
        == 1_050
    )


def test_uncached_input_is_unknown_without_input() -> None:
    assert (
        uncached_input_tokens_from_counts(
            input_tokens=None,
            cache_read_tokens=10,
            cache_write_tokens=None,
            provider="OpenAI",
            configured_provider="openai",
            model_id="gpt-6-luna",
        )
        is None
    )
