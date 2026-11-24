"""Apply the Claude prompt-cache ladder to OpenRouter Chat Completions requests.

OpenRouter forwards ``cache_control`` markers on content parts and function tools to Anthropic-family upstreams,
which cache only up to explicit breakpoints, so only model ids routed to Anthropic are marked. Placement follows
:mod:`mindroom.providers.cache_ladder`: the stable system prompt (the first system message), the two newest cacheable
messages, and the last tool, with one TTL and at most four markers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.providers.cache_ladder import (
    MESSAGE_RUNG_COUNT,
    mark_last_tool,
    mark_message_cache_rungs,
    prompt_cache_control,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "contains_cache_marker",
    "openrouter_prompt_cache_control",
    "request_kwargs_with_prompt_cache",
    "takes_automatic_markers",
]

_ANTHROPIC_MODEL_PREFIXES = ("anthropic/", "~anthropic/")
_SYSTEM_ROLES = frozenset({"system", "developer"})
# Roles whose non-empty string content is sent as a text part on the automatic cache path.
_TEXT_PART_ROLES = frozenset({"user", "assistant", "tool"})


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


def _with_marked_system_prompt(
    message: dict[str, Any],
    system: Sequence[str],
    cache_control: dict[str, str],
) -> dict[str, Any]:
    """Send the joined system prompt as one text part per system text, marking the first, the stable prefix.

    The turn's context follows in its own part, so it can change without invalidating the prefix.
    """
    if not isinstance(message.get("content"), str) or not system:
        return message
    marker = {"cache_control": dict(cache_control)}
    parts = [{"type": "text", "text": text, **(marker if index == 0 else {})} for index, text in enumerate(system)]
    return {**message, "content": parts}


def _with_text_part_content(message: dict[str, Any]) -> dict[str, Any]:
    """Use text parts so a message's wire form never depends on where cache markers sit."""
    content = message.get("content")
    if message.get("role") not in _TEXT_PART_ROLES or not isinstance(content, str) or not content:
        return message
    return {**message, "content": [{"type": "text", "text": content}]}


def contains_cache_marker(value: object) -> bool:
    """Return whether ``value`` holds an authored ``cache_control``, which turns the automatic markers off."""
    if isinstance(value, dict):
        return "cache_control" in value or any(contains_cache_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_cache_marker(item) for item in value)
    return False


def takes_automatic_markers(request_kwargs: dict[str, Any]) -> bool:
    """Return whether Chat Completions kwargs take the automatic markers: they have messages and author no marker.

    Any authored ``cache_control`` (on messages, tools, or the ``extra_body`` the SDK merges into the payload) turns
    them all off.
    """
    return isinstance(request_kwargs.get("messages"), list) and not any(
        contains_cache_marker(request_kwargs.get(key)) for key in ("messages", "tools", "extra_body")
    )


def request_kwargs_with_prompt_cache(
    request_kwargs: dict[str, Any],
    cache_control: dict[str, str],
    system: Sequence[str],
) -> dict[str, Any]:
    """Return Chat Completions kwargs with Claude ladder breakpoints; ``system`` holds the request's system texts.

    A request that does not take the automatic markers (``takes_automatic_markers``) is left untouched, so they never
    exceed the limit or mix TTLs with authored ones. Otherwise the system prompt, conversation rungs, and last tool
    take markers in that order, at most four.
    """
    if not takes_automatic_markers(request_kwargs):
        return request_kwargs
    messages = request_kwargs["messages"]
    # One system marker, the message rungs, and one tool marker always fit the four-marker limit.
    prepared = messages.copy()
    system_count = 0
    while system_count < len(prepared) and prepared[system_count].get("role") in _SYSTEM_ROLES:
        system_count += 1
    if system_count:
        prepared[0] = _with_marked_system_prompt(prepared[0], system, cache_control)

    # Keep the text-part form even after a marker moves so the cached prefix stays byte-identical.
    candidates = [_with_text_part_content(message) for message in prepared[system_count:]]
    prepared[system_count:] = mark_message_cache_rungs(candidates, cache_control, MESSAGE_RUNG_COUNT)
    prepared_kwargs = {**request_kwargs, "messages": prepared}

    marked_tools, tools_marked = mark_last_tool(request_kwargs.get("tools"), cache_control)
    if tools_marked:
        prepared_kwargs["tools"] = marked_tools
    return prepared_kwargs
