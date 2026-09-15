"""Temporary compatibility repairs for Agno's OpenAI Chat parser."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agno.models.response import ModelResponse
    from openai.types.chat import ChatCompletion

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

    def parse_tool_calls(self, tool_calls_data: list[Any]) -> list[dict[str, Any]]:
        """Drop empty slots created by sparse streamed tool-call indexes."""
        parsed = super().parse_tool_calls(tool_calls_data)  # ty: ignore[unresolved-attribute]
        return [tool_call for tool_call in parsed if isinstance(tool_call.get("function"), dict)]
