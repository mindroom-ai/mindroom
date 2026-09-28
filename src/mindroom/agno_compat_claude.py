"""Temporary request and response compatibility for Agno Claude adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mindroom.model_defaults import CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES

if TYPE_CHECKING:
    from agno.models.response import ModelResponse
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types.beta import BetaMessage

_SAMPLING_CONTROL_NAMES = ("temperature", "top_p", "top_k")

# AGNO_COMPAT: Claude requests include unsupported sampling controls.
# Reason: Agno 3.0.9 moves sampling controls into extra_body even for current
# Claude generations that reject those controls in supported request modes.
# Upstream issue: https://github.com/agno-agi/agno/issues/9931
# Upstream PR: https://github.com/agno-agi/agno/pull/9933
# Remove when: The pinned Agno release removes model fields and raw request
# overrides from both top-level params and extra_body for the same generations.
# Coverage: tests/test_claude_compat.py::test_default_sampling_models_lose_sampling_controls_everywhere;
# tests/test_claude_compat.py::test_other_claude_models_keep_sampling_controls_in_extra_body.

# AGNO_COMPAT: Claude parsing drops terminal stop reasons.
# Reason: Agno 3.0.9 does not expose Claude's terminal stop_reason in parsed
# provider_data, which prevents consumers from detecting provider-capped output.
# Upstream issue: No matching issue identified; this metadata extension point is untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno parser exposes stop_reason through provider_data or
# another stable terminal-metadata interface.
# Coverage: tests/test_compaction_summary_provider_compat.py::test_summary_uses_stop_reason_and_raw_body_precedence.


class ClaudeProviderSDKCompat:
    """Sanitize Agno-built requests and preserve terminal metadata."""

    id: str

    def get_request_params(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Remove unsupported sampling controls after Agno merges request parameters."""
        request_params = super().get_request_params(  # ty: ignore[unresolved-attribute]
            response_format=response_format,
            tools=tools,
        )
        if self.id.casefold().endswith(CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES):
            extra_body = request_params.get("extra_body")
            for parameter_name in _SAMPLING_CONTROL_NAMES:
                request_params.pop(parameter_name, None)
                if isinstance(extra_body, dict):
                    extra_body.pop(parameter_name, None)
            if isinstance(extra_body, dict) and not extra_body:
                del request_params["extra_body"]
        return request_params

    def _parse_provider_response(
        self,
        response: AnthropicMessage | BetaMessage,
        response_format: dict[str, Any] | type[Any] | None = None,
        **kwargs: object,
    ) -> ModelResponse:
        parsed = super()._parse_provider_response(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
            **kwargs,
        )
        parsed.provider_data = {**(parsed.provider_data or {}), "stop_reason": response.stop_reason}
        return parsed


# AGNO_COMPAT: Claude history replays response-only text citations.
# Reason: Agno 3.0.9 stores Claude response blocks with model_dump(exclude_none=True)
# and replays them verbatim, so a document citation whose document_title is null
# loses that required field and every later request fails with HTTP 400
# ("citations.0.char_location.document_title: Field required"). Restoring the
# field is not enough: document citations point at request-scoped document
# indexes, and historical documents are not replayed (MindRoom strips historical
# media), so the API then rejects the index ("Invalid document index in document
# citation"). Agno also has no replay hook for repairing stored blocks.
# Upstream issue: No matching issue identified; replaying citations without their
# cited sources is untracked.
# Upstream PR: https://github.com/agno-agi/agno/pull/10147 restores null document
# titles only, which leaves the stale document index failure.
# Remove when: The pinned Agno release omits response citations from replayed
# assistant text blocks, or drops citations whose cited sources are absent from
# the request, so stored and already-poisoned histories replay without them.
# Coverage: tests/test_claude_citation_replay.py::test_agno_replays_untitled_document_citation_without_its_required_title;
# tests/test_claude_citation_replay.py::test_loaded_claude_replays_cited_answer_as_plain_text;
# tests/test_claude_citation_replay.py::test_poisoned_stored_thread_replays_without_citations;
# tests/test_claude_citation_replay.py::test_vertex_claude_request_payload_replays_without_citations.
def request_kwargs_without_replayed_citations(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop response citations from assistant text blocks, keeping their text.

    Citations are response annotations. Their required fields and cited source
    indexes cannot be replayed faithfully, and the model does not need them to
    read its earlier answer. The input structure is never mutated.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    prepared_messages: list[Any] | None = None
    for message_index, message in enumerate(messages):
        message_dict = _as_dict(message)
        if message_dict is None or message_dict.get("role") != "assistant":
            continue
        content = message_dict.get("content")
        if not isinstance(content, list) or not any(_cited_text_block(block) is not None for block in content):
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        prepared_content: list[Any] = []
        for block in content:
            cited_block = _cited_text_block(block)
            prepared_content.append(
                block
                if cited_block is None
                else {key: value for key, value in cited_block.items() if key != "citations"},
            )
        prepared_messages[message_index] = {**message_dict, "content": prepared_content}
    if prepared_messages is None:
        return request_kwargs
    return {**request_kwargs, "messages": prepared_messages}


def _as_dict(value: object) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _cited_text_block(block: object) -> dict[str, Any] | None:
    block_dict = _as_dict(block)
    if block_dict is None or block_dict.get("type") != "text" or "citations" not in block_dict:
        return None
    return block_dict
