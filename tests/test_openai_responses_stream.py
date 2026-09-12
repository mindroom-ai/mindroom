"""Exercise Responses stream completion through the real SDK and agent runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.exceptions import ModelProviderError
from agno.media import Image
from agno.models.message import Message
from agno.models.openai import OpenAIResponses
from agno.run.agent import RunCompletedEvent, RunErrorEvent
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from openai import AsyncOpenAI, OpenAI

from mindroom.codex_model import CodexResponses
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.provider_media_fallback import install_provider_media_fallback

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from agno.models.response import ModelResponse


pytestmark = pytest.mark.asyncio


def _event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': 0, **fields})}\n\n"


def _response(response_id: str, status: str, output: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-astra",
        "status": status,
        "output": output or [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1,
        "top_p": 1,
        "usage": None,
        "error": None,
        "incomplete_details": None,
    }


def _created(response_id: str = "resp_unfinished") -> str:
    return _event("response.created", response=_response(response_id, "in_progress"))


def _text() -> str:
    return _event("response.output_text.delta", item_id="msg_answer", output_index=0, content_index=0, delta="Ready")


def _tool_stream() -> str:
    call = {
        "type": "function_call",
        "id": "fc_status",
        "call_id": "call_status",
        "name": "get_status",
        "arguments": "{}",
        "status": "completed",
    }
    return (
        _created("resp_tools")
        + _event("response.output_item.added", output_index=0, item={**call, "arguments": "", "status": "in_progress"})
        + _event("response.function_call_arguments.delta", output_index=0, item_id="fc_status", delta="{}")
        + _event("response.output_item.done", output_index=0, item=call)
        + _event("response.completed", response=_response("resp_tools", "completed", [call]))
    )


class _InterruptedStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, data: str) -> None:
        self.data = data.encode()

    def __iter__(self) -> Iterator[bytes]:
        yield self.data
        msg = "Connection dropped"
        raise httpx.ReadError(msg)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk


@asynccontextmanager
async def _model(*streams: str | httpx.Response, store: bool = True) -> AsyncIterator[MindRoomOpenAIResponses]:
    remaining = iter(streams)

    def respond(_request: httpx.Request) -> httpx.Response:
        response = next(remaining)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response)

    transport = httpx.MockTransport(respond)
    with OpenAI(api_key="test-key", max_retries=0, http_client=httpx.Client(transport=transport)) as client:
        async with AsyncOpenAI(
            api_key="test-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=transport),
        ) as async_client:
            yield MindRoomOpenAIResponses(id="gpt-6-astra", client=client, async_client=async_client, store=store)


async def _invoke(model: MindRoomOpenAIResponses, *, sync: bool) -> AsyncIterator[ModelResponse]:
    messages = [Message(role="user", content="Check status")]
    assistant = Message(role="assistant")
    if sync:
        for chunk in model.invoke_stream(messages, assistant):
            yield chunk
    else:
        async for chunk in model.ainvoke_stream(messages, assistant):
            yield chunk


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize(
    "stream",
    [
        "",
        _created(),
        _created() + _text(),
        _created()
        + _event(
            "response.failed",
            response={
                **_response("resp_unfinished", "failed"),
                "error": {"code": "server_error", "message": "Generation failed"},
            },
        ),
        _created()
        + _event(
            "response.incomplete",
            response={
                **_response("resp_unfinished", "incomplete"),
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        ),
    ],
    ids=["empty", "created-only", "partial-text", "failed", "incomplete"],
)
async def test_unsuccessful_stream_raises_without_publishing_response_id(stream: str, *, sync: bool) -> None:
    """EOF, failed, and incomplete responses must not become replay anchors."""
    async with _model(stream) as model:
        with pytest.raises(ModelProviderError):  # noqa: PT012 - inspect each chunk before failure
            async for chunk in _invoke(model, sync=sync):
                assert not chunk.provider_data or "response_id" not in chunk.provider_data


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize("store", [True, False], ids=["stored", "stateless"])
async def test_completed_text_publishes_response_id_only_at_completion(*, sync: bool, store: bool) -> None:
    """Successful completion is independent of usage metrics and storage mode."""
    stream = (
        _created("resp_answer") + _text() + _event("response.completed", response=_response("resp_answer", "completed"))
    )
    async with _model(stream, store=store) as model:
        chunks = [chunk async for chunk in _invoke(model, sync=sync)]

    assert "".join(chunk.content or "" for chunk in chunks) == "Ready"
    assert all(not chunk.provider_data or "response_id" not in chunk.provider_data for chunk in chunks[:-1])
    assert chunks[-1].provider_data == {"response_id": "resp_answer"}


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
async def test_completed_tool_stream_does_not_complete_the_next_invocation(*, sync: bool) -> None:
    """A valid tools-only response cannot hide a later truncated response."""
    async with _model(_tool_stream(), _created()) as model:
        chunks = [chunk async for chunk in _invoke(model, sync=sync)]
        assert [call["function"]["name"] for chunk in chunks for call in chunk.tool_calls] == ["get_status"]
        assert chunks[-1].provider_data == {"response_id": "resp_tools"}

        with pytest.raises(ModelProviderError, match=r"response\.completed"):
            _ = [chunk async for chunk in _invoke(model, sync=sync)]


async def test_concurrent_invocations_do_not_share_completion_state() -> None:
    """Completion of one request cannot validate another active request on the same model."""
    completed = _created("resp_answer") + _event("response.completed", response=_response("resp_answer", "completed"))
    async with _model(completed, _created()) as model:
        first = model.ainvoke_stream([Message(role="user", content="First")], Message(role="assistant"))
        second = model.ainvoke_stream([Message(role="user", content="Second")], Message(role="assistant"))
        await anext(first)
        await anext(second)
        _ = [chunk async for chunk in first]
        with pytest.raises(ModelProviderError, match=r"response\.completed"):
            _ = [chunk async for chunk in second]


async def test_cancelled_request_remains_cancelled() -> None:
    """Cancellation while waiting for the provider must not become a completion error."""
    entered = asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Future()
        raise AssertionError

    async with AsyncOpenAI(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client)
        task = asyncio.create_task(
            anext(model.ainvoke_stream([Message(role="user", content="Check")], Message(role="assistant"))),
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_closing_sync_stream_closes_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the wrapper must close the actual parent even when another reference keeps it alive."""
    async with _model(_created() + _text()) as model:
        messages = [Message(role="user", content="Check")]
        assistant = Message(role="assistant")
        parent = OpenAIResponses.invoke_stream(model, messages, assistant)
        monkeypatch.setattr(OpenAIResponses, "invoke_stream", lambda *_args: parent)
        stream = model.invoke_stream(messages, assistant)
        assert isinstance(stream, Generator)
        next(stream)
        stream.close()
        with pytest.raises(StopIteration):
            next(parent)


@pytest.mark.parametrize("cancel", [False, True], ids=["close", "cancel"])
async def test_closing_async_stream_closes_parent(monkeypatch: pytest.MonkeyPatch, *, cancel: bool) -> None:
    """Early close or cancellation after a chunk must finalize the actual superclass stream."""
    async with _model(_created() + _text()) as model:
        messages = [Message(role="user", content="Check")]
        assistant = Message(role="assistant")
        parent = OpenAIResponses.ainvoke_stream(model, messages, assistant)
        monkeypatch.setattr(OpenAIResponses, "ainvoke_stream", lambda *_args: parent)
        stream = model.ainvoke_stream(messages, assistant)
        assert isinstance(stream, AsyncGenerator)
        await anext(stream)
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await stream.athrow(asyncio.CancelledError())
        else:
            await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(parent)


async def test_agent_records_truncated_followup_as_error_after_completed_tool(tmp_path: Path) -> None:
    """An earlier successful tool must not allow a truncated final response into history."""

    def get_status() -> str:
        """Return the local status."""
        return "ready"

    db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
    async with _model(_tool_stream(), _created()) as model:
        agent = Agent(id="status_agent", model=model, db=db, tools=[get_status], add_history_to_context=True)
        events = [
            event
            async for event in agent.arun("Check status", session_id="status_session", stream=True, stream_events=True)
        ]
        assert not any(isinstance(event, RunCompletedEvent) for event in events)
        assert any(isinstance(event, RunErrorEvent) for event in events)

        session = db.get_session("status_session", SessionType.AGENT)
        assert isinstance(session, AgentSession)
        run = session.runs[-1]
        assert RunStatus(run.status) is RunStatus.error
        assert run.tools is not None
        assert [(tool.tool_name, tool.result) for tool in run.tools] == [("get_status", "ready")]
        history = [*session.get_messages(agent_id="status_agent"), Message(role="user", content="Follow up")]
        assert "previous_response_id" not in model.get_request_params(messages=history)


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("stream", "disconnect"),
    [
        (_created() + _text(), False),
        (_created() + _text(), True),
        (_tool_stream().split("event: response.completed")[0], False),
        (_tool_stream().split("event: response.completed")[0], True),
        (
            _created("resp_answer")
            + _text()
            + _event("response.completed", response=_response("resp_answer", "completed")),
            True,
        ),
    ],
    ids=[
        "partial-text-eof",
        "partial-text-transport-error",
        "partial-tool-eof",
        "partial-tool-transport-error",
        "completed-transport-error",
    ],
)
async def test_agent_does_not_retry_incomplete_stream(
    tmp_path: Path,
    stream: str,
    *,
    sync: bool,
    disconnect: bool,
) -> None:
    """Retries must not combine partial output with a successful response or execute its tools."""
    executed_tools: list[str] = []

    def get_status() -> str:
        """Return the local status."""
        executed_tools.append("get_status")
        return "ready"

    completed = (
        _created("resp_no_tools")
        + _text()
        + _event("response.completed", response=_response("resp_no_tools", "completed"))
    )
    db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
    first_response = (
        httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_InterruptedStream(stream))
        if disconnect
        else stream
    )
    async with _model(first_response, completed, completed) as model:
        model.retries = 1
        model.delay_between_retries = 0
        agent = Agent(id="status_agent", model=model, db=db, tools=[get_status], add_history_to_context=True)
        if sync:
            events = list(agent.run("Check status", session_id="status_session", stream=True, stream_events=True))
        else:
            events = [
                event
                async for event in agent.arun(
                    "Check status",
                    session_id="status_session",
                    stream=True,
                    stream_events=True,
                )
            ]

        assert executed_tools == []
        assert not any(isinstance(event, RunCompletedEvent) for event in events)
        assert any(isinstance(event, RunErrorEvent) for event in events)
        session = db.get_session("status_session", SessionType.AGENT)
        assert isinstance(session, AgentSession)
        assert RunStatus(session.runs[-1].status) is RunStatus.error
        history = [*session.get_messages(agent_id="status_agent"), Message(role="user", content="Follow up")]
        assert "previous_response_id" not in model.get_request_params(messages=history)


@pytest.mark.parametrize("disconnect", [False, True], ids=["eof", "transport-error"])
async def test_codex_media_fallback_does_not_retry_incomplete_tool_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    disconnect: bool,
) -> None:
    """A blocking Codex call must not execute failed-stream tools through media fallback."""
    executed_tools: list[str] = []

    def get_status() -> str:
        """Return the local status."""
        executed_tools.append("get_status")
        return "ready"

    partial = _tool_stream().split("event: response.completed")[0]
    first_response = (
        httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_InterruptedStream(partial))
        if disconnect
        else partial
    )
    completed = (
        _created("resp_no_tools")
        + _text()
        + _event("response.completed", response=_response("resp_no_tools", "completed"))
    )
    db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
    async with _model(first_response, completed, completed) as sdk_model:
        model = CodexResponses(id="gpt-6-astra")
        monkeypatch.setattr(model, "get_async_client", lambda: sdk_model.async_client)
        install_provider_media_fallback(model, fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT)
        agent = Agent(id="status_agent", model=model, db=db, tools=[get_status])
        result = await agent.arun(
            "Check status",
            session_id="status_session",
            images=[Image(url="https://example.com/image.png")],
        )

    assert executed_tools == []
    assert result.status is RunStatus.error
    session = db.get_session("status_session", SessionType.AGENT)
    assert isinstance(session, AgentSession)
    assert RunStatus(session.runs[-1].status) is RunStatus.error
    assert session.get_messages(agent_id="status_agent") == []


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize("status_code", [429, 503, None], ids=["429", "503", "transport-error"])
async def test_agent_still_retries_transient_provider_errors(status_code: int | None, *, sync: bool) -> None:
    """The incomplete-stream guard must preserve ordinary provider retries before any output."""
    failed = (
        httpx.Response(status_code, json={"error": {"message": "Temporarily unavailable", "type": "server_error"}})
        if status_code is not None
        else httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_InterruptedStream(""))
    )
    completed = (
        _created("resp_answer") + _text() + _event("response.completed", response=_response("resp_answer", "completed"))
    )
    async with _model(failed, completed) as model:
        model.retries = 1
        model.delay_between_retries = 0
        agent = Agent(model=model)
        if sync:
            events = list(agent.run("Check status", stream=True, stream_events=True))
        else:
            events = [event async for event in agent.arun("Check status", stream=True, stream_events=True)]

    assert not any(isinstance(event, RunErrorEvent) for event in events)
    assert [event.content for event in events if isinstance(event, RunCompletedEvent)] == ["Ready"]
