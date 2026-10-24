"""Apply the Claude prompt-cache ladder to OpenRouter Chat Completions requests.

OpenRouter forwards ``cache_control`` markers on content parts and function tools to Anthropic-family upstreams,
which cache only up to explicit breakpoints, so only model ids routed to Anthropic are marked. Placement follows
:mod:`mindroom.providers.cache_ladder`: the shared system prefix, the two newest cacheable messages, and the last
tool, with one TTL and at most four markers.
"""

from __future__ import annotations

from typing import Any

from mindroom.providers.cache_ladder import (
    MESSAGE_RUNG_COUNT,
    mark_last_tool,
    mark_message_cache_rungs,
    prompt_cache_control,
    split_shared_system_prefix,
)

__all__ = ["openrouter_prompt_cache_control", "request_kwargs_with_prompt_cache"]

_ANTHROPIC_MODEL_PREFIXES = ("anthropic/", "~anthropic/")
_SYSTEM_ROLES = frozenset({"system", "developer"})
# Roles whose plain-string content becomes a markable text part when it takes a rung.
_RUNG_ROLES = frozenset({"user", "assistant", "tool"})


def openrouter_prompt_cache_control(
    model_id: str,
    *,
    cache_system_prompt: bool,
    extended_cache_time: bool,
) -> dict[str, str] | None:
    """Return the breakpoint marker for an Anthropic-routed model, or None when caching does not apply."""
    if not cache_system_prompt or not model_id.startswith(_ANTHROPIC_MODEL_PREFIXES):
        return None
    return prompt_cache_control(extended_cache_time=extended_cache_time)


def _with_marked_system_prompt(message: dict[str, Any], cache_control: dict[str, str]) -> dict[str, Any]:
    """Mark the system prompt, splitting the shared prefix from session context."""
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return message
    system_blocks = split_shared_system_prefix([{"type": "text", "text": content, "cache_control": cache_control}])
    return {**message, "content": system_blocks}


def _with_text_part_content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if message.get("role") not in _RUNG_ROLES or not isinstance(content, str) or not content:
        return message
    return {**message, "content": [{"type": "text", "text": content}]}


def _contains_cache_marker(value: object) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_contains_cache_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_cache_marker(item) for item in value)
    return False


def request_kwargs_with_prompt_cache(request_kwargs: dict[str, Any], cache_control: dict[str, str]) -> dict[str, Any]:
    """Return Chat Completions kwargs with Claude ladder breakpoints.

    A request that already carries any authored ``cache_control`` (on messages, tools, or the ``extra_body`` the
    SDK merges into the payload) is left untouched, so automatic markers never exceed the limit or mix TTLs with
    authored ones. Otherwise the system prompt, conversation rungs, and last tool take markers in that order, at
    most four.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list) or any(
        _contains_cache_marker(request_kwargs.get(key)) for key in ("messages", "tools", "extra_body")
    ):
        return request_kwargs
    # One system marker, the message rungs, and one tool marker always fit the four-marker limit.
    prepared = messages.copy()
    system_count = 0
    while system_count < len(prepared) and prepared[system_count].get("role") in _SYSTEM_ROLES:
        system_count += 1
    if system_count:
        prepared[0] = _with_marked_system_prompt(prepared[0], cache_control)

    conversation = prepared[system_count:]
    # Only messages that receive a marker keep the text-part form; others stay unchanged.
    candidates = [_with_text_part_content(message) for message in conversation]
    marked, _ = mark_message_cache_rungs(candidates, cache_control, MESSAGE_RUNG_COUNT)
    prepared[system_count:] = [
        original if marked_message is candidate else marked_message
        for original, candidate, marked_message in zip(conversation, candidates, marked, strict=True)
    ]
    prepared_kwargs = {**request_kwargs, "messages": prepared}

    marked_tools, tools_marked = mark_last_tool(request_kwargs.get("tools"), cache_control)
    if tools_marked:
        prepared_kwargs["tools"] = marked_tools
    return prepared_kwargs
