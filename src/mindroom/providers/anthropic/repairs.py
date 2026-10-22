"""Replay-safe transforms of one Claude request body.

Stored assistant turns echo the content blocks the API produced, and some of those blocks carry
response-only fields or depend on request-scoped state that a later request cannot provide. The API
rejects such a request with HTTP 400, so every later turn of that thread would fail; these transforms
repair the wire body instead, which also heals histories stored before the rules existed. Inline media
Claude cannot read, or past the provider's size limits, becomes a short text note for the same reason.
Each transform reads the body ``format_claude_messages`` built, whose ``messages`` are role dicts with a block
list as ``content``; a block is a stored dict or one of the SDK's block models. None of the transforms mutate
their input.
"""

from __future__ import annotations

import json
from typing import Any

from mindroom.providers.claude_wire_blocks import (
    SERVER_TOOL_USE_BLOCK_TYPE,
    TOOL_SEARCH_RESULT_BLOCK_TYPE,
    TOOL_SEARCH_TOOL_NAME,
    as_dict,
)

_BASE64_DOCUMENT_MEDIA_TYPE = "application/pdf"
# Vertex rejects Claude requests over 30 MB, the direct API over 32 MB, and Bedrock over 20 MB. Leave room
# for the text, tools, and history around the inline media.
MAX_INLINE_MEDIA_BYTES = 24_000_000
BEDROCK_MAX_INLINE_MEDIA_BYTES = 16_000_000
# Claude rejects a base64 image over 10 MB on the direct API and over 5 MB on Bedrock and Vertex.
MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
PARTNER_MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
# The request schema for replayed tool-search results accepts only these keys (ToolSearchToolResultBlockParam);
# response blocks additionally carry citations, parsed_output, and text, which the API rejects as extra inputs.
_TOOL_SEARCH_RESULT_INPUT_KEYS = frozenset({"type", "tool_use_id", "content", "cache_control"})


def _with_messages(body: dict[str, Any], messages: list[Any] | None) -> dict[str, Any]:
    return body if messages is None else {**body, "messages": messages}


def without_replayed_citations(body: dict[str, Any]) -> dict[str, Any]:
    """Drop response citations from replayed assistant text blocks, keeping their text.

    Citations are response annotations: a null ``document_title`` loses a required field on replay, and
    document citations point at request-scoped document indexes whose documents are not replayed. The model
    does not need them to read its earlier answer.
    """
    messages = body["messages"]
    prepared_messages: list[Any] | None = None
    for message_index, message in enumerate(messages):
        content = message["content"]
        if message["role"] != "assistant" or not any(_cited_text_block(block) is not None for block in content):
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        prepared_content = [
            block
            if (cited := _cited_text_block(block)) is None
            else {k: v for k, v in cited.items() if k != "citations"}
            for block in content
        ]
        prepared_messages[message_index] = {**message, "content": prepared_content}
    return _with_messages(body, prepared_messages)


def with_leading_tool_results(body: dict[str, Any]) -> dict[str, Any]:
    """Move ``tool_result`` blocks ahead of the other blocks in each user turn.

    The API only pairs tool results that open the turn, so a resumed result stored after a media
    follow-up message, or a turn stored by a provider that accepts the other order, replays only this way.
    The other blocks keep their relative order after the results.
    """
    messages = body["messages"]
    prepared_messages: list[Any] | None = None
    for message_index, message in enumerate(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        tool_results = [block for block in content if _is_tool_result_block(block)]
        if all(_is_tool_result_block(block) for block in content[: len(tool_results)]):
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        other_blocks = [block for block in content if not _is_tool_result_block(block)]
        prepared_messages[message_index] = {**message, "content": [*tool_results, *other_blocks]}
    return _with_messages(body, prepared_messages)


def with_supported_inline_media(body: dict[str, Any], *, max_inline_bytes: int, max_image_bytes: int) -> dict[str, Any]:
    """Replace inline media Claude would reject with a short text note.

    Base64 documents other than PDF and base64 images over ``max_image_bytes`` are always replaced. Images
    and documents that would push the request's inline media past ``max_inline_bytes`` are replaced too,
    keeping earlier blocks.
    """
    messages = body["messages"]
    prepared_messages: list[Any] | None = None
    inline_bytes = 0
    for message_index, message in enumerate(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        prepared_content: list[Any] | None = None
        for block_index, block in enumerate(content):
            note, inline_bytes = _inline_media_note(block, inline_bytes, max_inline_bytes, max_image_bytes)
            if note is None:
                continue
            if prepared_content is None:
                prepared_content = list(content)
            prepared_content[block_index] = note
        if prepared_content is None:
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        prepared_messages[message_index] = {**message, "content": prepared_content}
    return _with_messages(body, prepared_messages)


def _inline_media_note(
    block: object,
    inline_bytes: int,
    max_inline_bytes: int,
    max_image_bytes: int,
) -> tuple[dict[str, str] | None, int]:
    """Return a note replacing one inline media block, or the request's new inline byte total."""
    block_dict = as_dict(block)
    source = as_dict(block_dict.get("source")) if block_dict is not None else None
    source_type = source.get("type") if source is not None else None
    if block_dict is None or source is None or source_type not in {"base64", "text"}:
        return None, inline_bytes
    kind = block_dict.get("type")
    media_type = source.get("media_type")
    data = source.get("data") or ""
    # The SDK sends text as escaped JSON, where a newline or quote takes two bytes.
    data_bytes = len(json.dumps(data, ensure_ascii=False).encode()) if source_type == "text" else len(data)
    if kind == "document" and source_type == "base64" and media_type != _BASE64_DOCUMENT_MEDIA_TYPE:
        reason = f"Claude reads only PDF and plain-text documents inline, not {media_type}"
    elif kind == "image" and data_bytes > max_image_bytes:
        reason = f"its {data_bytes / 1_000_000:.1f} MB inline payload exceeds the provider's per-image limit"
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


def with_replay_safe_tool_search_results(body: dict[str, Any]) -> dict[str, Any]:
    """Replay tool-search blocks in request shape and drop search uses left without their result.

    Stored search results keep response-only fields the request schema rejects, and a search
    ``server_tool_use`` can arrive without its ``tool_search_tool_result`` when native search and client
    tools are called together; either makes every later request fail. Valid pairs and other server-tool
    types stay intact, and the identity keys (``type``, ``tool_use_id``) are preserved.
    """
    messages = body["messages"]
    prepared_messages: list[Any] | None = None
    for message_index, message in enumerate(messages):
        sanitized_content, changed = _replay_safe_message_content(message["content"])
        if not changed:
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        prepared_messages[message_index] = {**message, "content": sanitized_content}
    return _with_messages(body, prepared_messages)


def _replay_safe_message_content(content: list[Any]) -> tuple[list[Any], bool]:
    prepared_content: list[Any] = []
    changed = False
    for block in content:
        block_dict = as_dict(block)
        if (
            block_dict is not None
            and block_dict.get("type") == TOOL_SEARCH_RESULT_BLOCK_TYPE
            and not block_dict.keys() <= _TOOL_SEARCH_RESULT_INPUT_KEYS
        ):
            prepared_content.append({k: v for k, v in block_dict.items() if k in _TOOL_SEARCH_RESULT_INPUT_KEYS})
            changed = True
        else:
            prepared_content.append(block)
    paired_result_ids = {
        block_dict["tool_use_id"]
        for block in prepared_content
        if (block_dict := as_dict(block)) is not None
        and block_dict.get("type") == TOOL_SEARCH_RESULT_BLOCK_TYPE
        and isinstance(block_dict.get("tool_use_id"), str)
    }
    sanitized_content: list[Any] = []
    for block in prepared_content:
        block_dict = as_dict(block)
        if (
            block_dict is not None
            and block_dict.get("type") == SERVER_TOOL_USE_BLOCK_TYPE
            and block_dict.get("name") == TOOL_SEARCH_TOOL_NAME
            and block_dict.get("id") not in paired_result_ids
        ):
            changed = True
            continue
        sanitized_content.append(block)
    return sanitized_content, changed


def _is_tool_result_block(block: object) -> bool:
    block_dict = as_dict(block)
    return block_dict is not None and block_dict.get("type") == "tool_result"


def _cited_text_block(block: object) -> dict[str, Any] | None:
    block_dict = as_dict(block)
    if block_dict is None or block_dict.get("type") != "text" or "citations" not in block_dict:
        return None
    return block_dict


__all__ = [
    "BEDROCK_MAX_INLINE_MEDIA_BYTES",
    "MAX_INLINE_IMAGE_BYTES",
    "MAX_INLINE_MEDIA_BYTES",
    "PARTNER_MAX_INLINE_IMAGE_BYTES",
    "with_leading_tool_results",
    "with_replay_safe_tool_search_results",
    "with_supported_inline_media",
    "without_replayed_citations",
]
