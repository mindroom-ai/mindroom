"""The wire-level request preparation of one Claude request: history repairs, deferred tools, and cache breakpoints.

Anthropic prompt caching reuses the request prefix (tools, system, messages) up to explicit ``cache_control``
breakpoints, and only probes the last twenty or so blocks before each one. The ladder marks the newest
cacheable content block, one earlier fallback boundary, the last tool definition, and the shared system
prefix ahead of the session context, all under one TTL, within the API limit of four markers.

The ladder primitives live here; the Agno-path ``claude_prompt_cache`` and the OpenRouter Claude ladder import
them. The repairs run on every request, cached or not, because an already-stored history can carry blocks the
request schema rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.hooks.enrichment import is_transient_context
from mindroom.model_defaults import TOOL_SEARCH_UNSUPPORTED_MODEL_ID_PREFIXES
from mindroom.providers.anthropic.repairs import (
    BEDROCK_MAX_INLINE_MEDIA_BYTES,
    MAX_INLINE_IMAGE_BYTES,
    MAX_INLINE_MEDIA_BYTES,
    PARTNER_MAX_INLINE_IMAGE_BYTES,
    with_leading_tool_results,
    with_replay_safe_tool_search_results,
    with_supported_inline_media,
    without_replayed_citations,
)
from mindroom.providers.claude_wire_blocks import (
    TOOL_SEARCH_RESULT_BLOCK_TYPE,
    TOOL_SEARCH_TOOL_NAME,
    TOOL_SEARCH_TOOL_TYPE,
    as_dict,
)
from mindroom.system_prompt import SESSION_CONTEXT_BOUNDARY

if TYPE_CHECKING:
    from mindroom.llm.provider import ToolChoice
    from mindroom.providers.anthropic.settings import ClaudeRouteKind

__all__ = [
    "MAX_CACHE_MARKERS",
    "MESSAGE_RUNG_COUNT",
    "has_cache_marker",
    "mark_last_tool",
    "mark_message_cache_rungs",
    "native_tool_search_supported",
    "prepare_request_body",
    "prompt_cache_control",
    "split_shared_system_prefix",
]

# The Anthropic API allows at most four cache_control markers per request.
MAX_CACHE_MARKERS = 4
# Newest cacheable block plus one fallback boundary in an earlier message.
MESSAGE_RUNG_COUNT = 2
_MARKABLE_BLOCK_TYPES = frozenset({"text", "tool_result", "document", "image"})
_NATIVE_TOOL_SEARCH_PROVIDERS = frozenset({"anthropic", "vertexai_claude"})
_TOOL_CHOICES = {"auto": {"type": "auto"}, "none": {"type": "none"}, "required": {"type": "any"}}


def native_tool_search_supported(provider: str, model_id: str) -> bool:
    """Return whether one authored provider/model pair supports server-side tool search.

    Every Claude model since Opus 4.5 / Sonnet 4.5 / Haiku 4.5 supports the search tool, so gating denylists
    the closed pre-4.5 set and new model releases take the native path without a code change.
    """
    canonical_provider = provider.strip().lower().replace("-", "_")
    if canonical_provider not in _NATIVE_TOOL_SEARCH_PROVIDERS:
        return False
    return not model_id.startswith(TOOL_SEARCH_UNSUPPORTED_MODEL_ID_PREFIXES)


def prompt_cache_control(*, extended_cache_time: bool = False) -> dict[str, str]:
    """Return the cache_control payload for one breakpoint marker."""
    cache_control: dict[str, str] = {"type": "ephemeral"}
    if extended_cache_time:
        cache_control["ttl"] = "1h"
    return cache_control


def has_cache_marker(block: object) -> bool:
    """Return whether a wire-format block or tool already carries a ``cache_control`` marker."""
    block_dict = as_dict(block)
    return block_dict is not None and block_dict.get("cache_control") is not None


def _is_transient_context_block(block: object) -> bool:
    block_dict = as_dict(block)
    return block_dict is not None and block_dict.get("type") == "text" and is_transient_context(block_dict.get("text"))


def _is_markable_block(block: object) -> bool:
    """Return whether a wire-format content block may carry cache_control."""
    block_dict = as_dict(block)
    if block_dict is None:
        # SDK block objects (assistant text/tool_use/thinking) are rebuilt on every request; ladder on dict
        # blocks only.
        return False
    block_type = block_dict.get("type")
    if block_type not in _MARKABLE_BLOCK_TYPES:
        return False
    if block_type == "text":
        return bool(block_dict.get("text")) and not _is_transient_context_block(block)
    if block_type == "tool_result":
        return bool(block_dict.get("content"))
    return True


def mark_message_cache_rungs(
    messages: list[Any],
    cache_control: dict[str, str],
    rung_budget: int,
) -> tuple[list[Any], int]:
    """Mark the newest cacheable block of up to ``rung_budget`` trailing messages.

    Returns the (copied) message list and the number of newly added markers. At most one block per message is
    marked, scanning from the end of the request, so consecutive requests in a tool loop always share the
    previous request's newest boundary. A block that already carries a marker occupies a rung but is not counted
    as newly added (its marker was already deducted from the caller's overall budget). The input structure is
    never mutated.
    """
    marked_messages = list(messages)
    rungs_occupied = 0
    markers_added = 0
    for message_index in range(len(marked_messages) - 1, -1, -1):
        if rungs_occupied >= rung_budget:
            break
        message_dict = as_dict(marked_messages[message_index])
        content = message_dict.get("content") if message_dict is not None else None
        if message_dict is None or not isinstance(content, list):
            continue
        for block_index in range(len(content) - 1, -1, -1):
            block = content[block_index]
            if has_cache_marker(block):
                rungs_occupied += 1
                break
            if not _is_markable_block(block):
                continue
            marked_block = dict(as_dict(block) or {})
            marked_block["cache_control"] = dict(cache_control)
            marked_content = list(content)
            marked_content[block_index] = marked_block
            marked_message = dict(message_dict)
            marked_message["content"] = marked_content
            marked_messages[message_index] = marked_message
            rungs_occupied += 1
            markers_added += 1
            break
    return marked_messages, markers_added


def mark_last_tool(tools: object, cache_control: dict[str, str]) -> tuple[object, int]:
    """Mark the last non-deferred tool so the tools prefix caches independently.

    Deferred tools may not carry ``cache_control`` (the API returns a 400) and sort after all non-deferred
    tools, so the marker position is byte-stable. The injected search tool is never marked either (whether the
    API accepts a marker on that server-tool type is unverified), so an all-deferred tool surface sends no tools
    marker; the tools prefix still caches under the system-prompt breakpoint because tools render ahead of system.
    """
    if not isinstance(tools, list) or not tools:
        return tools, 0
    for tool_index in range(len(tools) - 1, -1, -1):
        tool_dict = as_dict(tools[tool_index])
        if tool_dict is not None and (
            tool_dict.get("defer_loading") is True or tool_dict.get("type") == TOOL_SEARCH_TOOL_TYPE
        ):
            continue
        if tool_dict is None or has_cache_marker(tool_dict):
            return tools, 0
        marked_tools = list(tools)
        marked_tool = dict(tool_dict)
        marked_tool["cache_control"] = dict(cache_control)
        marked_tools[tool_index] = marked_tool
        return marked_tools, 1
    return tools, 0


def split_shared_system_prefix(system: list[Any]) -> list[Any]:
    """Move a marked first system text block's cache marker to the shared instructions.

    The session-context suffix becomes a separate unmarked block, so dates and learning can change without
    invalidating the shared prefix. Returns the input list unchanged when there is no marked boundary to split.
    """
    if not system:
        return system
    first_block = system[0]
    if not isinstance(first_block, dict) or first_block.get("type") != "text":
        return system
    text = first_block.get("text")
    if not isinstance(text, str) or "cache_control" not in first_block:
        return system
    shared_text, boundary, session_text = text.partition(SESSION_CONTEXT_BOUNDARY)
    if not boundary or not shared_text.strip():
        return system

    shared_block = {**first_block, "text": shared_text}
    session_block = {key: value for key, value in first_block.items() if key != "cache_control"}
    session_block["text"] = boundary + session_text
    return [shared_block, session_block, *system[1:]]


def _count_cache_markers(body: dict[str, Any]) -> int:
    """Count the ``cache_control`` markers the body already carries."""
    count = sum(1 for block in body.get("system") or [] if has_cache_marker(block))
    count += sum(1 for tool in body.get("tools") or [] if has_cache_marker(tool))
    for message in body.get("messages") or []:
        content = (as_dict(message) or {}).get("content")
        if isinstance(content, list):
            count += sum(1 for block in content if has_cache_marker(block))
    return count


def _with_prompt_cache_ladder(body: dict[str, Any], cache_control: dict[str, str]) -> dict[str, Any]:
    """Add the message rungs, then the tools marker, within the API's marker budget."""
    marker_budget = MAX_CACHE_MARKERS - _count_cache_markers(body)
    if marker_budget <= 0:
        return body
    prepared = dict(body)
    messages = prepared.get("messages")
    if isinstance(messages, list) and messages:
        marked_messages, markers_added = mark_message_cache_rungs(
            messages,
            cache_control,
            min(MESSAGE_RUNG_COUNT, marker_budget),
        )
        prepared["messages"] = marked_messages
        marker_budget -= markers_added
    if marker_budget > 0:
        marked_tools, tools_marked = mark_last_tool(prepared.get("tools"), cache_control)
        if tools_marked:
            prepared["tools"] = marked_tools
    return prepared


def _with_shared_system_prefix(body: dict[str, Any]) -> dict[str, Any]:
    """Keep the shared instructions cacheable independently of the session context that follows them."""
    system = body.get("system")
    if not isinstance(system, list):
        return body
    split_system = split_shared_system_prefix(system)
    return body if split_system is system else {**body, "system": split_system}


def _with_deferred_tool_search(body: dict[str, Any], deferred_tool_names: frozenset[str]) -> dict[str, Any]:
    """Tag deferred tools with ``defer_loading`` and put the server-side regex search tool first.

    Every deferred definition still ships; the API keeps deferred schemas out of the rendered prompt prefix and
    expands them when the model searches. The array order is deterministic (search tool, the other tools, then
    the deferred tools sorted by name) so the cached prefix stays byte-stable. A deferred tool may not carry a
    cache marker, which is why the ladder's tools marker targets the last non-deferred tool.
    """
    tools = body.get("tools")
    if not deferred_tool_names or not isinstance(tools, list):
        return body
    kept: list[Any] = []
    deferred: list[dict[str, Any]] = []
    for tool in tools:
        tool_dict = as_dict(tool)
        if tool_dict is not None and tool_dict.get("name") in deferred_tool_names:
            deferred.append(
                {key: value for key, value in tool_dict.items() if key != "cache_control"} | {"defer_loading": True},
            )
        else:
            kept.append(tool)
    if not deferred:
        return body
    deferred.sort(key=lambda tool: str(tool.get("name")))
    return {**body, "tools": [{"type": TOOL_SEARCH_TOOL_TYPE, "name": TOOL_SEARCH_TOOL_NAME}, *kept, *deferred]}


def _request_tool_names(body: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        name
        for tool in body.get("tools") or []
        if (tool_dict := as_dict(tool)) is not None and isinstance(name := tool_dict.get("name"), str)
    )


def _search_result_with_available_references(
    block: dict[str, Any],
    available: frozenset[str],
) -> dict[str, Any] | None:
    """Return one search result without references to unavailable tools, or None when all are available."""
    result = as_dict(block.get("content"))
    references = result.get("tool_references") if result is not None else None
    if result is None or not isinstance(references, list):
        return None
    kept = [
        reference
        for reference in references
        if (reference_dict := as_dict(reference)) is not None and reference_dict.get("tool_name") in available
    ]
    if len(kept) == len(references):
        return None
    return {**block, "content": {**result, "tool_references": kept}}


def _content_without_unavailable_references(content: list[Any], available: frozenset[str]) -> list[Any] | None:
    """Return one turn's blocks with stale search references removed, or None when nothing changed."""
    prepared: list[Any] | None = None
    for index, block in enumerate(content):
        block_dict = as_dict(block)
        if block_dict is None or block_dict.get("type") != TOOL_SEARCH_RESULT_BLOCK_TYPE:
            continue
        filtered = _search_result_with_available_references(block_dict, available)
        if filtered is None:
            continue
        if prepared is None:
            prepared = list(content)
        prepared[index] = filtered
    return prepared


def _without_unavailable_tool_references(body: dict[str, Any]) -> dict[str, Any]:
    """Drop replayed tool-search references to tools absent from this request.

    A search result can reference tools a later request no longer offers, which Claude rejects. A search left
    without references stays as an empty result, like a search that matched nothing, so the signed thinking
    around it is not moved.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body
    available = _request_tool_names(body)
    prepared_messages: list[Any] | None = None
    for message_index, message in enumerate(messages):
        message_dict = as_dict(message)
        content = message_dict.get("content") if message_dict is not None else None
        if message_dict is None or not isinstance(content, list):
            continue
        prepared_content = _content_without_unavailable_references(content, available)
        if prepared_content is None:
            continue
        if prepared_messages is None:
            prepared_messages = list(messages)
        prepared_messages[message_index] = {**message_dict, "content": prepared_content}
    return body if prepared_messages is None else {**body, "messages": prepared_messages}


def _with_tool_choice(body: dict[str, Any], tool_choice: ToolChoice | None) -> dict[str, Any]:
    """Apply the request's tool choice when the request offers tools; the API rejects a choice without them.

    The SDK merges ``extra_body`` over the named fields, so an authored ``extra_body.tool_choice`` is replaced too.
    """
    if tool_choice is None or not body.get("tools"):
        return body
    choice = _TOOL_CHOICES[tool_choice] if isinstance(tool_choice, str) else dict(tool_choice)
    prepared = {**body, "tool_choice": choice}
    extra_body = body.get("extra_body")
    if isinstance(extra_body, dict) and "tool_choice" in extra_body:
        prepared["extra_body"] = {**extra_body, "tool_choice": choice}
    return prepared


def prepare_request_body(
    body: dict[str, Any],
    *,
    route: ClaudeRouteKind,
    deferred_tool_names: frozenset[str],
    tool_choice: ToolChoice | None,
    extended_cache_time: bool,
    cache_system_prompt: bool,
) -> dict[str, Any]:
    """Return the request body as it goes on the wire.

    Repairs come first, then the deferred tool search, the tool choice, and finally the cache breakpoints when
    the model caches its prompt. The input body is never mutated.
    """
    prepared = with_replay_safe_tool_search_results(body)
    prepared = _without_unavailable_tool_references(prepared)
    prepared = without_replayed_citations(prepared)
    prepared = with_leading_tool_results(prepared)
    prepared = with_supported_inline_media(
        prepared,
        max_inline_bytes=BEDROCK_MAX_INLINE_MEDIA_BYTES if route == "bedrock_claude" else MAX_INLINE_MEDIA_BYTES,
        max_image_bytes=MAX_INLINE_IMAGE_BYTES if route == "anthropic" else PARTNER_MAX_INLINE_IMAGE_BYTES,
    )
    prepared = _with_deferred_tool_search(prepared, deferred_tool_names)
    prepared = _with_tool_choice(prepared, tool_choice)
    if cache_system_prompt:
        cache_control = prompt_cache_control(extended_cache_time=extended_cache_time)
        prepared = _with_shared_system_prefix(prepared)
        prepared = _with_prompt_cache_ladder(prepared, cache_control)
    return prepared
