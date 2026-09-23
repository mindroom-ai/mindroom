"""Exercise Responses stream completion through the real SDK and agent runtime."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.exceptions import ModelProviderError
from agno.media import Image
from agno.models.message import Message
from agno.models.openai import OpenAIResponses
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.cancel import acancel_run
from agno.session.agent import AgentSession
from openai import AsyncOpenAI, OpenAI

from mindroom.agent_storage import create_state_storage
from mindroom.agno_compat_session_persistence import drain_agent_cancellation
from mindroom.codex_model import CodexResponses
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.error_handling import IncompleteResponsesStreamError
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.provider_media_fallback import install_provider_media_fallback
from mindroom.system_prompt import render_session_context
from mindroom.usage_stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from agno.models.response import ModelResponse
    from openai.types.responses import ResponseStreamEvent


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
    def __init__(self, data: str, error: Exception | None = None) -> None:
        self.data = data.encode()
        self.error = error

    def __iter__(self) -> Iterator[bytes]:
        yield self.data
        msg = "Connection dropped"
        raise self.error or httpx.ReadError(msg)

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
            model = MindRoomOpenAIResponses(id="gpt-6-astra", client=client, async_client=async_client, store=store)
            install_provider_media_fallback(model, fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT)
            yield model


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
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_shared_prompt_breakpoint_reaches_api(
    *,
    sync: bool,
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All Responses request paths serialize the stable boundary through the real SDK."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        completed = _response("resp_cached", "completed")
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_created("resp_cached") + _event("response.completed", response=completed),
            )
        return httpx.Response(200, json=completed)

    transport = httpx.MockTransport(respond)
    with OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport)) as client:
        async with AsyncOpenAI(api_key="test-key", http_client=httpx.AsyncClient(transport=transport)) as async_client:
            model = MindRoomOpenAIResponses(id="gpt-6-astra", client=client, async_client=async_client, store=False)
            for context in ("First conversation.", "Different conversation."):
                messages = [
                    Message(role="system", content="Shared instructions.\n" + render_session_context(context)),
                    Message(role="user", content="Hello"),
                ]
                assistant = Message(role="assistant")
                if stream and sync:
                    list(model.invoke_stream(messages, assistant))
                elif stream:
                    async for _ in model.ainvoke_stream(messages, assistant):
                        pass
                elif sync:
                    model.invoke(messages, assistant)
                else:
                    await model.ainvoke(messages, assistant)

    first, second = requests
    assert first["input"][0] == second["input"][0]
    assert first["input"][0] == {
        "role": "developer",
        "content": [
            {"type": "input_text", "text": "Shared instructions.\n", "prompt_cache_breakpoint": {"mode": "explicit"}},
        ],
    }
    assert first["input"][1] != second["input"][1]
    assert "prompt_cache_options" not in first


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
@pytest.mark.parametrize("portable", [False, True])
async def test_completed_text_publishes_response_id_only_at_completion(
    *,
    sync: bool,
    store: bool,
    portable: bool,
) -> None:
    """Successful completion is independent of usage metrics and storage mode."""
    stream = (
        _created("resp_answer") + _text() + _event("response.completed", response=_response("resp_answer", "completed"))
    )
    async with _model(stream, store=store) as model:
        model.configure_portable_replay(enabled=portable)
        chunks = [chunk async for chunk in _invoke(model, sync=sync)]

    assert "".join(chunk.content or "" for chunk in chunks) == "Ready"
    assert all(
        not chunk.provider_data or not {"response_id", "mindroom_portable_replay"} & chunk.provider_data.keys()
        for chunk in chunks[:-1]
    )
    assert chunks[-1].provider_data == {
        "response_id": "resp_answer",
        "mindroom_response_stored": store,
        "mindroom_portable_replay": portable,
        "mindroom_native_compaction": None,
    }


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
async def test_completed_tool_stream_does_not_complete_the_next_invocation(*, sync: bool) -> None:
    """A valid tools-only response cannot hide a later truncated response."""
    async with _model(_tool_stream(), _created()) as model:
        chunks = [chunk async for chunk in _invoke(model, sync=sync)]
        assert [call["function"]["name"] for chunk in chunks for call in chunk.tool_calls] == ["get_status"]
        assert chunks[-1].provider_data == {
            "response_id": "resp_tools",
            "mindroom_response_stored": True,
            "mindroom_portable_replay": False,
            "mindroom_native_compaction": None,
        }

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
    ("status", "disconnect", "reported_usage"),
    [
        ("completed", False, True),
        ("incomplete", False, True),
        ("failed", False, True),
        ("completed", True, True),
        ("incomplete", False, False),
        ("failed", False, False),
        ("completed", True, False),
    ],
    ids=[
        "completed",
        "incomplete",
        "failed",
        "disconnect",
        "unmetered-incomplete",
        "unmetered-failed",
        "unmetered-disconnect",
    ],
)
async def test_terminal_usage_survives_stream_failure(
    tmp_path: Path,
    status: str,
    *,
    sync: bool,
    disconnect: bool,
    reported_usage: bool,
) -> None:
    """Received provider counters reach durable usage exactly once, including unsuccessful runs."""
    response = _response("resp_usage", status)
    if reported_usage:
        response["usage"] = {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 40},
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 60},
            "total_tokens": 1100,
        }
    if status == "failed":
        response["error"] = {"code": "server_error", "message": "Generation failed"}
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    stream = _created("resp_usage") + _text() + _event(f"response.{status}", response=response)
    provider_response = (
        httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_InterruptedStream(stream))
        if disconnect
        else stream
    )
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    assert isinstance(storage, SqliteDb)
    try:
        async with _model(provider_response) as model:
            agent = Agent(id="status_agent", model=model, db=storage, add_history_to_context=True)
            if sync:
                list(agent.run("Check status", session_id="status_session", stream=True))
            else:
                async for _ in agent.arun("Check status", session_id="status_session", stream=True):
                    pass
            session = storage.get_session("status_session", SessionType.AGENT)
            assert isinstance(session, AgentSession)
            completed = status == "completed" and not disconnect
            assert RunStatus(session.runs[-1].status) is (RunStatus.completed if completed else RunStatus.error)
            if not completed:
                history = [*session.get_messages(agent_id="status_agent"), Message(role="user", content="Follow up")]
                assert "previous_response_id" not in model.get_request_params(messages=history)
        with sqlite3.connect(storage.db_file) as connection:
            rows = connection.execute("SELECT usage_data FROM status_sessions_usage").fetchall()
        assert len(rows) == 1
        usage = json.loads(rows[0][0])
        metrics = usage["metrics"]
        expected = {
            "input_tokens": 1000,
            "cache_read_tokens": 800,
            "cache_write_tokens": 40,
            "output_tokens": 100,
            "reasoning_tokens": 60,
            "total_tokens": 1100,
        }
        if not reported_usage:
            expected = dict.fromkeys(expected, 0)
        assert {key: metrics.get(key, 0) for key in expected} == expected
        model_metrics = metrics.get("details", {}).get("model", [])
        if reported_usage:
            assert len(model_metrics) == 1
            assert {key: model_metrics[0][key] for key in expected} == expected
            assert len(usage["requests"]) == 1
            assert {key: usage["requests"][0]["metrics"].get(key, 0) for key in expected} == expected
            assert usage["requests"][0]["created_at"] > 0
        else:
            assert all(not item.get(key, 0) for item in model_metrics for key in expected)
    finally:
        storage.close()


def _metered_completion(response_id: str) -> dict[str, object]:
    completed = _response(response_id, "completed")
    completed["usage"] = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 800},
        "output_tokens": 100,
        "output_tokens_details": {"reasoning_tokens": 60},
        "total_tokens": 1100,
    }
    return completed


class _HeldStream(httpx.AsyncByteStream):
    """Deliver one metered completion, then keep the response open before EOF."""

    def __init__(self, response_id: str) -> None:
        self.completed = _metered_completion(response_id)
        self.sent = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield (_created() + _text() + _event("response.completed", response=self.completed)).encode()
        self.sent.set()
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed.set()

    def response(self) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self)


def _assert_reported_usage(
    storage: SqliteDb,
    *,
    total: int,
    requests: list[tuple[int, int]],
    status: RunStatus = RunStatus.cancelled,
) -> None:
    """The stopped run, session totals, and durable request details agree on reported usage."""
    session = storage.get_session("session", session_type=SessionType.AGENT)
    assert isinstance(session, AgentSession)
    run = session.runs[-1]
    assert run.status == status
    assert run.metrics is not None
    assert run.metrics.total_tokens == total
    assert session.session_data["session_metrics"]["total_tokens"] == total
    with sqlite3.connect(storage.db_file) as connection:
        rows = connection.execute("SELECT usage_data FROM status_sessions_usage").fetchall()
    assert len(rows) == 1
    usage = json.loads(rows[0][0])
    assert usage["metrics"]["total_tokens"] == total
    assert [
        (request["metrics"]["total_tokens"], request["metrics"]["cache_read_tokens"]) for request in usage["requests"]
    ] == requests


async def test_received_request_usage_survives_task_cancellation(tmp_path: Path) -> None:
    """Cancellation while awaiting EOF persists already-received request counters."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"status": AgentConfig(display_name="Status")})
    storage = create_state_storage(
        "status",
        tmp_path / "agents/status",
        subdir="sessions",
        session_table="status_sessions",
    )
    held = _HeldStream("resp_cancelled")
    try:
        async with _model(held.response()) as model:
            agent = Agent(id="status", model=model, db=storage, telemetry=False)

            async def run() -> None:
                async with drain_agent_cancellation(agent, "run") as bind:
                    with bind():
                        async for _ in agent.arun("Check status", run_id="run", session_id="session", stream=True):
                            pass

            task = asyncio.create_task(run())
            try:
                async with asyncio.timeout(5):
                    await held.sent.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        assert session.runs[-1].status == RunStatus.cancelled
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 1100
        assert len(report.request_breakdown) == 1
        assert report.request_breakdown[0].totals.total_tokens == 1100
        assert report.request_breakdown[0].totals.cache_read_tokens == 800
        assert report.request_coverage.unavailable_sources == 0
    finally:
        storage.close()


@pytest.mark.parametrize(
    ("stop", "status"),
    [("close", RunStatus.cancelled), ("throw", RunStatus.error)],
    ids=["closed", "failed"],
)
async def test_received_request_usage_survives_abandoned_stream(
    tmp_path: Path,
    *,
    stop: str,
    status: RunStatus,
) -> None:
    """Stopping the run stream at the terminal chunk still records its reported usage."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    assert isinstance(storage, SqliteDb)
    held = _HeldStream("resp_closed")
    try:
        async with _model(held.response()) as model:
            agent = Agent(id="status", model=model, db=storage, telemetry=False)
            async with asyncio.timeout(5):
                async with drain_agent_cancellation(agent, "run") as bind:
                    with bind():
                        events = agent.arun("Check status", run_id="run", session_id="session", stream=True)
                    while True:
                        with bind():
                            event = await anext(events)
                        # The provider's completion reaches the consumer before the stream ends.
                        if isinstance(event, RunContentEvent) and (event.model_provider_data or {}).get("response_id"):
                            break
                    with bind():
                        if stop == "throw":
                            assert isinstance(await events.athrow(RuntimeError("Consumer failed")), RunErrorEvent)
                        await events.aclose()
                # Garbage collection finalizes the abandoned model stream after persistence.
                await held.closed.wait()
        _assert_reported_usage(storage, total=1100, requests=[(1100, 800)], status=status)
    finally:
        storage.close()


@pytest.mark.parametrize("retried", [False, True], ids=["first-attempt", "retried"])
async def test_received_request_usage_survives_cancel_request(tmp_path: Path, *, retried: bool) -> None:
    """A cancellation checked at the terminal chunk counts reported usage once."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    assert isinstance(storage, SqliteDb)
    held = _HeldStream("resp_cancel_requested")
    failed = {
        **_metered_completion("resp_failed"),
        "status": "failed",
        "error": {"code": "server_error", "message": "Generation failed"},
    }
    streams = [_created("resp_failed") + _event("response.failed", response=failed)] if retried else []
    run_output: RunOutput | None = None
    try:
        async with _model(*streams, held.response()) as model:
            model.retries = 1
            model.delay_between_retries = 0
            agent = Agent(id="status", model=model, db=storage, telemetry=False)
            async with asyncio.timeout(5):
                async for event in agent.arun(
                    "Check status",
                    run_id="run",
                    session_id="session",
                    stream=True,
                    yield_run_output=True,
                ):
                    if isinstance(event, RunContentEvent) and event.content:
                        # Agno checks this request when the next chunk, the completion, arrives.
                        assert await acancel_run("run")
                    if isinstance(event, RunOutput):
                        run_output = event
                # Late finalization of the abandoned model stream must not count it again.
                await held.closed.wait()
        total = 2200 if retried else 1100
        assert run_output is not None
        assert run_output.status == RunStatus.cancelled
        assert run_output.metrics is not None
        assert run_output.metrics.total_tokens == total
        # Counters combined across attempts stay aggregate-only.
        _assert_reported_usage(storage, total=total, requests=[] if retried else [(1100, 800)])
    finally:
        storage.close()


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize("complete_retry", [True, False], ids=["completed", "exhausted"])
async def test_terminal_usage_survives_retry(tmp_path: Path, *, sync: bool, complete_retry: bool) -> None:
    """Retry totals survive without inventing one provider request from two attempts."""
    failed = _response("resp_failed", "failed")
    failed["error"] = {"code": "server_error", "message": "Generation failed"}
    failed["usage"] = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 40},
        "output_tokens": 100,
        "output_tokens_details": {"reasoning_tokens": 60},
        "total_tokens": 1100,
    }
    retry_status = "completed" if complete_retry else "failed"
    retried = {
        **_response("resp_retry", retry_status),
        "usage": failed["usage"],
        "error": None if complete_retry else failed["error"],
    }
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"status": AgentConfig(display_name="Status")})
    storage = create_state_storage(
        "status",
        tmp_path / "agents/status",
        subdir="sessions",
        session_table="status_sessions",
    )
    assert isinstance(storage, SqliteDb)
    try:
        async with _model(
            _created("resp_failed") + _event("response.failed", response=failed),
            _created("resp_retry")
            + (_text() if complete_retry else "")
            + _event(f"response.{retry_status}", response=retried),
        ) as model:
            model.retries = 1
            model.delay_between_retries = 0
            agent = Agent(id="status", model=model, db=storage)
            if sync:
                list(agent.run("Check status", session_id="status_session", stream=True))
            else:
                async for _ in agent.arun("Check status", session_id="status_session", stream=True):
                    pass
        session = storage.get_session("status_session", SessionType.AGENT)
        assert isinstance(session, AgentSession)
        result = session.runs[-1]
        assert RunStatus(result.status) is (RunStatus.completed if complete_retry else RunStatus.error)
        if complete_retry:
            assert result.content == "Ready"
        assert result.metrics is not None
        assert result.metrics.input_tokens == 2000
        assert result.metrics.cache_read_tokens == 1600
        assert result.metrics.cache_write_tokens == 80
        assert result.metrics.output_tokens == 200
        assert result.metrics.reasoning_tokens == 120
        assert result.metrics.total_tokens == 2200
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 2200
        assert report.to_dict()["request_breakdown"] == []
        assert report.request_coverage is not None
        assert report.request_coverage.unavailable_sources == 1
    finally:
        storage.close()


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_codex_blocking_usage_is_not_counted_twice(
    tmp_path: Path,
    *,
    sync: bool,
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent totals, assistant requests, and exports agree for Codex's stream-only endpoint."""
    completed = _response("resp_completed", "completed")
    completed["usage"] = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 40},
        "output_tokens": 100,
        "output_tokens_details": {"reasoning_tokens": 60},
        "total_tokens": 1100,
    }
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"status": AgentConfig(display_name="Status")})
    storage = create_state_storage(
        "status",
        tmp_path / "agents/status",
        subdir="sessions",
        session_table="status_sessions",
    )
    assert isinstance(storage, SqliteDb)
    try:
        async with _model(_created() + _text() + _event("response.completed", response=completed)) as sdk_model:
            model = CodexResponses(id="gpt-6-astra")
            monkeypatch.setattr(model, "get_client", lambda: sdk_model.client)
            monkeypatch.setattr(model, "get_async_client", lambda: sdk_model.async_client)
            agent = Agent(id="status", model=model, db=storage, telemetry=False)
            if sync and stream:
                list(agent.run("Check status", session_id="status_session", stream=True))
            elif sync:
                agent.run("Check status", session_id="status_session")
            elif stream:
                async for _ in agent.arun("Check status", session_id="status_session", stream=True):
                    pass
            else:
                await agent.arun("Check status", session_id="status_session")
        session = storage.get_session("status_session", SessionType.AGENT)
        assert isinstance(session, AgentSession)
        run = session.runs[-1]
        assert RunStatus(run.status) is RunStatus.completed
        assert run.content == "Ready"
        assert run.metrics is not None
        assert run.metrics.input_tokens == 1000
        assert run.messages is not None
        assistant = next(message for message in run.messages if message.role == "assistant")
        assert assistant.content == "Ready"
        assert assistant.provider_data is not None
        assert assistant.provider_data["response_id"] == "resp_completed"
        assert assistant.metrics.input_tokens == 1000
        assert assistant.metrics.cache_read_tokens == 800
        assert assistant.metrics.cache_write_tokens == 40
        assert assistant.metrics.output_tokens == 100
        assert assistant.metrics.reasoning_tokens == 60
        assert assistant.metrics.total_tokens == 1100
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 1100
        assert [row["totals"]["total_tokens"] for row in report.to_dict()["request_breakdown"]] == [1100]
        assert report.request_coverage is not None
        assert report.request_coverage.unavailable_sources == 0
    finally:
        storage.close()


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("stream", "disconnect"),
    [
        (
            _event(
                kind,
                response=_response(
                    "resp_snapshot",
                    "in_progress",
                    [
                        {
                            "type": "function_call",
                            "id": "fc_snapshot",
                            "call_id": "call_snapshot",
                            "name": "get_status",
                            "arguments": "{}",
                        },
                    ],
                ),
            ),
            True,
        )
        for kind in ("response.created", "response.in_progress")
    ]
    + [
        (_tool_stream().split("event: response.output_item.done")[0], True),
        (_created() + _event("response.web_search_call.in_progress", item_id="ws_search", output_index=0), True),
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
        "created-with-tool-snapshot",
        "in-progress-with-tool-snapshot",
        "tool-start-transport-error",
        "hosted-tool-start-transport-error",
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
@pytest.mark.parametrize(
    "lifecycle",
    ["", _created(), _created() + _event("response.in_progress", response=_response("resp_unfinished", "in_progress"))],
    ids=["before-events", "after-created", "after-in-progress"],
)
async def test_agent_still_retries_transient_provider_errors(
    status_code: int | None,
    lifecycle: str,
    *,
    sync: bool,
) -> None:
    """The incomplete-stream guard must preserve ordinary provider retries before any output."""
    failed = (
        httpx.Response(status_code, json={"error": {"message": "Temporarily unavailable", "type": "server_error"}})
        if status_code is not None
        else httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_InterruptedStream(lifecycle))
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


@pytest.mark.parametrize(
    ("prefix", "recovered"),
    [
        pytest.param(_created(), True, id="created-only"),
        pytest.param(
            _created() + _event("response.in_progress", response=_response("resp_unfinished", "in_progress")),
            True,
            id="in-progress-only",
        ),
        pytest.param(_created() + _text(), False, id="partial-text"),
        pytest.param(
            _created()
            + _event("response.reasoning_summary_text.delta", item_id="rs_check", output_index=0, delta="Checking"),
            False,
            id="partial-reasoning",
        ),
        pytest.param(_tool_stream().split("event: response.output_item.done")[0], False, id="partial-tool"),
        pytest.param(
            _created() + _event("response.web_search_call.in_progress", item_id="ws_search", output_index=0),
            False,
            id="hosted-tool-started",
        ),
    ],
)
async def test_media_hook_preserves_safe_followup_retries(prefix: str, *, recovered: bool) -> None:
    """Retry only empty follow-up streams without executing a completed tool again."""
    executed_tools: list[str] = []

    def get_status() -> str:
        """Return the local status."""
        executed_tools.append("get_status")
        return "ready"

    failed = prefix + _event("error", error={"type": "server_error", "message": "Temporarily overloaded"})
    completed = (
        _created("resp_answer") + _text() + _event("response.completed", response=_response("resp_answer", "completed"))
    )
    async with _model(_tool_stream(), failed, completed) as model:
        model.retries = 1
        model.delay_between_retries = 0
        agent = Agent(model=model, tools=[get_status])
        events = [event async for event in agent.arun("Check status", stream=True, stream_events=True)]

    assert executed_tools == ["get_status"]
    assert [event.content for event in events if isinstance(event, RunCompletedEvent)] == (
        ["Ready"] if recovered else []
    )
    assert any(isinstance(event, RunErrorEvent) for event in events) is not recovered


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize("prefix", [_created(), _created() + _text()], ids=["lifecycle-only", "partial-text"])
async def test_partial_stream_diagnostic_preserves_cause_type(prefix: str, *, sync: bool) -> None:
    """A wrapped transport failure must keep its type without copying provider payloads."""
    failed = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_InterruptedStream(prefix, httpx.ReadTimeout("")),
    )
    async with _model(failed) as model:
        with pytest.raises(ModelProviderError, match="ReadTimeout"):
            _ = [chunk async for chunk in _invoke(model, sync=sync)]


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("content", "Partial"), ("reasoning_content", "Thinking"), ("tool_calls", [{"id": "call_partial"}])],
)
async def test_parsed_lifecycle_output_prevents_retry(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    *,
    sync: bool,
) -> None:
    """Lifecycle labels must not override output exposed by an upstream parser."""
    original = OpenAIResponses._parse_provider_response_delta

    def parse_with_output(
        self: OpenAIResponses,
        stream_event: ResponseStreamEvent,
        assistant_message: Message,
        tool_use: dict[str, Any],
    ) -> tuple[ModelResponse, dict[str, Any]]:
        parsed, tool_use = original(self, stream_event, assistant_message, tool_use)
        setattr(parsed, field, value)
        return parsed, tool_use

    monkeypatch.setattr(OpenAIResponses, "_parse_provider_response_delta", parse_with_output)
    failed = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_InterruptedStream(_created()))
    async with _model(failed) as model:
        with pytest.raises(IncompleteResponsesStreamError):
            _ = [chunk async for chunk in _invoke(model, sync=sync)]
