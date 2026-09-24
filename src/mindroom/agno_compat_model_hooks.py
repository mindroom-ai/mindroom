"""Explicit bindings for missing Agno model lifecycle extension points.

Owners supply transformations and execution policies; this module only binds
them to the Agno methods that currently expose the required lifecycle stages.
"""

from __future__ import annotations

from contextlib import aclosing, contextmanager
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Iterator

    from agno.exceptions import ModelProviderError
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput, RunOutputEvent
    from agno.run.team import TeamRunOutput, TeamRunOutputEvent

type _AsyncInvoke = Callable[..., Coroutine[object, object, ModelResponse]]
type _AsyncStream = Callable[..., AsyncIterator[ModelResponse]]
type _SyncStream = Callable[..., Iterator[ModelResponse]]
type _RetryPredicate = Callable[[ModelProviderError], bool]


class _MessageProjection(Protocol):
    """Owner-managed transient request messages and history publication."""

    @property
    def outbound_messages(self) -> list[Message]:
        """Messages to pass to Agno."""
        ...

    def publish_model_mutations(self) -> None:
        """Return model mutations to owner history after the invocation."""
        ...


class _ClientProvider(Protocol):
    """Provider adapter with the SDK client factories used by Claude models."""

    def get_client(self) -> object: ...

    def get_async_client(self) -> object: ...


# AGNO_COMPAT: Model message projection lacks a public hook.
# Reason: Agno has no model-message projection hook shared by fallback models.
# Upstream issue: No matching public transient-message projection issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: A public model request hook accepts transient message projections and
# preserves owner-controlled history publication on success, failure, and cancellation.
# Coverage: tests/test_approval_receipt.py.
def install_message_projection(
    model: Model,
    *,
    marker: str,
    project: Callable[[list[Message]], _MessageProjection | None],
) -> None:
    """Bind a message projection to this model's async response entry point."""
    try:
        original = cast("Callable[..., Awaitable[ModelResponse]]", model.aresponse)
        model_dict = vars(model)
    except (AttributeError, TypeError):
        return
    if model_dict.get(marker) is True:
        return
    model_dict[marker] = True

    async def response_with_projection(*args: object, **kwargs: object) -> ModelResponse:
        messages: object = kwargs.get("messages")
        if isinstance(messages, list):
            projection = project(cast("list[Message]", messages))
            if projection is None:
                return await original(*args, **kwargs)
            try:
                return await original(*args, **{**kwargs, "messages": projection.outbound_messages})
            finally:
                projection.publish_model_mutations()
        if args and isinstance(args[0], list):
            projection = project(cast("list[Message]", args[0]))
            if projection is None:
                return await original(*args, **kwargs)
            try:
                return await original(projection.outbound_messages, *args[1:], **kwargs)
            finally:
                projection.publish_model_mutations()
        return await original(*args, **kwargs)

    model_dict["aresponse"] = response_with_projection


def _with_tool_checkpoint(
    messages: list[Message],
    kwargs: dict[str, object],
    after_tools: Callable[[list[Message], ModelResponse], Awaitable[None]],
) -> dict[str, object]:
    """Run the owner's post-tool policy without discarding Agno's checkpoint."""
    previous = cast("Callable[[ModelResponse], Awaitable[None]] | None", kwargs.get("after_tool_results"))

    async def after_tool_results(result: ModelResponse) -> None:
        try:
            await after_tools(messages, result)
        finally:
            if previous is not None:
                await previous(result)

    return {**kwargs, "after_tool_results": after_tool_results}


# AGNO_COMPAT: Post-tool callbacks lack mutable messages and results.
# Reason: after_tool_results exists in Agno 3.0.9 but does not expose mutable
# input messages or raw tool-result messages to the owner through Agent/Team runs.
# Approved continuations append results directly before entering aresponse or
# aresponse_stream, bypassing both formatting and media callbacks.
# Mid-turn judgments must be awaited after a completed batch or at resumed entry,
# before the next provider request; existing checkpoints must run even on cancellation.
# Upstream issue: No matching public post-tool message callback issue identified.
# Upstream PR: None identified; existing checkpoint callbacks are only a partial API.
# Remove when: Public awaitable callbacks expose messages/results after formatting and media
# insertion, including resumed batches; retain the owner's notice deduplication
# and stop-after policy.
# Coverage: tests/test_mid_turn.py exercises streaming, resumed batches, terminal tools,
# and checkpoint cancellation; tests/test_queued_message_notify.py and
# tests/test_approval_queued_notice.py cover queued notices and approval resumes.
def install_tool_result_callback(
    model: Model,
    *,
    marker: str,
    callback: Callable[[list[Message], list[Message]], None],
    before_response: Callable[[list[Message]], None],
    before_response_async: Callable[[list[Message]], Awaitable[None]],
    after_tools_async: Callable[[list[Message], ModelResponse], Awaitable[None]],
) -> None:
    """Observe tool-result stages and response entry after approved batches."""
    try:
        original_format = model.format_function_call_results
        model_dict = vars(model)
    except (AttributeError, TypeError):
        return
    if model_dict.get(marker) is True:
        return
    model_dict[marker] = True
    original_response = cast("Callable[..., Awaitable[ModelResponse]]", model.aresponse)
    original_stream = cast(
        "Callable[..., AsyncGenerator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]]",
        model.aresponse_stream,
    )

    async def response(messages: list[Message], *args: object, **kwargs: object) -> ModelResponse:
        before_response(messages)
        await before_response_async(messages)
        return await original_response(messages, *args, **_with_tool_checkpoint(messages, kwargs, after_tools_async))

    async def response_stream(
        messages: list[Message],
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]:
        before_response(messages)
        await before_response_async(messages)
        async with aclosing(
            original_stream(messages, *args, **_with_tool_checkpoint(messages, kwargs, after_tools_async)),
        ) as stream:
            async for event in stream:
                yield event

    model_dict["aresponse"] = response
    model_dict["aresponse_stream"] = response_stream

    def format_results(
        messages: list[Message],
        function_call_results: list[Message],
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> None:
        original_format(
            messages=messages,
            function_call_results=function_call_results,
            compress_tool_results=compress_tool_results,
            **kwargs,
        )
        callback(messages, function_call_results)

    def handle_media(
        messages: list[Message],
        function_call_results: list[Message],
        send_media_to_model: bool = True,
    ) -> None:
        original_media(
            messages=messages,
            function_call_results=function_call_results,
            send_media_to_model=send_media_to_model,
        )
        callback(messages, function_call_results)

    model_dict["format_function_call_results"] = format_results
    try:
        original_media = model._handle_function_call_media
    except AttributeError:
        return
    model_dict["_handle_function_call_media"] = handle_media


# AGNO_COMPAT: Agno's response loops call the model for as long as it requests tools.
# Reason: Agno 3.0.9 keeps looping while the model requests tools. tool_call_limit only refuses
# counted calls; calls naming an unknown tool or carrying unparseable arguments are answered in
# get_function_calls_to_run before any limit accounting, so a model that keeps requesting tools
# keeps its run alive indefinitely. The loop's counters are local to aresponse/aresponse_stream,
# and one model object can serve concurrent runs, so bounding a run's model requests needs a
# per-loop gate consulted before each request.
# Upstream issue: https://github.com/agno-agi/agno/issues/8304 and
# https://github.com/agno-agi/agno/issues/10041 cover refused tool_call_limit batches; no upstream
# issue identified for loops of unknown-tool or unparseable-argument calls.
# Upstream PR: https://github.com/agno-agi/agno/pull/10042 and
# https://github.com/agno-agi/agno/pull/8324 are open and stop only after a batch of limit refusals,
# which unknown-tool and unparseable-argument batches never produce.
# Remove when: Agno ends every async streaming and non-streaming response loop after a bounded
# number of model requests, whatever the requested tool calls, through its normal completion path;
# the owner's cap derived from tool_call_limit and its once-per-run tool_call_limit_reached warning
# must remain.
# Coverage: tests/test_tool_call_budget.py.
def install_response_request_gate(
    model: Model,
    *,
    marker: str,
    open_gate: Callable[[int | None], Callable[[], bool] | None],
) -> None:
    """Consult one gate per async Agno response loop before each of that loop's model requests.

    ``open_gate`` receives the loop's ``tool_call_limit`` when ``aresponse`` or ``aresponse_stream``
    starts and returns the loop's gate, or ``None`` to leave the loop ungated.
    Agno passes the run's ``run_response`` to every iteration, so loops of concurrent runs sharing
    this model keep separate gates; a nested loop of the same run shares its outer loop's gate.
    A refused request never reaches the provider: the iteration ends with an empty assistant message
    and no tool calls, so Agno's loop breaks through its normal completion path.
    """
    model_dict = vars(model)
    if model_dict.get(marker) is True:
        return
    model_dict[marker] = True
    original_response = cast("Callable[..., Awaitable[ModelResponse]]", model.aresponse)
    original_response_stream = cast(
        "Callable[..., AsyncGenerator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]]",
        model.aresponse_stream,
    )
    original_process = cast("Callable[..., Awaitable[None]]", model._aprocess_model_response)
    original_process_stream = cast("Callable[..., AsyncGenerator[ModelResponse]]", model.aprocess_response_stream)
    gates: dict[int, Callable[[], bool]] = {}

    async def response(
        *args: object,
        tool_call_limit: int | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        **kwargs: object,
    ) -> ModelResponse:
        with _gated_response_loop(gates, open_gate, run_response, tool_call_limit):
            return await original_response(
                *args,
                tool_call_limit=tool_call_limit,
                run_response=run_response,
                **kwargs,
            )

    async def response_stream(
        *args: object,
        tool_call_limit: int | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]:
        with _gated_response_loop(gates, open_gate, run_response, tool_call_limit):
            async with aclosing(
                original_response_stream(
                    *args,
                    tool_call_limit=tool_call_limit,
                    run_response=run_response,
                    **kwargs,
                ),
            ) as stream:
                async for event in stream:
                    yield event

    async def process_model_response(
        *args: object,
        model_response: ModelResponse,
        run_response: RunOutput | TeamRunOutput | None = None,
        **kwargs: object,
    ) -> None:
        if _request_refused(gates, run_response):
            # The non-streaming loop keeps one ModelResponse and adds its usage to the run
            # metrics after every request, so a refused request must not repeat the last usage.
            model_response.response_usage = None
            return
        await original_process(*args, model_response=model_response, run_response=run_response, **kwargs)

    async def process_response_stream(
        *args: object,
        run_response: RunOutput | TeamRunOutput | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        if _request_refused(gates, run_response):
            return
        async with aclosing(original_process_stream(*args, run_response=run_response, **kwargs)) as stream:
            async for delta in stream:
                yield delta

    model_dict["aresponse"] = response
    model_dict["aresponse_stream"] = response_stream
    model_dict["_aprocess_model_response"] = process_model_response
    model_dict["aprocess_response_stream"] = process_response_stream


@contextmanager
def _gated_response_loop(
    gates: dict[int, Callable[[], bool]],
    open_gate: Callable[[int | None], Callable[[], bool] | None],
    run_response: RunOutput | TeamRunOutput | None,
    tool_call_limit: int | None,
) -> Iterator[None]:
    """Hold the gate of a run's outermost response loop for as long as that loop runs."""
    key = id(run_response)
    gate = None if run_response is None or key in gates else open_gate(tool_call_limit)
    if gate is None:
        yield
        return
    gates[key] = gate
    try:
        yield
    finally:
        del gates[key]


def _request_refused(gates: dict[int, Callable[[], bool]], run_response: RunOutput | TeamRunOutput | None) -> bool:
    """Consult the gate of the loop serving ``run_response`` for one more model request."""
    gate = gates.get(id(run_response)) if run_response is not None else None
    return gate is not None and not gate()


# AGNO_COMPAT: Model invocation and streaming lack composable middleware.
# Reason: Agno has no composable invocation/stream middleware, so owners must
# capture and replace instance methods to retain installation order and context.
# Upstream issue: No matching public model invocation middleware issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Public invocation hooks compose sync/async streams and request context
# with the same ordering; logging, retry limits, and stream cleanup remain owners.
# Coverage: tests/test_llm_request_logging.py; tests/test_claude_stream_retry.py; tests/test_provider_stream_retry.py.
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


# AGNO_COMPAT: Final provider requests lack an attempt-scoped hook.
# Reason: Agno has no attempt-scoped hook at the final provider request; agent
# pre-hooks run before compression, and its answer cache bypasses invocation hooks.
# Upstream issue: No matching public scoped provider-request hook issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: A public scoped request hook runs after compression and before cached
# answers, restoring previous hooks on exit; retain owner participation and run identity.
# Coverage: tests/test_participation.py::test_agent_turn_applies_decision_before_model_answer;
# tests/test_participation.py::test_cancellation_is_not_converted_to_silence;
# tests/test_participation_ownership.py::test_helpers_cannot_acquire_primary_decision.
@contextmanager
def temporary_async_invocation_hooks(
    model: Model,
    *,
    invoke: _AsyncInvoke,
    stream: _AsyncStream,
) -> Iterator[None]:
    """Bind attempt-scoped provider callbacks while bypassing Agno's answer cache."""
    model_dict = vars(model)
    saved = {name: model_dict.get(name) for name in ("ainvoke", "ainvoke_stream", "cache_response")}
    model_dict["ainvoke"] = invoke
    model_dict["ainvoke_stream"] = stream
    model_dict["cache_response"] = False
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                model_dict.pop(name, None)
            else:
                model_dict[name] = value


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
