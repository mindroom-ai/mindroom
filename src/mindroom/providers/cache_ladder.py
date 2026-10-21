"""The Claude prompt-cache ladder: breakpoint markers, message rungs, the tools marker, and the shared system prefix.

Anthropic prompt caching reuses the request prefix (tools, system, messages) up to explicit ``cache_control``
breakpoints, and only probes the last twenty or so blocks before each one. The ladder marks the newest
cacheable content block, one earlier fallback boundary, the last tool definition, and the shared system
prefix ahead of the session context, all under one TTL, within the API limit of four markers.

The owned Claude adapter (``providers/anthropic/prompt_cache.py``), the OpenRouter Claude ladder
(``providers/openai/openrouter_cache.py``), and the Agno-path ``claude_prompt_cache`` compose these primitives,
so neither provider family depends on the other.
"""

from __future__ import annotations

from typing import Any

from mindroom.hooks.enrichment import is_transient_context
from mindroom.providers.claude_wire_blocks import TOOL_SEARCH_TOOL_TYPE, as_dict
from mindroom.system_prompt import SESSION_CONTEXT_BOUNDARY

__all__ = [
    "MAX_CACHE_MARKERS",
    "MESSAGE_RUNG_COUNT",
    "has_cache_marker",
    "mark_last_tool",
    "mark_message_cache_rungs",
    "prompt_cache_control",
    "split_shared_system_prefix",
]

# The Anthropic API allows at most four cache_control markers per request.
MAX_CACHE_MARKERS = 4
# Newest cacheable block plus one fallback boundary in an earlier message.
MESSAGE_RUNG_COUNT = 2
_MARKABLE_BLOCK_TYPES = frozenset({"text", "tool_result", "document", "image"})


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
