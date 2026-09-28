"""Preserve Agno's current request messages when a run is interrupted."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _run as agent_run
from agno.metrics import MessageMetrics, accumulate_model_metrics
from agno.models.base import MessageData, Model
from agno.models.response import ModelResponse
from agno.team import _run as team_run

from mindroom.usage_storage import has_token_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.messages import RunMessages
    from agno.run.team import TeamRunOutput
    from pydantic import BaseModel

_SUPPORTED_VERSION = "3.0.9"
_PATCHED = False
_LOCK = threading.Lock()


@dataclass(frozen=True, eq=False)
class _ModelRequest:
    """Provider stream state that terminal cleanup needs if the stream is abandoned."""

    model: Model
    messages: list[Message]
    assistant_message: Message
    stream_data: MessageData
    run_response: RunOutput | TeamRunOutput | None


# Keyed by run identity; one run streams at most one model request at a time.
_ACTIVE_REQUESTS: dict[int, _ModelRequest] = {}


# AGNO_COMPAT: Stopping an async run abandons its suspended model stream.
# Reason: Agno's async run and model generators iterate nested streams without closing
# them. Consumer closure or a cancellation check between chunks persists the run while
# the model stream is suspended; garbage collection later finalizes that chain outermost
# first, so received usage misses the run, session, and request totals.
# Upstream issue: No matching issue identified; https://github.com/agno-agi/agno/issues/9489
# covers related abandoned-generation bookkeeping, not usage settlement.
# Upstream PR: None identified.
# Remove when: Agno closes in-flight model streams innermost first before terminal
# cancellation or error persistence, including consumer closure between chunks.
# Coverage: tests/test_openai_responses_stream.py::test_received_request_usage_survives_abandoned_stream;
# tests/test_openai_responses_stream.py::test_received_request_usage_survives_cancel_request.
def _settle_abandoned_request(run_response: RunOutput | TeamRunOutput) -> None:
    request = _ACTIVE_REQUESTS.pop(id(run_response), None)
    if request is None:
        return
    settled = request.assistant_message.model_copy()
    # Reuse provider accounting, including counters retained from failed attempts.
    request.model._populate_assistant_message_from_stream_data(
        settled,
        MessageData(response_metrics=request.stream_data.response_metrics),
    )
    # Late finalization of the abandoned stream must not count this request again.
    request.assistant_message.metrics = MessageMetrics()
    if not has_token_usage(settled.metrics.to_dict()):
        return
    request.messages.append(settled)
    accumulate_model_metrics(
        ModelResponse(response_usage=settled.metrics),
        request.model,
        request.model.model_type,
        run_response.metrics,
    )


# AGNO_COMPAT: Terminal cleanup retains stale checkpoint or continuation messages.
# Reason: Agent and Team flush in-flight messages only if the saved list is empty,
# so later requests disappear from failed/cancelled runs despite retained totals.
# Upstream issue: No matching issue identified; terminal snapshot refresh is untracked.
# Upstream PR: None identified.
# Remove when: Both error and cancellation cleanup save the latest in-flight
# messages, including resumed runs, while respecting add_to_agent_memory.
# Coverage: tests/test_agno_compat_run_messages.py::test_terminal_snapshot_keeps_requests_after_checkpoint;
# tests/test_agno_compat_run_messages.py::test_interrupted_continuation_exports_every_completed_request.
def _flush_messages(run_response: RunOutput | TeamRunOutput, run_messages: RunMessages | None) -> None:
    _settle_abandoned_request(run_response)
    if run_messages is not None:
        run_response.messages = [message for message in run_messages.messages if message.add_to_agent_memory]


def _with_current_messages(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def cancel(
        run_response: RunOutput | TeamRunOutput,
        error: BaseException,
        run_messages: RunMessages | None = None,
        *args: object,
        **kwargs: object,
    ) -> RunOutput | TeamRunOutput:
        _settle_abandoned_request(run_response)
        if run_messages is not None:
            # Let Agno retain its partial-content, approval, and member cleanup.
            run_response.messages = None
        return original(run_response, error, run_messages, *args, **kwargs)

    return cancel


# AGNO_COMPAT: Interrupted model streams count usage without retaining its message.
# Reason: Model accumulates assistant metrics in finally but appends the message
# only after the stream returns successfully. Retain that same metered message
# before the exception reaches terminal cleanup; leave aggregate accounting alone.
# Upstream issue: https://github.com/agno-agi/agno/issues/9489 describes the same
# lost-message boundary; request-detail preservation is not separately tracked.
# Upstream PR: None identified.
# Remove when: Model preserves metered assistant messages on stream failure and
# cancellation without duplicating successful requests or inventing usage.
# Coverage: tests/test_openai_responses_stream.py::test_terminal_usage_survives_stream_failure;
# tests/test_openai_responses_stream.py::test_received_request_usage_survives_task_cancellation;
# tests/test_openai_responses_stream.py::test_terminal_usage_survives_retry.
def _retain_metered_message(messages: list[Message], assistant_message: Message) -> None:
    if has_token_usage(assistant_message.metrics.to_dict()) and not any(
        message is assistant_message for message in messages
    ):
        messages.append(assistant_message)


# AGNO_COMPAT: Request messages omit the model that incurred their usage.
# Reason: Run-level model details can span multiple models after continuation,
# so request timestamps and counters alone cannot be attributed to one model.
# Upstream issue: None identified; per-request attribution is an extension gap.
# Upstream PR: None identified.
# Remove when: Agno persists each assistant request's model and provider.
# Coverage: tests/test_request_usage.py::test_mixed_model_requests_keep_models_and_actual_dates;
# tests/test_openai_responses_stream.py::test_mixed_model_failed_stream_keeps_request_attribution.
def _record_request_model(model: Model, assistant_message: Message) -> None:
    assistant_message.provider_data = {
        **(assistant_message.provider_data or {}),
        "mindroom_model": {"id": model.id, "provider": model.get_provider()},
    }


def _with_request_model[T](original: Callable[..., T]) -> Callable[..., T]:
    @wraps(original)
    def populate(model: Model, assistant_message: Message, *args: object, **kwargs: object) -> T:
        result = original(model, assistant_message, *args, **kwargs)
        _record_request_model(model, assistant_message)
        return result

    return populate


def _begin_request(request: _ModelRequest) -> None:
    _record_request_model(request.model, request.assistant_message)
    if request.run_response is not None:
        _ACTIVE_REQUESTS[id(request.run_response)] = request


def _finish_request(request: _ModelRequest) -> bool:
    """Return whether the stream still owns its request, rather than terminal cleanup."""
    if request.run_response is None:
        return True
    key = id(request.run_response)
    if _ACTIVE_REQUESTS.get(key) is not request:
        return False
    del _ACTIVE_REQUESTS[key]
    return True


def _with_metered_messages(
    original: Callable[..., Iterator[ModelResponse]],
) -> Callable[..., Iterator[ModelResponse]]:
    @wraps(original)
    def stream(
        model: Model,
        messages: list[Message],
        assistant_message: Message,
        stream_data: MessageData,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        request = _ModelRequest(model, messages, assistant_message, stream_data, run_response)
        _begin_request(request)
        try:
            yield from original(
                model,
                messages,
                assistant_message,
                stream_data,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
            )
        except BaseException:
            if _finish_request(request):
                _retain_metered_message(messages, assistant_message)
            raise
        _finish_request(request)

    return stream


def _with_metered_messages_async(
    original: Callable[..., AsyncIterator[ModelResponse]],
) -> Callable[..., AsyncIterator[ModelResponse]]:
    @wraps(original)
    async def stream(
        model: Model,
        messages: list[Message],
        assistant_message: Message,
        stream_data: MessageData,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        request = _ModelRequest(model, messages, assistant_message, stream_data, run_response)
        _begin_request(request)
        try:
            async for response in original(
                model,
                messages,
                assistant_message,
                stream_data,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
            ):
                yield response
        except BaseException:
            if _finish_request(request):
                _retain_metered_message(messages, assistant_message)
            raise
        _finish_request(request)

    return stream


def install_patch() -> None:
    """Install the repair once for the pinned SDK before using owned storage."""
    global _PATCHED
    with _LOCK:
        if _PATCHED:
            return
        if version("agno") != _SUPPORTED_VERSION:
            msg = "Unsupported Agno interrupted-message implementation"
            raise RuntimeError(msg)
        agent_run.flush_in_flight_messages_on_error = cast("Any", _flush_messages)
        team_run.flush_in_flight_messages_on_error_team = cast("Any", _flush_messages)
        agent_run._handle_run_cancellation = cast("Any", _with_current_messages(agent_run._handle_run_cancellation))
        team_run._handle_team_run_cancellation = cast(
            "Any",
            _with_current_messages(team_run._handle_team_run_cancellation),
        )
        Model.process_response_stream = cast("Any", _with_metered_messages(Model.process_response_stream))
        Model.aprocess_response_stream = cast("Any", _with_metered_messages_async(Model.aprocess_response_stream))
        Model._populate_assistant_message = cast("Any", _with_request_model(Model._populate_assistant_message))
        Model._populate_assistant_message_from_stream_data = cast(
            "Any",
            _with_request_model(Model._populate_assistant_message_from_stream_data),
        )
        _PATCHED = True
