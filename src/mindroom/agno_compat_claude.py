"""Temporary request and response compatibility for Agno Claude adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.claude_wire_blocks import (
    SERVER_TOOL_USE_BLOCK_TYPE,
    TOOL_SEARCH_RESULT_BLOCK_TYPE,
    TOOL_SEARCH_TOOL_NAME,
    as_dict,
)
from mindroom.model_defaults import CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES

if TYPE_CHECKING:
    from agno.metrics import MessageMetrics
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


# AGNO_COMPAT: Claude streams report usage only when they complete.
# Reason: Agno 3.0.9 reads stream usage only from the final message_stop snapshot, although Anthropic
# reports input and cache usage in message_start. A reply stopped before message_stop records no usage
# for that request, though Anthropic bills the input and cache tokens it reported.
# Upstream issue: Tracking gap; no issue tracks stopped streams. Agno moved Claude stream usage to
# message_stop to fix double counting in https://github.com/agno-agi/agno/issues/6537, so a fix must still
# count completed streams once.
# Upstream PR: None identified.
# Remove when: The pinned Agno release keeps message_start usage for a stream that ends before
# message_stop, while counting completed streams once.
# Coverage: tests/test_claude_stream_usage.py::test_stopped_claude_reply_keeps_the_usage_reported_at_stream_start;
# tests/test_claude_stream_usage.py::test_hard_stopped_claude_reply_keeps_the_usage_reported_at_stream_start;
# tests/test_claude_stream_usage.py::test_claude_reply_closed_from_another_task_keeps_its_start_usage;
# tests/test_claude_stream_usage.py::test_completed_claude_stream_counts_its_usage_once.


class ClaudeProviderSDKCompat:
    """Sanitize Agno-built requests, preserve terminal metadata, and keep stream start usage for settlement."""

    id: str
    # Usage from message_start of the stream this model is reading, until it reports its final usage.
    # A model streams one request at a time, and settlement of an interrupted request may run in another task.
    _stream_start_usage: MessageMetrics | None = None

    def take_unfinished_stream_usage(self) -> MessageMetrics | None:
        """Return and forget the start usage of a stream that never reported its final usage."""
        start, self._stream_start_usage = self._stream_start_usage, None
        return start

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

    def _parse_provider_response_delta(
        self,
        response: object,
        response_format: dict[str, Any] | type[Any] | None = None,
    ) -> ModelResponse:
        # Only Claude models parse these events, so the provider SDK is already loaded.
        from anthropic.types import RawMessageStartEvent  # noqa: PLC0415
        from anthropic.types.beta import BetaRawMessageStartEvent  # noqa: PLC0415

        parsed = super()._parse_provider_response_delta(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
        )
        if isinstance(response, (RawMessageStartEvent, BetaRawMessageStartEvent)):
            # Keep it aside: settlement adds it only if the stream ends before its final usage.
            self._stream_start_usage = self._get_metrics(response.message.usage)  # ty: ignore[unresolved-attribute]
        elif parsed.response_usage is not None:
            self._stream_start_usage = None
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
        message_dict = as_dict(message)
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


# AGNO_COMPAT: Claude history can place a resumed tool result after the batch's media message.
# Reason: Agno 3.0.9 appends a "The tool call above generated the attached media."
# user message right after a tool batch whose results carried media. When that
# batch also paused a call for approval, continue_run appends the approved
# call's result after the media message, and the stored run keeps that order.
# format_messages merges both into one user turn whose text and image blocks
# precede the late tool_result, but the API only pairs tool_result blocks that
# open the turn, so the resumed request and every later Claude request that
# replays the run fail with HTTP 400 ("tool_use ids were found without
# tool_result blocks immediately after"). Runs stored by providers that accept
# this order fail the same way once the agent switches to Claude.
# Upstream issue: No matching issue identified; resumed tool results after media follow-ups are untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno Claude formatter puts every tool_result ahead of
# other blocks in a user turn, so already-stored runs replay; appending resumed
# results before the media message alone leaves stored runs broken.
# Coverage: tests/test_claude_tool_result_order.py::test_approved_tool_result_leads_after_sibling_media;
# tests/test_claude_tool_result_order.py::test_stored_responses_thread_replays_to_claude_with_results_first;
# tests/test_claude_tool_result_order.py::test_vertex_claude_request_payload_puts_tool_results_first.
def request_kwargs_with_leading_tool_results(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Move tool_result blocks ahead of the other blocks in each user turn.

    The other blocks keep their relative order after the results. The input
    structure is never mutated.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    prepared_messages: list[Any] | None = None
    for message_index, message in enumerate(messages):
        message_dict = as_dict(message)
        if message_dict is None or message_dict.get("role") != "user":
            continue
        content = message_dict.get("content")
        if not isinstance(content, list):
            continue
        tool_results = [block for block in content if _is_tool_result_block(block)]
        if all(_is_tool_result_block(block) for block in content[: len(tool_results)]):
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        other_blocks = [block for block in content if not _is_tool_result_block(block)]
        prepared_messages[message_index] = {**message_dict, "content": [*tool_results, *other_blocks]}
    if prepared_messages is None:
        return request_kwargs
    return {**request_kwargs, "messages": prepared_messages}


# AGNO_COMPAT: Claude requests inline every non-text file as a base64 document.
# Reason: Agno 3.0.9 formats any file it does not map to a text source as a
# base64 document block, but Claude only accepts PDF there, so one .pptx, .xlsx,
# or .zip attachment fails the whole request with HTTP 400 ("document.source.
# base64.media_type: Input should be 'application/pdf'") on every tool round.
# Agno also never checks inline media against the provider's request body limit
# (Vertex rejects Claude requests over 30 MB, Bedrock over 20 MB).
# Upstream issue: No matching issue identified; unsupported Claude document types are untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno Claude formatter keeps files Claude cannot read
# inline out of document blocks; keep the size budget, which is a provider limit.
# Coverage: tests/test_claude_inline_media.py::test_unsupported_documents_become_text_notes_while_pdf_stays_inline;
# tests/test_claude_inline_media.py::test_vertex_claude_request_payload_describes_unsupported_documents;
# tests/test_claude_inline_media.py::test_inline_media_past_the_request_size_budget_becomes_a_text_note;
# tests/test_claude_inline_media.py::test_bedrock_uses_a_smaller_inline_media_budget_than_vertex.
_BASE64_DOCUMENT_MEDIA_TYPE = "application/pdf"
# Vertex rejects Claude requests over 30 MB, the direct API over 32 MB, and
# Bedrock over 20 MB. Leave room for the text, tools, and history around the
# inline base64 media.
MAX_INLINE_MEDIA_BYTES = 24_000_000
BEDROCK_MAX_INLINE_MEDIA_BYTES = 16_000_000


def request_kwargs_with_supported_inline_media(
    request_kwargs: dict[str, Any],
    *,
    max_inline_bytes: int,
) -> dict[str, Any]:
    """Replace inline media Claude would reject with a short text note.

    Base64 documents other than PDF are always replaced. Base64 images and PDFs
    that would push the request's inline media past ``max_inline_bytes`` are
    replaced too, keeping earlier blocks. The input structure is never mutated.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    prepared_messages: list[Any] | None = None
    inline_bytes = 0
    for message_index, message in enumerate(messages):
        message_dict = as_dict(message)
        if message_dict is None or message_dict.get("role") != "user":
            continue
        content = message_dict.get("content")
        if not isinstance(content, list):
            continue
        prepared_content, inline_bytes = _content_with_supported_inline_media(
            content,
            inline_bytes,
            max_inline_bytes,
        )
        if prepared_content is None:
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        prepared_messages[message_index] = {**message_dict, "content": prepared_content}
    if prepared_messages is None:
        return request_kwargs
    return {**request_kwargs, "messages": prepared_messages}


def _content_with_supported_inline_media(
    content: list[Any],
    inline_bytes: int,
    max_inline_bytes: int,
) -> tuple[list[Any] | None, int]:
    """Return one turn's content with notes in place of rejected media, or None when unchanged."""
    prepared_content: list[Any] | None = None
    for block_index, block in enumerate(content):
        note, inline_bytes = _inline_media_note(block, inline_bytes, max_inline_bytes)
        if note is None:
            continue
        if prepared_content is None:
            prepared_content = list(content)
        prepared_content[block_index] = note
    return prepared_content, inline_bytes


def _inline_media_note(
    block: object,
    inline_bytes: int,
    max_inline_bytes: int,
) -> tuple[dict[str, str] | None, int]:
    """Return a note replacing one base64 media block, or the request's new inline byte total."""
    block_dict = as_dict(block)
    source = as_dict(block_dict.get("source")) if block_dict is not None else None
    if block_dict is None or source is None or source.get("type") != "base64":
        return None, inline_bytes
    kind = block_dict.get("type")
    media_type = source.get("media_type")
    data_bytes = len(source.get("data") or "")
    if kind == "document" and media_type != _BASE64_DOCUMENT_MEDIA_TYPE:
        reason = f"Claude reads only PDF and plain-text documents inline, not {media_type}"
    elif inline_bytes + data_bytes > max_inline_bytes:
        reason = (
            f"its {data_bytes / 1_000_000:.1f} MB inline payload would push the request past the provider's size limit"
        )
    else:
        return None, inline_bytes + data_bytes
    text = (
        f"[Attached {kind} not sent inline: {reason}. Its content was not inspected. "
        "Use get_attachment without view to inspect or save it, then use other available tools.]"
    )
    return {"type": "text", "text": text}, inline_bytes


def _is_tool_result_block(block: object) -> bool:
    block_dict = as_dict(block)
    return block_dict is not None and block_dict.get("type") == "tool_result"


def _cited_text_block(block: object) -> dict[str, Any] | None:
    block_dict = as_dict(block)
    if block_dict is None or block_dict.get("type") != "text" or "citations" not in block_dict:
        return None
    return block_dict


# The request schema for replayed tool-search results accepts only these keys
# (ToolSearchToolResultBlockParam); response blocks additionally carry
# citations/parsed_output/text, which the API rejects as extra inputs.
_TOOL_SEARCH_RESULT_INPUT_KEYS = frozenset({"type", "tool_use_id", "content", "cache_control"})


def _tool_search_result_ids(content: list[Any]) -> set[str]:
    """Return tool-use IDs paired with search results in one message."""
    result_ids: set[str] = set()
    for block in content:
        block_dict = as_dict(block)
        if block_dict is None or block_dict.get("type") != TOOL_SEARCH_RESULT_BLOCK_TYPE:
            continue
        tool_use_id = block_dict.get("tool_use_id")
        if isinstance(tool_use_id, str):
            result_ids.add(tool_use_id)
    return result_ids


def _replay_safe_message_content(content: list[Any]) -> tuple[list[Any], bool]:
    """Strip response-only fields from replayed search results and drop search uses left without one."""
    prepared_content: list[Any] = []
    changed = False
    for block in content:
        block_dict = as_dict(block)
        if (
            block_dict is not None
            and block_dict.get("type") == TOOL_SEARCH_RESULT_BLOCK_TYPE
            and not block_dict.keys() <= _TOOL_SEARCH_RESULT_INPUT_KEYS
        ):
            prepared_content.append(
                {key: value for key, value in block_dict.items() if key in _TOOL_SEARCH_RESULT_INPUT_KEYS},
            )
            changed = True
        else:
            prepared_content.append(block)

    paired_result_ids = _tool_search_result_ids(prepared_content)
    sanitized_content: list[Any] = []
    for block in prepared_content:
        block_dict = as_dict(block)
        block_id = block_dict.get("id") if block_dict is not None else None
        if (
            block_dict is not None
            and block_dict.get("type") == SERVER_TOOL_USE_BLOCK_TYPE
            and block_dict.get("name") == TOOL_SEARCH_TOOL_NAME
            and (not isinstance(block_id, str) or block_id not in paired_result_ids)
        ):
            changed = True
            continue
        sanitized_content.append(block)
    return sanitized_content, changed


# AGNO_COMPAT: Claude history replays tool-search response blocks the request schema rejects.
# Reason: Agno 3.0.9 stores captured server-tool blocks with `model_dump()` and replays them verbatim, so a
# `tool_search_tool_result` keeps response-only fields (`citations`, `parsed_output`, `text`), and a search
# `server_tool_use` without its result is replayed too; either makes every later request fail with a 400.
# Upstream issue: https://github.com/agno-agi/agno/issues/8687, open; the same verbatim replay for
# code-execution citations, not tool-search blocks or unpaired search uses. Agno PR #6879 added the replay.
# Upstream PR: https://github.com/agno-agi/agno/pull/8686, open and partial; strips citations only from
# code-execution result blocks.
# Remove when: The pinned Agno replays tool-search blocks in request shape and drops unpaired search uses.
# Coverage: tests/test_extra_kwargs.py::test_replay_safe_tool_search_results_strips_response_only_fields;
# tests/test_extra_kwargs.py::test_replay_safe_tool_search_results_drops_only_orphaned_search_uses.
def request_kwargs_with_replay_safe_tool_search_results(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Repair replayed tool-search blocks before sending assistant history.

    Agno replays captured server-tool blocks verbatim in assistant history,
    and the SDK response block carries fields (``citations``, ``parsed_output``,
    ``text``) that the request schema rejects with a 400 ("Extra inputs are
    not permitted"). Once such a block is persisted, every later turn of that
    conversation replays it, so the thread stays broken until the block is
    sanitized here. Keys used for history identity (``type``, ``tool_use_id``)
    are preserved.

    Anthropic can also return a ``server_tool_use`` without its matching
    ``tool_search_tool_result`` when native search and client tools are called
    together. Replaying that orphan produces another 400. Valid pairs and other
    server-tool types remain intact. The input structure is never mutated.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    sanitized_messages = list(messages)
    changed = False
    for message_index, message in enumerate(sanitized_messages):
        message_dict = as_dict(message)
        content = message_dict.get("content") if message_dict is not None else None
        if message_dict is None or not isinstance(content, list):
            continue
        sanitized_content, content_changed = _replay_safe_message_content(content)
        if content_changed:
            sanitized_message = dict(message_dict)
            sanitized_message["content"] = sanitized_content
            sanitized_messages[message_index] = sanitized_message
            changed = True
    if not changed:
        return request_kwargs
    prepared_kwargs = dict(request_kwargs)
    prepared_kwargs["messages"] = sanitized_messages
    return prepared_kwargs
