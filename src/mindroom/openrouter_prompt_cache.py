"""Apply the Claude prompt-cache ladder to OpenRouter Chat Completions requests.

OpenRouter forwards ``cache_control`` markers on Chat Completions content parts
and function tool definitions to Anthropic-family upstreams (Anthropic, Vertex,
Bedrock, and Azure), which only cache up to explicit breakpoints. Other
OpenRouter model families cache implicitly or use different markers, so this
module only marks requests whose model ID routes to Anthropic.

Breakpoint placement reuses :mod:`mindroom.claude_prompt_cache`: the shared
system prefix, the newest cacheable part of the two newest cacheable messages,
and the last tool definition, all with one TTL and at most four markers. This
module only translates between the OpenAI message shape and those rules.
"""

from __future__ import annotations

from typing import Any

from mindroom.claude_prompt_cache import (
    MAX_CACHE_MARKERS,
    MESSAGE_RUNG_COUNT,
    count_cache_markers,
    mark_last_tool,
    mark_message_cache_rungs,
    prompt_cache_control,
    split_shared_system_prefix,
)
from mindroom.hooks.enrichment import is_transient_context

_ANTHROPIC_MODEL_PREFIXES = ("anthropic/", "~anthropic/")
_SYSTEM_ROLES = frozenset({"system", "developer"})
# Roles whose plain-string content becomes a markable text part when it takes a rung.
_RUNG_ROLES = frozenset({"user", "assistant", "tool"})
# The tools array keeps one marker for itself; messages share the rest.
_TOOLS_MARKER_COUNT = 1


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


def _is_transient_user_message(message: dict[str, Any]) -> bool:
    return message.get("role") == "user" and is_transient_context(message.get("content"))


def _move_transient_context_after_user_turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move transient user messages after the durable user messages they precede.

    Anthropic merges consecutive user messages into one turn; keeping generated
    per-request context at the end of that turn keeps the durable prompt in
    the prefix that later requests replay.
    """
    reordered: list[dict[str, Any]] = []
    run_start = 0
    for index, message in enumerate([*messages, None]):
        if message is not None and message.get("role") == "user":
            continue
        user_run = messages[run_start:index]
        reordered.extend(user_message for user_message in user_run if not _is_transient_user_message(user_message))
        reordered.extend(user_message for user_message in user_run if _is_transient_user_message(user_message))
        if message is not None:
            reordered.append(message)
        run_start = index + 1
    return reordered


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


def formatted_messages_with_prompt_cache(
    messages: list[dict[str, Any]],
    cache_control: dict[str, str],
) -> list[dict[str, Any]]:
    """Return formatted Chat Completions messages with Claude ladder breakpoints."""
    prepared = _move_transient_context_after_user_turn(messages)
    system_count = 0
    while system_count < len(prepared) and prepared[system_count].get("role") in _SYSTEM_ROLES:
        system_count += 1
    if system_count:
        prepared[0] = _with_marked_system_prompt(prepared[0], cache_control)

    rung_budget = min(
        MESSAGE_RUNG_COUNT,
        MAX_CACHE_MARKERS - _TOOLS_MARKER_COUNT - count_cache_markers({"messages": prepared}),
    )
    if rung_budget <= 0:
        return prepared
    conversation = prepared[system_count:]
    # Only messages that receive a marker keep the text-part form; others stay unchanged.
    candidates = [_with_text_part_content(message) for message in conversation]
    marked, _ = mark_message_cache_rungs(candidates, cache_control, rung_budget)
    return [
        *prepared[:system_count],
        *(
            original if marked_message is candidate else marked_message
            for original, candidate, marked_message in zip(conversation, candidates, marked, strict=True)
        ),
    ]


def request_params_with_tools_cache_breakpoint(
    request_params: dict[str, Any],
    cache_control: dict[str, str],
) -> dict[str, Any]:
    """Mark the last tool definition so the tools prefix caches independently."""
    marked_tools, tools_marked = mark_last_tool(request_params.get("tools"), cache_control)
    if not tools_marked:
        return request_params
    return {**request_params, "tools": marked_tools}
