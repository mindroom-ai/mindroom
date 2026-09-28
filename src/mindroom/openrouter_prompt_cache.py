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
The ladder runs on the complete request at the SDK client, so authored
markers on messages and tools share one budget with the automatic markers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _request_kwargs_with_prompt_cache(
    request_kwargs: dict[str, Any],
    cache_control: dict[str, str],
) -> dict[str, Any]:
    """Return Chat Completions kwargs with Claude ladder breakpoints within one request-wide budget.

    Existing markers on messages and tools count first; the system prompt,
    conversation rungs, and last tool then take markers in that order until
    the API limit is reached.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    prepared = _move_transient_context_after_user_turn(messages)
    budget = MAX_CACHE_MARKERS - count_cache_markers({"messages": prepared, "tools": request_kwargs.get("tools")})
    system_count = 0
    while system_count < len(prepared) and prepared[system_count].get("role") in _SYSTEM_ROLES:
        system_count += 1
    if system_count and budget > 0:
        marked_system = _with_marked_system_prompt(prepared[0], cache_control)
        budget -= marked_system is not prepared[0]
        prepared[0] = marked_system

    if budget > 0:
        conversation = prepared[system_count:]
        # Only messages that receive a marker keep the text-part form; others stay unchanged.
        candidates = [_with_text_part_content(message) for message in conversation]
        marked, markers_added = mark_message_cache_rungs(candidates, cache_control, min(MESSAGE_RUNG_COUNT, budget))
        budget -= markers_added
        prepared[system_count:] = [
            original if marked_message is candidate else marked_message
            for original, candidate, marked_message in zip(conversation, candidates, marked, strict=True)
        ]
    prepared_kwargs = {**request_kwargs, "messages": prepared}

    if budget > 0:
        marked_tools, tools_marked = mark_last_tool(request_kwargs.get("tools"), cache_control)
        if tools_marked:
            prepared_kwargs["tools"] = marked_tools
    return prepared_kwargs


class _PromptCacheCompletionsProxy:
    """Chat completions namespace proxy that adds the cache ladder to each request."""

    def __init__(self, completions: object, cache_control: Callable[[], dict[str, str] | None]) -> None:
        self._completions: Any = completions
        self._cache_control = cache_control

    def create(self, **request_kwargs: Any) -> object:  # noqa: ANN401 - mirrors the SDK signature
        cache_control = self._cache_control()
        if cache_control is not None:
            request_kwargs = _request_kwargs_with_prompt_cache(request_kwargs, cache_control)
        return self._completions.create(**request_kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._completions, name)


class _PromptCacheChatProxy:
    def __init__(self, chat: object, cache_control: Callable[[], dict[str, str] | None]) -> None:
        self._chat: Any = chat
        self._cache_control = cache_control

    @property
    def completions(self) -> _PromptCacheCompletionsProxy:
        return _PromptCacheCompletionsProxy(self._chat.completions, self._cache_control)

    def __getattr__(self, name: str) -> object:
        return getattr(self._chat, name)


class _PromptCacheClientProxy:
    """OpenAI SDK client proxy that routes chat completions through the cache ladder."""

    def __init__(self, client: object, cache_control: Callable[[], dict[str, str] | None]) -> None:
        self._client: Any = client
        self._cache_control = cache_control

    @property
    def chat(self) -> _PromptCacheChatProxy:
        return _PromptCacheChatProxy(self._client.chat, self._cache_control)

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


def with_prompt_cache_ladder(client: object, cache_control: Callable[[], dict[str, str] | None]) -> object:
    """Wrap an OpenAI SDK client so chat completions carry Claude ladder breakpoints.

    ``cache_control`` is read per request, so opt-outs set after the client is
    cached (for example on summary models) still apply.
    """
    return _PromptCacheClientProxy(client, cache_control)
