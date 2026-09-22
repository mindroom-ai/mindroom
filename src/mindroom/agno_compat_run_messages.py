"""Preserve Agno's current request messages when a run is interrupted."""

from __future__ import annotations

import threading
from functools import wraps
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _run as agent_run
from agno.models.base import Model
from agno.team import _run as team_run

from mindroom.usage_storage import TOKEN_FIELDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.run.messages import RunMessages
    from agno.run.team import TeamRunOutput

_PATCHED = False
_LOCK = threading.Lock()


# AGNO_COMPAT: Terminal cleanup retains stale checkpoint or continuation messages.
# Reason: Agent and Team flush in-flight messages only if the saved list is empty,
# so later requests disappear from failed/cancelled runs despite retained totals.
# Upstream issue: No matching issue identified; terminal snapshot refresh is untracked.
# Upstream PR: None identified.
# Remove when: Both error and cancellation cleanup save the latest in-flight
# messages, including resumed runs, while respecting add_to_agent_memory.
# Coverage: tests/test_agno_compat_run_messages.py.
def _flush_messages(run_response: RunOutput | TeamRunOutput, run_messages: RunMessages | None) -> None:
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
# tests/test_openai_responses_stream.py::test_received_request_usage_survives_task_cancellation.
def _retain_metered_message(messages: list[Message], assistant_message: Message) -> None:
    metrics = assistant_message.metrics.to_dict()
    if any(metrics.get(key, 0) for key in TOKEN_FIELDS) and not any(
        message is assistant_message for message in messages
    ):
        messages.append(assistant_message)


def _with_metered_messages(
    original: Callable[..., Iterator[ModelResponse]],
) -> Callable[..., Iterator[ModelResponse]]:
    @wraps(original)
    def stream(
        model: Model,
        messages: list[Message],
        assistant_message: Message,
        *args: object,
        **kwargs: object,
    ) -> Iterator[ModelResponse]:
        try:
            yield from original(model, messages, assistant_message, *args, **kwargs)
        except BaseException:
            _retain_metered_message(messages, assistant_message)
            raise

    return stream


def _with_metered_messages_async(
    original: Callable[..., AsyncIterator[ModelResponse]],
) -> Callable[..., AsyncIterator[ModelResponse]]:
    @wraps(original)
    async def stream(
        model: Model,
        messages: list[Message],
        assistant_message: Message,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        try:
            async for response in original(model, messages, assistant_message, *args, **kwargs):
                yield response
        except BaseException:
            _retain_metered_message(messages, assistant_message)
            raise

    return stream


def install_patch() -> None:
    """Install the repair once for the pinned SDK before using owned storage."""
    global _PATCHED
    with _LOCK:
        if _PATCHED:
            return
        if version("agno") != "3.0.9":
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
        _PATCHED = True
