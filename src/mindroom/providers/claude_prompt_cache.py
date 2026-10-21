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

Agent-built system prompts carry an explicit boundary before their session
context. Split that text into two system blocks and move the existing system
marker to the shared prefix. Dates and learning can then change
without invalidating the agent's instructions. The message rungs still cache
the full system and conversation prefix within each thread.

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
stripped down to the request schema, references to tools absent from the
current request are removed, search uses missing their result are removed,
and response citations are dropped from replayed assistant text blocks.
These response shapes otherwise produce a 400 on the next request. Inline
documents Claude cannot read, and inline media past the request size budget,
likewise become short text notes pointing at the attachment tools. This is why
the client proxy is installed unconditionally — the ladder and defer tagging
gate themselves per request, but a cache-disabled model with no deferred tools
can still replay poisoned history.

The ladder primitives themselves live in :mod:`mindroom.providers.anthropic.prompt_cache`,
which the owned Claude adapter and the OpenRouter Claude ladder share with this hook.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from mindroom.agno_compat_model_hooks import install_client_factories
from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.llm_request_logging import record_llm_request_tools
from mindroom.logging_config import get_logger
from mindroom.providers.agno_compat_claude import (
    BEDROCK_MAX_INLINE_MEDIA_BYTES,
    MAX_INLINE_IMAGE_BYTES,
    MAX_INLINE_MEDIA_BYTES,
    PARTNER_MAX_INLINE_IMAGE_BYTES,
    request_kwargs_with_leading_tool_results,
    request_kwargs_with_replay_safe_tool_search_results,
    request_kwargs_with_supported_inline_media,
    request_kwargs_without_replayed_citations,
)
from mindroom.providers.anthropic.prompt_cache import (
    MAX_CACHE_MARKERS,
    MESSAGE_RUNG_COUNT,
    has_cache_marker,
    mark_last_tool,
    mark_message_cache_rungs,
    native_tool_search_supported,
    prompt_cache_control,
    split_shared_system_prefix,
)
from mindroom.providers.claude_wire_blocks import (
    TOOL_SEARCH_RESULT_BLOCK_TYPE,
    TOOL_SEARCH_TOOL_NAME,
    TOOL_SEARCH_TOOL_TYPE,
    as_dict,
)
from mindroom.providers.model_instance_checks import isinstance_of_loaded
from mindroom.providers.tool_policy import provider_tools_disabled

__all__ = [
    "MAX_CACHE_MARKERS",
    "MESSAGE_RUNG_COUNT",
    "aclose_anthropic_async_client",
    "arefresh_session_backed_bedrock_async_client",
    "as_anthropic_claude",
    "claude_deferred_tool_names",
    "claude_loaded_tool_names",
    "install_claude_deferred_tool_search",
    "install_claude_prompt_cache_hook",
    "mark_last_tool",
    "mark_message_cache_rungs",
    "native_tool_search_supported",
    "prewarm_anthropic_async_client",
    "prompt_cache_control",
    "split_shared_system_prefix",
]

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.models.anthropic import Claude as AnthropicClaude
    from agno.models.message import Message

    from mindroom.providers.bedrock_claude import MindRoomBedrockClaude

_PROMPT_CACHE_HOOK_ATTR = "_mindroom_claude_prompt_cache_hook_installed"
_DEFERRED_TOOL_NAMES_ATTR = "_mindroom_claude_deferred_tool_names"
_ANTHROPIC_CLAUDE_CLASS = ("agno.models.anthropic.claude", "Claude")
_BEDROCK_CLAUDE_CLASS = ("mindroom.providers.bedrock_claude", "MindRoomBedrockClaude")
_VERTEX_CLAUDE_CLASS = ("mindroom.providers.vertex_claude_compat", "MindroomVertexAIClaude")
logger = get_logger(__name__)


def as_anthropic_claude(model: object) -> AnthropicClaude | None:
    """Narrow one model to Agno's Claude without importing the anthropic SDK.

    A Claude instance can only exist once ``agno.models.anthropic.claude`` is
    imported, so the loaded-class check keeps non-Claude runtimes from paying
    the anthropic import just to no-op these hooks (#1436).
    """
    if not isinstance_of_loaded(model, _ANTHROPIC_CLAUDE_CLASS):
        return None
    return cast("AnthropicClaude", model)


def _is_session_backed_bedrock_claude(model: object) -> bool:
    """Return whether a Bedrock model refreshes credentials from a session."""
    if not isinstance_of_loaded(model, _BEDROCK_CLAUDE_CLASS):
        return False
    return cast("MindRoomBedrockClaude", model).session is not None


def prewarm_anthropic_async_client(model: object) -> None:
    """Build and cache a Claude async SDK client without making a request."""
    claude_model = as_anthropic_claude(model)
    if claude_model is None:
        return
    try:
        claude_model.get_async_client()
    except Exception:
        async_client, claude_model.async_client = claude_model.async_client, None
        if async_client is not None:
            try:
                asyncio.run(async_client.close())
            except Exception:
                logger.exception("Failed to close partially initialized Claude async client")
        raise


async def aclose_anthropic_async_client(model: object) -> None:
    """Close and clear a retained Claude async SDK client, if present."""
    claude_model = as_anthropic_claude(model)
    if claude_model is None:
        return
    async_client, claude_model.async_client = claude_model.async_client, None
    if async_client is not None:
        await async_client.close()


async def arefresh_session_backed_bedrock_async_client(model: object) -> None:
    """Refresh one retained session-backed client between serialized turns."""
    if not _is_session_backed_bedrock_claude(model):
        return
    claude_model = cast("MindRoomBedrockClaude", model)
    previous_client = claude_model.async_client

    def replace_client() -> None:
        claude_model.async_client = None
        try:
            prewarm_anthropic_async_client(claude_model)
        except BaseException:
            claude_model.async_client = previous_client
            raise

    try:
        await run_blocking_until_complete(replace_client)
    finally:
        if previous_client is not None and claude_model.async_client is not previous_client:
            await run_coroutine_until_complete(previous_client.close())


def _count_cache_markers(request_kwargs: dict[str, Any]) -> int:
    """Count cache_control markers already present in a wire-format request."""
    count = 0
    system = request_kwargs.get("system")
    if isinstance(system, list):
        count += sum(1 for block in system if has_cache_marker(block))
    tools = request_kwargs.get("tools")
    if isinstance(tools, list):
        count += sum(1 for tool in tools if has_cache_marker(tool))
    messages = request_kwargs.get("messages")
    if isinstance(messages, list):
        for message in messages:
            message_dict = as_dict(message)
            content = message_dict.get("content") if message_dict is not None else None
            if isinstance(content, list):
                count += sum(1 for block in content if has_cache_marker(block))
    return count


def claude_deferred_tool_names(model: object) -> frozenset[str]:
    """Return the wire tool names registered for deferred loading on one model."""
    deferred_tool_names = vars(model).get(_DEFERRED_TOOL_NAMES_ATTR)
    return deferred_tool_names if isinstance(deferred_tool_names, frozenset) else frozenset()


def claude_loaded_tool_names(messages: Sequence[Message]) -> frozenset[str]:
    """Return the tool names whose definitions tool searches in these messages loaded into Claude's context."""
    names: set[str] = set()
    for message in messages:
        blocks = message.provider_data.get("server_tool_blocks") if isinstance(message.provider_data, dict) else None
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


def _request_tool_names(request_kwargs: dict[str, Any]) -> frozenset[str]:
    """Return client tool names available on the current request."""
    tools = request_kwargs.get("tools")
    if not isinstance(tools, list):
        return frozenset()
    return frozenset(
        name
        for tool in tools
        if (tool_dict := as_dict(tool)) is not None and isinstance(name := tool_dict.get("name"), str)
    )


def _search_result_with_available_references(
    block_dict: dict[str, Any],
    available_tool_names: frozenset[str],
) -> dict[str, Any] | None:
    """Return one search result without references to unavailable tools, or None when all are available."""
    content = as_dict(block_dict.get("content"))
    tool_references = content.get("tool_references") if content is not None else None
    if content is None or not isinstance(tool_references, list):
        return None
    available_references = [
        reference
        for reference in tool_references
        if (reference_dict := as_dict(reference)) is not None
        and isinstance(tool_name := reference_dict.get("tool_name"), str)
        and tool_name in available_tool_names
    ]
    if len(available_references) == len(tool_references):
        return None
    # Even when every reference is stale, keep the search as an empty result:
    # dropping its pair would change a signed turn whose blocks surround it.
    return {**block_dict, "content": {**content, "tool_references": available_references}}


def _request_kwargs_without_unavailable_tool_references(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop replayed tool-search references to tools absent from this request.

    Search results can reference tools that a later request no longer offers after
    its tool surface changes, which Claude rejects. A search left with no
    references stays as an empty result, like a search that matched nothing, so
    the signed thinking blocks around it are not moved together.
    """
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return request_kwargs
    available_tool_names = _request_tool_names(request_kwargs)
    prepared_messages = list(messages)
    changed = False
    for message_index, message in enumerate(messages):
        message_dict = as_dict(message)
        content = message_dict.get("content") if message_dict is not None else None
        if message_dict is None or not isinstance(content, list):
            continue
        prepared_content = list(content)
        content_changed = False
        for block_index, block in enumerate(content):
            block_dict = as_dict(block)
            if block_dict is None or block_dict.get("type") != TOOL_SEARCH_RESULT_BLOCK_TYPE:
                continue
            filtered_block = _search_result_with_available_references(block_dict, available_tool_names)
            if filtered_block is not None:
                prepared_content[block_index] = filtered_block
                content_changed = True
        if content_changed:
            prepared_messages[message_index] = {**message_dict, "content": prepared_content}
            changed = True
    if not changed:
        return request_kwargs
    return {**request_kwargs, "messages": prepared_messages}


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
        tool_dict = as_dict(tool)
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
        {"type": TOOL_SEARCH_TOOL_TYPE, "name": TOOL_SEARCH_TOOL_NAME},
        *non_deferred_tools,
        *deferred_tools,
    ]
    return prepared_kwargs


def _request_kwargs_with_shared_system_prefix(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep the shared instructions cacheable independently of session context."""
    system = request_kwargs.get("system")
    if not isinstance(system, list):
        return request_kwargs
    split_system = split_shared_system_prefix(system)
    if split_system is system:
        return request_kwargs
    return {**request_kwargs, "system": split_system}


def _request_kwargs_with_prompt_cache_ladder(
    request_kwargs: dict[str, Any],
    cache_control: dict[str, str],
) -> dict[str, Any]:
    """Return request kwargs with ladder breakpoints added within the API budget."""
    marker_budget = MAX_CACHE_MARKERS - _count_cache_markers(request_kwargs)
    if marker_budget <= 0:
        return request_kwargs
    prepared_kwargs = dict(request_kwargs)

    messages = prepared_kwargs.get("messages")
    if isinstance(messages, list) and messages:
        rung_budget = min(MESSAGE_RUNG_COUNT, marker_budget)
        marked_messages, markers_added = mark_message_cache_rungs(messages, cache_control, rung_budget)
        prepared_kwargs["messages"] = marked_messages
        marker_budget -= markers_added

    if marker_budget > 0:
        marked_tools, tools_marked = mark_last_tool(prepared_kwargs.get("tools"), cache_control)
        if tools_marked:
            prepared_kwargs["tools"] = marked_tools

    return prepared_kwargs


class _OffloadedAsyncStreamManager:
    """Construct one synchronous SDK stream manager away from the event loop."""

    def __init__(self, stream_factory: Callable[[], Any]) -> None:
        self._stream_factory = stream_factory
        self._stream_manager: Any = None

    async def __aenter__(self) -> object:
        current_task = asyncio.current_task()
        assert current_task is not None
        deferred_cancel_count = 0
        deferred_cancel: asyncio.CancelledError | None = None
        setup_task = asyncio.create_task(asyncio.to_thread(self._stream_factory))

        while not setup_task.done():
            try:
                await asyncio.shield(setup_task)
            except asyncio.CancelledError as exc:
                if setup_task.cancelled():
                    raise
                caller_cancel_count = current_task.cancelling()
                if caller_cancel_count <= 0:
                    raise
                for _ in range(caller_cancel_count):
                    current_task.uncancel()
                deferred_cancel_count += caller_cancel_count
                deferred_cancel = deferred_cancel or exc

        try:
            self._stream_manager = setup_task.result()
        except BaseException as setup_error:
            if deferred_cancel is None:
                raise
            for _ in range(deferred_cancel_count):
                current_task.cancel()
            raise deferred_cancel from setup_error

        for _ in range(deferred_cancel_count):
            current_task.cancel()
        return await self._stream_manager.__aenter__()

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None:
        result: bool | None = await self._stream_manager.__aexit__(exc_type, exc_value, traceback)
        return result


class _PromptCacheMessagesProxy:
    """Messages namespace proxy that adds the cache ladder on create/stream."""

    def __init__(
        self,
        messages_namespace: object,
        model: AnthropicClaude,
        *,
        offload_stream_setup: bool,
    ) -> None:
        self._messages_namespace: Any = messages_namespace
        self._model = model
        self._offload_stream_setup = offload_stream_setup

    def _prepared(self, request_kwargs: dict[str, Any]) -> dict[str, Any]:
        return _prepare_claude_request_kwargs(self._model, request_kwargs)

    def create(self, **request_kwargs: object) -> object:
        return self._messages_namespace.create(**self._prepared(request_kwargs))

    def stream(self, **request_kwargs: object) -> object:
        if self._offload_stream_setup:
            return _OffloadedAsyncStreamManager(
                lambda: self._messages_namespace.stream(**self._prepared(request_kwargs)),
            )
        return self._messages_namespace.stream(**self._prepared(request_kwargs))

    def __getattr__(self, name: str) -> object:
        return getattr(self._messages_namespace, name)


def _request_kwargs_without_provider_execution(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep definitions cacheable while preventing tool calls in participation checks."""
    if not provider_tools_disabled():
        return request_kwargs
    extra_body = request_kwargs.get("extra_body")
    effective = {**request_kwargs, **extra_body} if isinstance(extra_body, dict) else request_kwargs
    if not effective.get("tools") and not effective.get("mcp_servers"):
        return request_kwargs
    prepared = {**request_kwargs, "tool_choice": {"type": "none"}}
    if isinstance(extra_body, dict) and "tool_choice" in extra_body:
        prepared["extra_body"] = {**extra_body, "tool_choice": {"type": "none"}}
    return prepared


def _prepare_claude_request_kwargs(
    model: AnthropicClaude,
    request_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Apply MindRoom's wire transformations to one Claude request payload."""
    prepared_kwargs = request_kwargs_with_replay_safe_tool_search_results(request_kwargs)
    prepared_kwargs = _request_kwargs_without_unavailable_tool_references(prepared_kwargs)
    prepared_kwargs = request_kwargs_without_replayed_citations(prepared_kwargs)
    prepared_kwargs = request_kwargs_with_leading_tool_results(prepared_kwargs)
    prepared_kwargs = request_kwargs_with_supported_inline_media(
        prepared_kwargs,
        max_inline_bytes=(
            BEDROCK_MAX_INLINE_MEDIA_BYTES
            if isinstance_of_loaded(model, _BEDROCK_CLAUDE_CLASS)
            else MAX_INLINE_MEDIA_BYTES
        ),
        max_image_bytes=(
            PARTNER_MAX_INLINE_IMAGE_BYTES
            if isinstance_of_loaded(model, _BEDROCK_CLAUDE_CLASS, _VERTEX_CLAUDE_CLASS)
            else MAX_INLINE_IMAGE_BYTES
        ),
    )
    prepared_kwargs = _request_kwargs_with_deferred_tool_search(
        prepared_kwargs,
        claude_deferred_tool_names(model),
    )
    prepared_kwargs = _request_kwargs_without_provider_execution(prepared_kwargs)
    if model.cache_system_prompt:
        cache_control = prompt_cache_control(extended_cache_time=model.extended_cache_time is True)
        prepared_kwargs = _request_kwargs_with_shared_system_prefix(prepared_kwargs)
        prepared_kwargs = _request_kwargs_with_prompt_cache_ladder(prepared_kwargs, cache_control)
    record_llm_request_tools(prepared_kwargs.get("tools"))
    return prepared_kwargs


class _PromptCacheBetaProxy:
    """Beta namespace proxy that routes beta.messages through the ladder."""

    def __init__(
        self,
        beta_namespace: object,
        model: AnthropicClaude,
        *,
        offload_stream_setup: bool,
    ) -> None:
        self._beta_namespace: Any = beta_namespace
        self._model = model
        self._offload_stream_setup = offload_stream_setup

    @property
    def messages(self) -> _PromptCacheMessagesProxy:
        return _PromptCacheMessagesProxy(
            self._beta_namespace.messages,
            self._model,
            offload_stream_setup=self._offload_stream_setup,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self._beta_namespace, name)


class _PromptCacheClientProxy:
    """Anthropic SDK client proxy that applies the ladder to message requests."""

    def __init__(
        self,
        client: object,
        model: AnthropicClaude,
        *,
        offload_stream_setup: bool = False,
    ) -> None:
        self._client: Any = client
        self._model = model
        self._offload_stream_setup = offload_stream_setup

    @property
    def messages(self) -> _PromptCacheMessagesProxy:
        return _PromptCacheMessagesProxy(
            self._client.messages,
            self._model,
            offload_stream_setup=self._offload_stream_setup,
        )

    @property
    def beta(self) -> _PromptCacheBetaProxy:
        return _PromptCacheBetaProxy(
            self._client.beta,
            self._model,
            offload_stream_setup=self._offload_stream_setup,
        )

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
    install_client_factories(
        claude_model,
        marker=_PROMPT_CACHE_HOOK_ATTR,
        wrap_sync=lambda client: _PromptCacheClientProxy(client, claude_model),
        wrap_async=lambda client: _PromptCacheClientProxy(client, claude_model, offload_stream_setup=True),
    )


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
