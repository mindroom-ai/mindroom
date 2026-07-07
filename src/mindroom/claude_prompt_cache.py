"""Prompt-cache breakpoint ladder for Anthropic-family Claude models.

Anthropic prompt caching only reuses the request prefix (tools -> system ->
messages) up to explicit ``cache_control`` breakpoints, and cache lookups only
probe roughly the last twenty content blocks before each breakpoint. Agno's
``cache_system_prompt`` marks the system block alone, so nothing after the
system prompt is ever cached: every tool-loop iteration re-processes all
earlier iterations of the same run, and once a turn grows past the lookup
window the next turn cannot reach the previous boundary at all.

This hook wraps the Anthropic SDK client used by Agno's Claude models and adds
up to three more breakpoints to each outgoing request:

- the newest cacheable content block (text / tool_result / document / image),
  so each request extends the cache entry written by the previous one;
- the newest cacheable block of an earlier message, as a fallback boundary for
  requests whose trailing messages were rewritten;
- the last tool definition, so the tools array survives system-prompt changes.

All markers share one TTL derived from the model settings, so the request
never mixes TTLs out of order (the API requires longer TTLs first, which is
also why Agno's ``cache_tools`` flag must stay off: it always emits a 5m tools
marker ahead of a potentially 1h system marker). The total marker count,
including markers Agno itself adds, is capped at the API limit of four.

The ladder operates on the wire-format request (after Agno's
``format_messages``) because Agno rebuilds assistant and tool_result blocks
from scratch on every request, so markers placed on Agno ``Message`` objects
can never reach tool results. String-vs-block content shape is irrelevant at
this layer: ``format_messages`` normalizes user strings into text blocks, and
the API hashes both shapes identically.
"""

from __future__ import annotations

from typing import Any, cast

from agno.models.anthropic import Claude as AnthropicClaude

_PROMPT_CACHE_HOOK_ATTR = "_mindroom_claude_prompt_cache_hook_installed"
# The Anthropic API allows at most four cache_control markers per request.
_MAX_CACHE_MARKERS = 4
# Newest cacheable block plus one fallback boundary in an earlier message.
_MESSAGE_RUNG_COUNT = 2
_MARKABLE_BLOCK_TYPES = frozenset({"text", "tool_result", "document", "image"})


def _prompt_cache_control(*, extended_cache_time: bool = False) -> dict[str, str]:
    """Return the cache_control payload for one breakpoint marker."""
    cache_control: dict[str, str] = {"type": "ephemeral"}
    if extended_cache_time:
        cache_control["ttl"] = "1h"
    return cache_control


def _as_dict(value: object) -> dict[str, Any] | None:
    """Return the value as a string-keyed dict when possible."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _block_has_cache_marker(block: object) -> bool:
    block_dict = _as_dict(block)
    return block_dict is not None and block_dict.get("cache_control") is not None


def _is_markable_block(block: object) -> bool:
    """Return whether a wire-format content block may carry cache_control."""
    block_dict = _as_dict(block)
    if block_dict is None:
        # SDK block objects (assistant text/tool_use/thinking) are rebuilt by
        # Agno each request; leave them alone and ladder on dict blocks only.
        return False
    block_type = block_dict.get("type")
    if block_type not in _MARKABLE_BLOCK_TYPES:
        return False
    if block_type == "text":
        return bool(block_dict.get("text"))
    if block_type == "tool_result":
        return bool(block_dict.get("content"))
    return True


def _count_cache_markers(request_kwargs: dict[str, Any]) -> int:
    """Count cache_control markers already present in a wire-format request."""
    count = 0
    system = request_kwargs.get("system")
    if isinstance(system, list):
        count += sum(1 for block in system if _block_has_cache_marker(block))
    tools = request_kwargs.get("tools")
    if isinstance(tools, list):
        count += sum(1 for tool in tools if _block_has_cache_marker(tool))
    messages = request_kwargs.get("messages")
    if isinstance(messages, list):
        for message in messages:
            message_dict = _as_dict(message)
            content = message_dict.get("content") if message_dict is not None else None
            if isinstance(content, list):
                count += sum(1 for block in content if _block_has_cache_marker(block))
    return count


def _mark_message_cache_rungs(
    messages: list[Any],
    cache_control: dict[str, str],
    rung_budget: int,
) -> tuple[list[Any], int]:
    """Mark the newest cacheable block of up to ``rung_budget`` trailing messages.

    Returns the (copied) message list and the number of newly added markers.
    At most one block per message is marked, scanning from the end of the
    request, so consecutive requests in a tool loop always share the previous
    request's newest boundary. A block that already carries a marker occupies
    a rung but is not counted as newly added (its marker was already deducted
    from the caller's overall budget). The input structure is never mutated.
    """
    marked_messages = list(messages)
    rungs_occupied = 0
    markers_added = 0
    for message_index in range(len(marked_messages) - 1, -1, -1):
        if rungs_occupied >= rung_budget:
            break
        message_dict = _as_dict(marked_messages[message_index])
        content = message_dict.get("content") if message_dict is not None else None
        if message_dict is None or not isinstance(content, list):
            continue
        for block_index in range(len(content) - 1, -1, -1):
            block = content[block_index]
            if _block_has_cache_marker(block):
                rungs_occupied += 1
                break
            if not _is_markable_block(block):
                continue
            marked_block = dict(_as_dict(block) or {})
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


def _mark_last_tool(tools: object, cache_control: dict[str, str]) -> tuple[object, int]:
    """Mark the last tool definition so the tools prefix caches independently."""
    if not isinstance(tools, list) or not tools:
        return tools, 0
    last_tool = _as_dict(tools[-1])
    if last_tool is None or _block_has_cache_marker(last_tool):
        return tools, 0
    marked_tools = list(tools)
    marked_tool = dict(last_tool)
    marked_tool["cache_control"] = dict(cache_control)
    marked_tools[-1] = marked_tool
    return marked_tools, 1


def _request_kwargs_with_prompt_cache_ladder(
    request_kwargs: dict[str, Any],
    cache_control: dict[str, str],
) -> dict[str, Any]:
    """Return request kwargs with ladder breakpoints added within the API budget."""
    marker_budget = _MAX_CACHE_MARKERS - _count_cache_markers(request_kwargs)
    if marker_budget <= 0:
        return request_kwargs
    prepared_kwargs = dict(request_kwargs)

    messages = prepared_kwargs.get("messages")
    if isinstance(messages, list) and messages:
        rung_budget = min(_MESSAGE_RUNG_COUNT, marker_budget)
        marked_messages, markers_added = _mark_message_cache_rungs(messages, cache_control, rung_budget)
        prepared_kwargs["messages"] = marked_messages
        marker_budget -= markers_added

    if marker_budget > 0:
        marked_tools, tools_marked = _mark_last_tool(prepared_kwargs.get("tools"), cache_control)
        if tools_marked:
            prepared_kwargs["tools"] = marked_tools

    return prepared_kwargs


class _PromptCacheMessagesProxy:
    """Messages namespace proxy that adds the cache ladder on create/stream."""

    def __init__(self, messages_namespace: object, model: AnthropicClaude) -> None:
        self._messages_namespace: Any = messages_namespace
        self._model = model

    def _prepared(self, request_kwargs: dict[str, Any]) -> dict[str, Any]:
        cache_control = _prompt_cache_control(extended_cache_time=self._model.extended_cache_time is True)
        return _request_kwargs_with_prompt_cache_ladder(request_kwargs, cache_control)

    def create(self, **request_kwargs: object) -> object:
        return self._messages_namespace.create(**self._prepared(request_kwargs))

    def stream(self, **request_kwargs: object) -> object:
        return self._messages_namespace.stream(**self._prepared(request_kwargs))

    def __getattr__(self, name: str) -> object:
        return getattr(self._messages_namespace, name)


class _PromptCacheBetaProxy:
    """Beta namespace proxy that routes beta.messages through the ladder."""

    def __init__(self, beta_namespace: object, model: AnthropicClaude) -> None:
        self._beta_namespace: Any = beta_namespace
        self._model = model

    @property
    def messages(self) -> _PromptCacheMessagesProxy:
        return _PromptCacheMessagesProxy(self._beta_namespace.messages, self._model)

    def __getattr__(self, name: str) -> object:
        return getattr(self._beta_namespace, name)


class _PromptCacheClientProxy:
    """Anthropic SDK client proxy that applies the ladder to message requests."""

    def __init__(self, client: object, model: AnthropicClaude) -> None:
        self._client: Any = client
        self._model = model

    @property
    def messages(self) -> _PromptCacheMessagesProxy:
        return _PromptCacheMessagesProxy(self._client.messages, self._model)

    @property
    def beta(self) -> _PromptCacheBetaProxy:
        return _PromptCacheBetaProxy(self._client.beta, self._model)

    # Python looks dunder methods up on the type, bypassing __getattr__, so
    # context-manager use of the proxied client must be delegated explicitly.
    def __enter__(self) -> _PromptCacheClientProxy:
        self._client.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None:
        result: bool | None = self._client.__exit__(exc_type, exc_value, traceback)
        return result

    async def __aenter__(self) -> _PromptCacheClientProxy:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None:
        result: bool | None = await self._client.__aexit__(exc_type, exc_value, traceback)
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


def install_claude_prompt_cache_hook(model: object) -> None:
    """Route an Agno Claude model's SDK clients through the prompt-cache ladder.

    Applies to every Anthropic-family Claude model (direct API, Vertex, and
    Bedrock all share the SDK client shape). The ladder is skipped per request
    while ``cache_system_prompt`` is falsy, so callers that deliberately
    disable caching (for example one-off summary calls) stay unmarked even
    when they reuse a hooked model instance.
    """
    if not isinstance(model, AnthropicClaude):
        return
    model_dict = vars(model)
    if model_dict.get(_PROMPT_CACHE_HOOK_ATTR) is True:
        return
    original_get_client = model.get_client
    original_get_async_client = model.get_async_client
    model_dict[_PROMPT_CACHE_HOOK_ATTR] = True

    def _get_client_with_prompt_cache() -> object:
        client = original_get_client()
        if not model.cache_system_prompt:
            return client
        return _PromptCacheClientProxy(client, model)

    def _get_async_client_with_prompt_cache() -> object:
        client = original_get_async_client()
        if not model.cache_system_prompt:
            return client
        return _PromptCacheClientProxy(client, model)

    model_dict["get_client"] = _get_client_with_prompt_cache
    model_dict["get_async_client"] = _get_async_client_with_prompt_cache
