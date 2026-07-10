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

The same wire hook also carries Anthropic's server-side tool search for
MindRoom's deferred tools: :func:`install_claude_deferred_tool_search` records
the wire tool names that should stay out of the rendered prompt prefix, and
``_prepared`` tags them with ``defer_loading: true``, injects the regex search
tool, and orders the tools array deterministically (non-deferred first, then
deferred sorted by name). Deferred tools may not carry ``cache_control`` (the
API rejects the request), which is why the ladder's tools marker targets the
last non-deferred tool.

The hook's third job is history repair: replayed tool-search results are
stripped down to the request schema, and search uses missing their result are
removed. Both response shapes otherwise produce a 400 on the next request.
This is why the client proxy is installed unconditionally — the ladder and
defer tagging gate themselves per request, but a cache-disabled model with no
deferred tools can still replay poisoned history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mindroom.model_defaults import TOOL_SEARCH_UNSUPPORTED_MODEL_ID_PREFIXES
from mindroom.model_instance_checks import isinstance_of_loaded

if TYPE_CHECKING:
    from agno.models.anthropic import Claude as AnthropicClaude

_PROMPT_CACHE_HOOK_ATTR = "_mindroom_claude_prompt_cache_hook_installed"
_DEFERRED_TOOL_NAMES_ATTR = "_mindroom_claude_deferred_tool_names"
# The Anthropic API allows at most four cache_control markers per request.
_MAX_CACHE_MARKERS = 4
# Newest cacheable block plus one fallback boundary in an earlier message.
_MESSAGE_RUNG_COUNT = 2
_MARKABLE_BLOCK_TYPES = frozenset({"text", "tool_result", "document", "image"})

_TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_regex_20251119"
_TOOL_SEARCH_TOOL_NAME = "tool_search_tool_regex"
_NATIVE_TOOL_SEARCH_PROVIDERS = frozenset({"anthropic", "vertexai_claude"})

_SERVER_TOOL_USE_BLOCK_TYPE = "server_tool_use"
_TOOL_SEARCH_RESULT_BLOCK_TYPE = "tool_search_tool_result"
# The request schema for replayed tool-search results accepts only these keys
# (ToolSearchToolResultBlockParam); response blocks additionally carry
# citations/parsed_output/text, which the API rejects as extra inputs.
_TOOL_SEARCH_RESULT_INPUT_KEYS = frozenset({"type", "tool_use_id", "content", "cache_control"})


def native_tool_search_supported(provider: str, model_id: str) -> bool:
    """Return whether one authored provider/model pair supports server-side tool search.

    Every Claude model since Opus 4.5 / Sonnet 4.5 / Haiku 4.5 supports the
    search tool, so gating denylists the closed pre-4.5 set and new model
    releases take the native path without a code change.
    """
    canonical_provider = provider.strip().lower().replace("-", "_")
    if canonical_provider not in _NATIVE_TOOL_SEARCH_PROVIDERS:
        return False
    return not model_id.startswith(TOOL_SEARCH_UNSUPPORTED_MODEL_ID_PREFIXES)


_ANTHROPIC_CLAUDE_CLASS = ("agno.models.anthropic.claude", "Claude")


def as_anthropic_claude(model: object) -> AnthropicClaude | None:
    """Narrow one model to Agno's Claude without importing the anthropic SDK.

    A Claude instance can only exist once ``agno.models.anthropic.claude`` is
    imported, so the loaded-class check keeps non-Claude runtimes from paying
    the anthropic import just to no-op these hooks (#1436).
    """
    if not isinstance_of_loaded(model, _ANTHROPIC_CLAUDE_CLASS):
        return None
    return cast("AnthropicClaude", model)


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
    """Mark the last non-deferred tool so the tools prefix caches independently.

    Deferred tools may not carry ``cache_control`` (the API returns a 400) and
    sort after all non-deferred tools, so the marker position is byte-stable.
    The injected search tool is never marked either (whether the API accepts a
    marker on that server-tool type is unverified), so an all-deferred tool
    surface sends no tools marker; the tools prefix still caches under the
    system-prompt breakpoint because tools render ahead of system.
    """
    if not isinstance(tools, list) or not tools:
        return tools, 0
    for tool_index in range(len(tools) - 1, -1, -1):
        tool_dict = _as_dict(tools[tool_index])
        if tool_dict is not None and (
            tool_dict.get("defer_loading") is True or tool_dict.get("type") == _TOOL_SEARCH_TOOL_TYPE
        ):
            continue
        if tool_dict is None or _block_has_cache_marker(tool_dict):
            return tools, 0
        marked_tools = list(tools)
        marked_tool = dict(tool_dict)
        marked_tool["cache_control"] = dict(cache_control)
        marked_tools[tool_index] = marked_tool
        return marked_tools, 1
    return tools, 0


def _model_deferred_tool_names(model: AnthropicClaude) -> frozenset[str]:
    """Return the wire tool names registered for deferred loading on one model."""
    deferred_tool_names = vars(model).get(_DEFERRED_TOOL_NAMES_ATTR)
    return deferred_tool_names if isinstance(deferred_tool_names, frozenset) else frozenset()


def _request_kwargs_with_replay_safe_tool_search_results(request_kwargs: dict[str, Any]) -> dict[str, Any]:
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
    together. Replaying that orphan produces another 400. Drop only unmatched
    regex-search uses; valid pairs and other server-tool types remain intact.
    The input structure is never mutated.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    sanitized_messages = list(messages)
    changed = False
    for message_index, message in enumerate(sanitized_messages):
        message_dict = _as_dict(message)
        content = message_dict.get("content") if message_dict is not None else None
        if message_dict is None or not isinstance(content, list):
            continue
        paired_result_ids = {
            tool_use_id
            for block in content
            if (block_dict := _as_dict(block)) is not None
            and block_dict.get("type") == _TOOL_SEARCH_RESULT_BLOCK_TYPE
            and isinstance(tool_use_id := block_dict.get("tool_use_id"), str)
        }
        sanitized_content: list[Any] = []
        content_changed = False
        for block in content:
            block_dict = _as_dict(block)
            if block_dict is None:
                sanitized_content.append(block)
                continue
            block_id = block_dict.get("id")
            if (
                block_dict.get("type") == _SERVER_TOOL_USE_BLOCK_TYPE
                and block_dict.get("name") == _TOOL_SEARCH_TOOL_NAME
                and (not isinstance(block_id, str) or block_id not in paired_result_ids)
            ):
                content_changed = True
                continue
            if block_dict.get("type") == _TOOL_SEARCH_RESULT_BLOCK_TYPE and not (
                set(block_dict) <= _TOOL_SEARCH_RESULT_INPUT_KEYS
            ):
                prepared_block = {
                    key: value for key, value in block_dict.items() if key in _TOOL_SEARCH_RESULT_INPUT_KEYS
                }
                content_changed = True
            else:
                prepared_block = block
            sanitized_content.append(prepared_block)
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


def _request_kwargs_with_deferred_tool_search(
    request_kwargs: dict[str, Any],
    deferred_tool_names: frozenset[str],
) -> dict[str, Any]:
    """Tag deferred tools with defer_loading and inject the server-side search tool.

    Every deferred tool's full definition still ships on every request; the API
    keeps deferred schemas out of the rendered prompt prefix and expands them
    inline when the model searches. The tools array is ordered deterministically
    (search tool, then the remaining non-deferred tools, then deferred tools
    sorted by name) so the cached prefix stays byte-stable across requests.
    """
    tools = request_kwargs.get("tools")
    if not deferred_tool_names or not isinstance(tools, list):
        return request_kwargs
    non_deferred_tools: list[Any] = []
    deferred_tools: list[dict[str, Any]] = []
    for tool in tools:
        tool_dict = _as_dict(tool)
        if tool_dict is not None and tool_dict.get("name") in deferred_tool_names:
            deferred_tool = {**tool_dict, "defer_loading": True}
            # A deferred tool may not carry cache_control (the API returns a
            # 400), so drop any marker Agno added (e.g. via cache_tools).
            deferred_tool.pop("cache_control", None)
            deferred_tools.append(deferred_tool)
        else:
            non_deferred_tools.append(tool)
    if not deferred_tools:
        return request_kwargs
    deferred_tools.sort(key=lambda tool: str(tool.get("name")))
    prepared_kwargs = dict(request_kwargs)
    prepared_kwargs["tools"] = [
        {"type": _TOOL_SEARCH_TOOL_TYPE, "name": _TOOL_SEARCH_TOOL_NAME},
        *non_deferred_tools,
        *deferred_tools,
    ]
    return prepared_kwargs


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
        prepared_kwargs = _request_kwargs_with_replay_safe_tool_search_results(request_kwargs)
        prepared_kwargs = _request_kwargs_with_deferred_tool_search(
            prepared_kwargs,
            _model_deferred_tool_names(self._model),
        )
        if not self._model.cache_system_prompt:
            return prepared_kwargs
        cache_control = _prompt_cache_control(extended_cache_time=self._model.extended_cache_time is True)
        return _request_kwargs_with_prompt_cache_ladder(prepared_kwargs, cache_control)

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
    Bedrock all share the SDK client shape). Every request goes through the
    client proxy; the ladder and defer_loading tagging gate themselves per
    request inside ``_prepared``, so callers that deliberately disable caching
    (for example one-off summary calls) stay unmarked even when they reuse a
    hooked model instance. The proxy is never skipped outright because
    replayed tool-search blocks must be repaired on every request — a
    cache-disabled model with no deferred tools still replays poisoned history.
    """
    claude_model = as_anthropic_claude(model)
    if claude_model is None:
        return
    model_dict = vars(claude_model)
    if model_dict.get(_PROMPT_CACHE_HOOK_ATTR) is True:
        return
    original_get_client = claude_model.get_client
    original_get_async_client = claude_model.get_async_client
    model_dict[_PROMPT_CACHE_HOOK_ATTR] = True

    def _get_client_with_prompt_cache() -> object:
        client = original_get_client()
        return _PromptCacheClientProxy(client, claude_model)

    def _get_async_client_with_prompt_cache() -> object:
        client = original_get_async_client()
        return _PromptCacheClientProxy(client, claude_model)

    model_dict["get_client"] = _get_client_with_prompt_cache
    model_dict["get_async_client"] = _get_async_client_with_prompt_cache


def install_claude_deferred_tool_search(model: object, *, deferred_tool_names: frozenset[str]) -> None:
    """Register wire tool names for Anthropic server-side tool search on one model.

    Every request through the hooked client sends the named tools with
    ``defer_loading: true`` plus the regex tool-search entry, so their schemas
    stay out of the rendered prompt prefix and tool discovery never invalidates
    the prompt cache. No-op for non-Claude models and empty name sets.
    """
    claude_model = as_anthropic_claude(model)
    if claude_model is None or not deferred_tool_names:
        return
    install_claude_prompt_cache_hook(claude_model)
    vars(claude_model)[_DEFERRED_TOOL_NAMES_ATTR] = frozenset(deferred_tool_names)
