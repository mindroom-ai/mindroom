"""The wire-level request preparation of one Claude request: history repairs, deferred tools, and cache breakpoints.

The cache breakpoints come from the ladder primitives in ``providers/cache_ladder.py``, which the OpenRouter
Claude ladder shares. The repairs run on every request, cached or not, because an already-stored history can
carry blocks the request schema rejects. ``claude_loaded_tool_names`` reads which deferred tools earlier tool
searches loaded, which the mid-turn history budget counts as sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    without_unavailable_tool_references,
)
from mindroom.providers.cache_ladder import (
    MAX_CACHE_MARKERS,
    MESSAGE_RUNG_COUNT,
    has_cache_marker,
    mark_last_tool,
    mark_message_cache_rungs,
    prompt_cache_control,
    split_shared_system_prefix,
)
from mindroom.providers.claude_wire_blocks import (
    TOOL_SEARCH_RESULT_BLOCK_TYPE,
    TOOL_SEARCH_TOOL_NAME,
    TOOL_SEARCH_TOOL_TYPE,
    as_dict,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolChoice
    from mindroom.providers.anthropic.settings import ClaudeRouteKind

__all__ = ["claude_loaded_tool_names", "native_tool_search_supported", "prepare_request_body"]

_NATIVE_TOOL_SEARCH_ROUTES: frozenset[ClaudeRouteKind] = frozenset({"anthropic", "vertexai_claude"})


def native_tool_search_supported(route: ClaudeRouteKind, model_id: str) -> bool:
    """Return whether one Claude route and model support server-side tool search.

    Every Claude model since Opus 4.5 / Sonnet 4.5 / Haiku 4.5 supports the search tool, so gating denylists
    the closed pre-4.5 set and new model releases take the native path without a code change.
    """
    return route in _NATIVE_TOOL_SEARCH_ROUTES and not model_id.startswith(TOOL_SEARCH_UNSUPPORTED_MODEL_ID_PREFIXES)


def claude_loaded_tool_names(messages: Sequence[Message]) -> frozenset[str]:
    """Return the tool names whose definitions tool searches in these messages loaded into Claude's context."""
    names: set[str] = set()
    for message in messages:
        blocks = message.provider_data.get("server_tool_blocks") if message.provider_data else None
        for block in blocks if isinstance(blocks, list) else ():
            block_dict = as_dict(block)
            content = as_dict(block_dict.get("content")) if block_dict is not None else None
            if block_dict is None or block_dict.get("type") != TOOL_SEARCH_RESULT_BLOCK_TYPE or content is None:
                continue
            references = content.get("tool_references")
            for reference in references if isinstance(references, list) else ():
                reference_dict = as_dict(reference)
                if reference_dict is not None and isinstance(name := reference_dict.get("tool_name"), str):
                    names.add(name)
    return frozenset(names)


def _count_cache_markers(body: dict[str, Any]) -> int:
    """Count the ``cache_control`` markers the body already carries."""
    count = sum(1 for block in body.get("system") or [] if has_cache_marker(block))
    count += sum(1 for tool in body.get("tools") or [] if has_cache_marker(tool))
    count += sum(1 for message in body["messages"] for block in message["content"] if has_cache_marker(block))
    return count


def _with_prompt_cache_ladder(body: dict[str, Any], cache_control: dict[str, str]) -> dict[str, Any]:
    """Add the message rungs, then the tools marker, within the API's marker budget."""
    marker_budget = MAX_CACHE_MARKERS - _count_cache_markers(body)
    if marker_budget <= 0:
        return body
    prepared = dict(body)
    prepared["messages"], markers_added = mark_message_cache_rungs(
        body["messages"],
        cache_control,
        min(MESSAGE_RUNG_COUNT, marker_budget),
    )
    marker_budget -= markers_added
    if marker_budget > 0:
        marked_tools, tools_marked = mark_last_tool(prepared.get("tools"), cache_control)
        if tools_marked:
            prepared["tools"] = marked_tools
    return prepared


def _with_system_prompt(body: dict[str, Any], system: str, cache_control: dict[str, str] | None) -> dict[str, Any]:
    """Send the conversation's system text as one block, marked when the model caches its prompt.

    Without system text, a ``system`` the raw request overrides set goes as authored.
    """
    if not system:
        return body
    marker = {} if cache_control is None else {"cache_control": dict(cache_control)}
    return {**body, "system": [{"text": system, "type": "text", **marker}]}


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


def _with_tool_choice(body: dict[str, Any], tool_choice: ToolChoice | None) -> dict[str, Any]:
    """Turn tool selection off for a ``"none"`` request that offers tools; the API rejects a choice without them.

    The SDK merges ``extra_body`` over the named fields, so an authored ``extra_body.tool_choice`` is replaced too.
    """
    if tool_choice is None or not body.get("tools"):
        return body
    choice = {"type": "none"}
    prepared = {**body, "tool_choice": choice}
    extra_body = body.get("extra_body")
    if isinstance(extra_body, dict) and "tool_choice" in extra_body:
        prepared["extra_body"] = {**extra_body, "tool_choice": choice}
    return prepared


def prepare_request_body(
    body: dict[str, Any],
    *,
    system: str,
    route: ClaudeRouteKind,
    deferred_tool_names: frozenset[str],
    tool_choice: ToolChoice | None,
    extended_cache_time: bool,
    cache_system_prompt: bool,
) -> dict[str, Any]:
    """Return the request body as it goes on the wire.

    ``body`` is the request the adapter built around ``format_claude_messages``, and ``system`` is that
    conversation's system text. Repairs come first, then the deferred tool search, the tool choice, the system
    prompt, and finally the cache breakpoints when the model caches its prompt. The input body is never mutated.
    """
    prepared = with_replay_safe_tool_search_results(body)
    prepared = without_unavailable_tool_references(prepared)
    prepared = without_replayed_citations(prepared)
    prepared = with_leading_tool_results(prepared)
    prepared = with_supported_inline_media(
        prepared,
        max_inline_bytes=BEDROCK_MAX_INLINE_MEDIA_BYTES if route == "bedrock_claude" else MAX_INLINE_MEDIA_BYTES,
        max_image_bytes=MAX_INLINE_IMAGE_BYTES if route == "anthropic" else PARTNER_MAX_INLINE_IMAGE_BYTES,
    )
    prepared = _with_deferred_tool_search(prepared, deferred_tool_names)
    prepared = _with_tool_choice(prepared, tool_choice)
    cache_control = prompt_cache_control(extended_cache_time=extended_cache_time) if cache_system_prompt else None
    prepared = _with_system_prompt(prepared, system, cache_control)
    if cache_control is not None:
        prepared = _with_shared_system_prefix(prepared)
        prepared = _with_prompt_cache_ladder(prepared, cache_control)
    return prepared
