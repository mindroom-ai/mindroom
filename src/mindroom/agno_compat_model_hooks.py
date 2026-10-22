"""Explicit bindings for missing Agno model lifecycle extension points.

Owners supply transformations and execution policies; this module only binds
them to the Agno methods that currently expose the required lifecycle stages.
"""

from __future__ import annotations

from contextlib import aclosing
from types import MethodType
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping

    from agno.exceptions import ModelProviderError
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.tools.function import Function, FunctionCall

type _AsyncInvoke = Callable[..., Coroutine[object, object, ModelResponse]]
type _AsyncStream = Callable[..., AsyncIterator[ModelResponse]]
type _SyncStream = Callable[..., Iterator[ModelResponse]]
type _RetryPredicate = Callable[[ModelProviderError], bool]
type _RequestPreparation = Callable[
    [list[Message], list[dict[str, Any]] | None, RunOutput | TeamRunOutput | None],
    Awaitable[None],
]


class _ClientProvider(Protocol):
    """Provider adapter with the SDK client factories used by Claude models."""

    def get_client(self) -> object: ...

    def get_async_client(self) -> object: ...


# AGNO_COMPAT: Response loops lack an awaitable pre-request hook with mutable messages.
# Reason: Mid-turn compaction must inspect and rewrite the loop's own message list, with the formatted tools and
# the run, after Agno appends each tool batch and before the next provider request. Agno's CompressionManager runs
# at that point but only compresses tool results, is enabled only by compress_tool_results, and never sees the run.
# Upstream issue: Tracking gap. Related open issues https://github.com/agno-agi/agno/issues/4952 and
# https://github.com/agno-agi/agno/issues/8342 ask for token-aware compaction but not for a caller-owned hook
# consulted before each model request.
# Upstream PR: None identified.
# Remove when: Agno awaits a public callback before every provider request of its async streaming and
# non-streaming loops, passing the mutable message list, the formatted tools, and the run; the owner's compaction
# policy remains MindRoom's.
# Coverage: tests/test_agno_compat_model_hooks.py::test_request_preparation_runs_before_every_provider_request;
# tests/test_agno_compat_model_hooks.py::test_tool_call_cap_installed_later_refuses_before_preparation.
def install_request_preparation(model: Model, *, marker: str, prepare: _RequestPreparation) -> None:
    """Await ``prepare`` before every provider request of this model's async response loops.

    ``prepare`` receives the loop's own message list, which Agno sends and keeps appending to, so it may rewrite
    the list in place. A tool-call cap installed later wraps this hook, so a refused request is never prepared.
    """
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    model_dict[marker] = True
    original_process = cast("Callable[..., Awaitable[None]]", model._aprocess_model_response)
    original_process_stream = cast("Callable[..., AsyncGenerator[ModelResponse]]", model.aprocess_response_stream)

    async def process_model_response(
        *args: object,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        **kwargs: object,
    ) -> None:
        await prepare(messages, tools, run_response)
        await original_process(*args, messages=messages, tools=tools, run_response=run_response, **kwargs)

    async def process_response_stream(
        *args: object,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        await prepare(messages, tools, run_response)
        async with aclosing(
            original_process_stream(*args, messages=messages, tools=tools, run_response=run_response, **kwargs),
        ) as stream:
            async for delta in stream:
                yield delta

    model_dict["_aprocess_model_response"] = process_model_response
    model_dict["aprocess_response_stream"] = process_response_stream


# AGNO_COMPAT: Model invocation and streaming lack composable middleware.
# Reason: Agno has no composable invocation/stream middleware, so owners must
# capture and replace instance methods to retain installation order and context.
# Upstream issue: No matching public model invocation middleware issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Public invocation hooks compose sync/async streams and request context
# with the same ordering; logging, retry limits, and stream cleanup remain owners.
# Coverage: tests/test_llm_request_logging.py, tests/test_claude_stream_retry.py,
# tests/test_provider_stream_retry.py, and tests/test_portable_tool_search.py.
def install_async_invocation_hooks(
    model: Model,
    *,
    marker: str,
    wrap_invoke: Callable[[_AsyncInvoke], _AsyncInvoke],
    wrap_stream: Callable[[_AsyncStream], _AsyncStream],
) -> None:
    """Bind an owner's async invocation wrappers once, in installation order."""
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    model_dict["ainvoke"] = wrap_invoke(model.ainvoke)
    model_dict["ainvoke_stream"] = wrap_stream(model.ainvoke_stream)
    model_dict[marker] = True


def install_stream_invocation_hooks(
    model: Model,
    *,
    marker: str,
    wrap_sync: Callable[[_SyncStream], _SyncStream],
    wrap_async: Callable[[_AsyncStream], _AsyncStream],
) -> None:
    """Bind an owner's sync and async stream wrappers once."""
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    original_sync = model.invoke_stream
    original_async = model.ainvoke_stream
    model_dict[marker] = True
    model_dict["invoke_stream"] = wrap_sync(original_sync)
    model_dict["ainvoke_stream"] = wrap_async(original_async)


# AGNO_COMPAT: Parsed tool calls cannot be rewritten before function resolution.
# Reason: Portable deferred tool search must turn a model's call_tool wrapper call into a call of the real
# function before Agno resolves it, so approval, hooks, job execution, and stored history see the real name.
# Agno resolves the parsed calls of every streaming and non-streaming loop in get_function_calls_to_run and
# offers no hook between parsing a provider response and that resolution.
# Upstream issue: Tracking gap; no Agno issue asks for a caller-owned tool-call rewrite before resolution.
# Upstream PR: None identified.
# Remove when: Agno exposes a public hook that rewrites an assistant message's parsed tool calls before every
# response loop resolves them to functions; the call_tool wrapper itself remains MindRoom's.
# Coverage: tests/test_portable_tool_search.py::test_installed_model_routes_call_tool_and_keeps_real_names_in_history and
# tests/test_portable_tool_search.py::test_rewrite_keeps_the_bound_method_shape_other_bindings_wrap.
def install_tool_call_rewrite(
    model: Model,
    *,
    marker: str,
    rewrite: Callable[[list[dict[str, Any]], Mapping[str, Function]], list[dict[str, Any]]],
) -> None:
    """Rewrite an assistant message's tool calls in place before Agno resolves them to functions."""
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    model_dict[marker] = True
    # Bind as a method, like the CLI checkpoint capture, so a deepcopied model calls itself and other wrappers can unwrap this one.
    original_function = model.get_function_calls_to_run.__func__

    def get_function_calls_to_run(
        rewritten_model: Model,
        assistant_message: Message,
        messages: list[Message],
        functions: dict[str, Function] | None = None,
    ) -> list[FunctionCall]:
        if assistant_message.tool_calls:
            assistant_message.tool_calls = rewrite(assistant_message.tool_calls, functions or {})
        return original_function(rewritten_model, assistant_message, messages, functions)

    model_dict["get_function_calls_to_run"] = MethodType(get_function_calls_to_run, model)


# AGNO_COMPAT: Retry cycles lack public context and classification hooks.
# Reason: Request-scoped media state must enclose Agno's private retry loops,
# while the owner's retry classifier must compose with Agno's predicate.
# Upstream issue: No matching public retry-cycle context/predicate hook identified.
# Upstream PR: None identified for this extension point.
# Remove when: Public hooks enclose the whole retry cycle and compose classifiers;
# preserve owner media capability learning, redaction, and replay safety.
# Coverage: tests/test_provider_media_fallback.py.
def install_retry_cycle_hooks(
    model: Model,
    *,
    marker: str,
    wrap_retry: Callable[[_AsyncInvoke], _AsyncInvoke],
    wrap_retry_stream: Callable[[_AsyncStream], _AsyncStream],
    wrap_predicate: Callable[[_RetryPredicate], _RetryPredicate],
    wrap_invoke: Callable[[_AsyncInvoke], _AsyncInvoke],
    wrap_stream: Callable[[_AsyncStream], _AsyncStream],
) -> None:
    """Bind a retry-cycle owner around Agno's private and public async methods."""
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    model_dict["_ainvoke_with_retry"] = wrap_retry(model._ainvoke_with_retry)
    model_dict["_ainvoke_stream_with_retry"] = wrap_retry_stream(model._ainvoke_stream_with_retry)
    model_dict["_is_retryable_error"] = wrap_predicate(model._is_retryable_error)
    model_dict["ainvoke"] = wrap_invoke(model.ainvoke)
    model_dict["ainvoke_stream"] = wrap_stream(model.ainvoke_stream)
    model_dict[marker] = True


# AGNO_COMPAT: Normalized provider payloads lack a public transform hook.
# Reason: Agno sends normalized Claude payloads through SDK clients without a
# public request transform; client factories must be replaced to bind the proxy.
# Upstream issue: No matching public normalized-provider-request hook identified.
# Upstream PR: None identified for this extension point.
# Remove when: A public provider request hook can apply owner transforms for sync,
# async, streaming and beta requests without replacing client factories.
# Coverage: tests/test_agent_prompt_cache.py; tests/test_prompt_cache_review.py.
def install_client_factories(
    model: _ClientProvider,
    *,
    marker: str,
    wrap_sync: Callable[[object], object],
    wrap_async: Callable[[object], object],
) -> None:
    """Bind provider client proxies while leaving payload policy with the owner."""
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    original_sync = model.get_client
    original_async = model.get_async_client
    model_dict[marker] = True
    model_dict["get_client"] = lambda: wrap_sync(original_sync())
    model_dict["get_async_client"] = lambda: wrap_async(original_async())
