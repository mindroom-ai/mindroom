"""Queued-message notices at real Agno approval continuation boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.team import Team
from agno.tools.function import Function

from mindroom.ai_runtime import install_queued_message_notice_hook, queued_message_signal_context
from mindroom.approval_receipt import approval_receipt_context, install_approval_receipt_hooks
from tests.history_helpers import RecordingModel

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from typing import Any

_NOTICE = "Pause tool use and summarize before handling the newer message."


@dataclass
class _QueuedState:
    pending: bool = False

    def has_pending_human_messages(self) -> bool:
        return self.pending


def _tool_calls() -> list[dict[str, Any]]:
    return [
        {
            "id": f"call-{item}",
            "type": "function",
            "function": {"name": "read_item", "arguments": f'{{"item": {item}}}'},
        }
        for item in (1, 2)
    ]


@dataclass
class _ContinuationModel(RecordingModel):
    requests: list[list[Message]] = field(default_factory=list)
    request_tools: bool = True
    fail_after_pause: bool = False
    failures_remaining: int = 0
    approval_receipt_after_response_id: bool = True

    def _response(self, messages: list[Message]) -> ModelResponse:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if self.request_tools and len(self.requests) == 1:
            return ModelResponse(
                role="assistant",
                tool_calls=_tool_calls(),
                provider_data={"response_id": "response-tool-batch"},
            )
        if self.fail_after_pause:
            raise ModelProviderError(message="Test provider unavailable", status_code=503)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ModelProviderError(message="Test provider retry", status_code=503)
        return ModelResponse(role="assistant", content="Pausing here for the newer message.")

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        return self._response(messages)

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        yield self._response(messages)


@pytest.mark.parametrize("entity_type", [Agent, Team], ids=["agent", "team"])
@pytest.mark.parametrize("stream", [False, True], ids=["response", "stream"])
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
@pytest.mark.asyncio
async def test_approved_batch_notifies_next_real_model_request(
    entity_type: type[Agent | Team],
    *,
    stream: bool,
    fallback: bool,
) -> None:
    """A queued follow-up reaches the model after every resumed tool result."""
    state = _QueuedState()
    executed: list[int] = []

    def read_item(item: int) -> str:
        executed.append(item)
        state.pending = True
        return f"Result {item}"

    model = _ContinuationModel(id="primary", fail_after_pause=fallback)
    fallback_model = _ContinuationModel(id="fallback", request_tools=False)
    install_queued_message_notice_hook(model, notice_text=_NOTICE)
    kwargs: dict[str, Any] = {
        "model": model,
        "tools": [Function(name="read_item", entrypoint=read_item, requires_confirmation=True)],
        "telemetry": False,
    }
    if entity_type is Team:
        kwargs["members"] = []
    if fallback:
        kwargs["fallback_models"] = [fallback_model]
    entity = entity_type(**kwargs)

    with queued_message_signal_context(state), approval_receipt_context("Trusted approval receipt."):
        paused = await entity.arun("Read both items.", session_id="notice-session", stream=False)
        assert paused.status == RunStatus.paused
        assert len(paused.requirements or []) == 2
        assert not executed
        install_approval_receipt_hooks(model, entity.fallback_config)
        for requirement in paused.requirements or []:
            requirement.confirm()
        if stream:
            outputs = [
                output
                async for output in entity.acontinue_run(
                    run_response=paused,
                    requirements=paused.requirements,
                    stream=True,
                    stream_events=True,
                    yield_run_output=True,
                )
            ]
            resumed = next(output for output in reversed(outputs) if isinstance(output, (RunOutput, TeamRunOutput)))
        else:
            resumed = await entity.acontinue_run(run_response=paused, requirements=paused.requirements, stream=False)

    assert resumed.status == RunStatus.completed
    assert executed == [1, 2]
    requests = [model.requests[-1]]
    if fallback:
        assert entity.fallback_config is not None
        resolved_fallback = entity.fallback_config.on_error[0]
        assert isinstance(resolved_fallback, _ContinuationModel)
        assert len(resolved_fallback.requests) == 1
        requests.append(resolved_fallback.requests[0])
    for messages in requests:
        if not stream:
            assert any(
                message.role == "system" and message.content == "Trusted approval receipt." for message in messages
            )
        assert [message.tool_call_id for message in messages if message.role == "tool"] == ["call-1", "call-2"]
        assert sum(message.content == _NOTICE for message in messages) == 1
        assert messages[-1].content == _NOTICE


@pytest.mark.parametrize("stream", [False, True], ids=["response", "stream"])
@pytest.mark.parametrize(
    "boundary", ["resolved", "system", "developer", "unresolved", "stop", "newer-input", "no-queue"]
)
@pytest.mark.asyncio
async def test_response_entry_only_notifies_a_resolved_current_batch(boundary: str, *, stream: bool) -> None:
    """Request entry must not split a batch, override stop-after, or revisit history."""
    model = _ContinuationModel(id="boundary", request_tools=False)
    install_queued_message_notice_hook(model, notice_text=_NOTICE)
    install_queued_message_notice_hook(model, notice_text=_NOTICE)
    messages = [
        Message(role="user", content="Read both items."),
        Message(role="assistant", tool_calls=_tool_calls()),
        Message(role="tool", tool_call_id="call-1", content="Result 1", stop_after_tool_call=boundary == "stop"),
    ]
    if boundary != "unresolved":
        messages.append(Message(role="tool", tool_call_id="call-2", content="Result 2"))
    if boundary in {"system", "developer"}:
        messages.insert(2, Message(role=boundary, content="Trusted runtime context."))
    if boundary == "newer-input":
        messages.append(Message(role="user", content="A different request."))

    with queued_message_signal_context(_QueuedState(pending=boundary != "no-queue")):
        if stream:
            async for _ in model.aresponse_stream(messages=messages):
                pass
        else:
            await model.aresponse(messages=messages)

    notices = [message for message in model.requests[0] if message.content == _NOTICE]
    assert len(notices) == (1 if boundary in {"resolved", "system", "developer"} else 0)


@pytest.mark.parametrize("stream", [False, True], ids=["response", "stream"])
@pytest.mark.parametrize("formatted", [False, True], ids=["resumed-results", "formatted-results"])
@pytest.mark.asyncio
async def test_notice_survives_provider_retry_without_duplicates(*, stream: bool, formatted: bool) -> None:
    """Provider retries and the ordinary tool callback share exactly one notice."""
    model = _ContinuationModel(
        id="retry",
        request_tools=False,
        failures_remaining=1,
        retries=1,
        delay_between_retries=0,
    )
    install_queued_message_notice_hook(model, notice_text=_NOTICE)
    messages = [Message(role="assistant", tool_calls=_tool_calls())]
    results = [Message(role="tool", tool_call_id=f"call-{item}", content=f"Result {item}") for item in (1, 2)]

    with queued_message_signal_context(_QueuedState(pending=True)):
        if formatted:
            model.format_function_call_results(messages=messages, function_call_results=results)
        else:
            messages.extend(results)
        if stream:
            async for _ in model.aresponse_stream(messages):
                pass
        else:
            await model.aresponse(messages)

    assert len(model.requests) == 2
    for request in model.requests:
        assert sum(message.content == _NOTICE for message in request) == 1
        assert request[-1].content == _NOTICE


@pytest.mark.asyncio
async def test_notice_hook_closes_the_wrapped_response_stream() -> None:
    """Closing a wrapped stream must finish its underlying response immediately."""
    model = _ContinuationModel(id="closing", request_tools=False)
    closed: list[bool] = []

    async def response_stream(_messages: list[Message]) -> AsyncGenerator[ModelResponse]:
        try:
            yield ModelResponse(content="partial response")
        finally:
            closed.append(True)

    vars(model)["aresponse_stream"] = response_stream
    install_queued_message_notice_hook(model, notice_text=_NOTICE)
    stream = cast("AsyncGenerator[ModelResponse]", model.aresponse_stream(messages=[]))
    await anext(stream)
    await stream.aclose()

    assert closed == [True]
