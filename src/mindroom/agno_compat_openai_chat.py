"""Temporary compatibility repairs for Agno's OpenAI Chat parser."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agno.metrics import MessageMetrics
    from agno.models.response import ModelResponse
    from openai.types.chat import ChatCompletion
    from openai.types.completion_usage import CompletionUsage

# AGNO_COMPAT: Sparse streamed tool-call indexes leave malformed placeholders.
# Reason: Agno 3.0.9 leaves empty slots when a streamed tool-call index starts
# above zero. This removes only those malformed placeholders; it does not repair
# index collisions or missing indexes.
# Upstream issue: https://github.com/agno-agi/agno/issues/8879
# Upstream PR: https://github.com/agno-agi/agno/pull/8880
# Remove when: The pinned Agno parser handles sparse indexes, index collisions,
# and missing indexes without producing empty or merged calls.
# Coverage: tests/test_openai_models.py::test_chat_models_drop_sparse_stream_placeholders.

# AGNO_COMPAT: Chat Completions parsing drops terminal finish reasons.
# Reason: Agno 3.0.9 drops Chat Completions finish_reason during parsing, while
# response completion classification needs the provider's terminal reason.
# Upstream issue: No matching issue identified; this metadata extension point is untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno parser exposes finish_reason in provider_data or
# another stable terminal-metadata interface.
# Coverage: tests/test_compaction_openai_summary.py::test_summary_rejects_explicit_output_limit_below_usage_cap;
# tests/test_compaction_openai_summary.py::test_summary_rejects_partial_text_stopped_by_content_filter.

# AGNO_COMPAT: Chat usage parsing drops cache-write input tokens.
# Reason: Agno 3.0.9 copies cached, audio, and reasoning token details but
# omits OpenAI's prompt_tokens_details.cache_write_tokens counter.
# Upstream issue: No matching issue identified; this metrics gap is untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno parser preserves cache-write tokens while still
# accepting provider payloads that omit the newer optional field.
# Coverage: tests/test_openai_models.py::test_openai_metrics_preserve_sdk_input_details.


class OpenAIChatProviderCompat:
    """Preserve terminal metadata and remove Agno's sparse parser slots.

    Keep this as a plain mixin ahead of the concrete Agno model. Making it a
    dataclass or an OpenAIChat subclass would overwrite provider-specific field
    defaults during dataclass collection.
    """

    def _parse_provider_response(self, response: ChatCompletion, **kwargs: object) -> ModelResponse:
        parsed = super()._parse_provider_response(response, **kwargs)  # ty: ignore[unresolved-attribute]
        parsed.provider_data = {**(parsed.provider_data or {}), "finish_reason": response.choices[0].finish_reason}
        return parsed

    def _get_metrics(self, response_usage: CompletionUsage) -> MessageMetrics:
        """Preserve cache-write tokens alongside Agno's other usage counters."""
        metrics = super()._get_metrics(response_usage)  # ty: ignore[unresolved-attribute]
        if prompt_token_details := response_usage.prompt_tokens_details:
            metrics.cache_write_tokens = prompt_token_details.cache_write_tokens or 0
        return metrics

    def parse_tool_calls(self, tool_calls_data: list[Any]) -> list[dict[str, Any]]:
        """Drop empty slots created by sparse streamed tool-call indexes."""
        parsed = super().parse_tool_calls(tool_calls_data)  # ty: ignore[unresolved-attribute]
        return [tool_call for tool_call in parsed if isinstance(tool_call.get("function"), dict)]
