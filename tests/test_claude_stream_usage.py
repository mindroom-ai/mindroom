"""Claude stream usage survives a stopped reply and counts once when the stream completes."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.run.agent import RunContentEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.cancel import acancel_run
from agno.session.agent import AgentSession
from anthropic import AsyncAnthropic

from mindroom.agent_storage import create_state_storage
from mindroom.agno_compat_session_persistence import drain_agent_cancellation
from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.claude_prompt_cache import install_claude_prompt_cache_hook
from mindroom.config.models import DebugConfig
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.provider_media_fallback import install_provider_media_fallback
from mindroom.provider_stream_retry import install_provider_stream_retry_hook
from mindroom.usage_storage import project_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# Anthropic reports input and cache usage when the stream starts, and final output usage at its end.
_START_USAGE = {
    "input_tokens": 1200,
    "cache_read_input_tokens": 48000,
    "cache_creation_input_tokens": 800,
    "output_tokens": 1,
}
_FINAL_USAGE = {**_START_USAGE, "output_tokens": 50}


def _event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"


def _start_and_text(*texts: str) -> str:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": _START_USAGE,
    }
    events = _event("message_start", message=message)
    events += _event("content_block_start", index=0, content_block={"type": "text", "text": ""})
    for text in texts:
        events += _event("content_block_delta", index=0, delta={"type": "text_delta", "text": text})
    return events


class _HeldStream(httpx.AsyncByteStream):
    """Start a reply, then keep the response open as a slow generation would."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _start_and_text("Checking", " status").encode()
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed.set()


def _agent(storage: object, response: httpx.Response, log_dir: Path) -> Agent:
    client = AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response)),
    )
    model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", async_client=client, max_tokens=1024)
    # The hooks model loading installs wrap the provider stream, as they do in production.
    install_llm_request_logging(model, agent_name="status", debug_config=DebugConfig(), default_log_dir=log_dir)
    install_claude_prompt_cache_hook(model)
    install_provider_stream_retry_hook(model, idle_timeout_seconds=30)
    install_provider_media_fallback(model, fallback_prompt="Media unavailable.")
    return Agent(id="status", model=model, db=storage, telemetry=False)


def _session_usage(storage: object) -> dict[str, int]:
    session = storage.get_session("session", session_type=SessionType.AGENT)  # type: ignore[attr-defined]
    assert isinstance(session, AgentSession)
    metrics = session.session_data["session_metrics"]
    return {
        name: metrics[name] for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    }


@pytest.mark.asyncio
async def test_stopped_claude_reply_keeps_the_usage_reported_at_stream_start(tmp_path: Path) -> None:
    """Anthropic bills the input and cache tokens it reported before the user stopped the reply."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    held = _HeldStream()
    run_output: RunOutput | None = None
    try:
        agent = _agent(
            storage,
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=held),
            tmp_path / "logs",
        )
        async with asyncio.timeout(5):
            async for event in agent.arun(
                "Check status",
                run_id="run",
                session_id="session",
                stream=True,
                yield_run_output=True,
            ):
                if isinstance(event, RunContentEvent) and event.content == "Checking":
                    # Agno checks this request when the next text chunk arrives.
                    assert await acancel_run("run")
                if isinstance(event, RunOutput):
                    run_output = event
            await held.closed.wait()

        assert run_output is not None
        assert run_output.status == RunStatus.cancelled
        assert run_output.metrics is not None
        assert (run_output.metrics.input_tokens, run_output.metrics.cache_read_tokens) == (1200, 48000)
        assert run_output.metrics.cache_write_tokens == 800
        assert _session_usage(storage) == {
            "input_tokens": 1200,
            "output_tokens": 1,
            "cache_read_tokens": 48000,
            "cache_write_tokens": 800,
        }
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_hard_stopped_claude_reply_keeps_the_usage_reported_at_stream_start(tmp_path: Path) -> None:
    """MindRoom's Stop cancels the reply task while it waits for Claude's next chunk."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    held = _HeldStream()
    started = asyncio.Event()
    try:
        agent = _agent(
            storage,
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=held),
            tmp_path / "logs",
        )
        async with drain_agent_cancellation(agent, "run") as bind:

            async def consume() -> None:
                with bind():
                    events = agent.arun("Check status", run_id="run", session_id="session", stream=True)
                while True:
                    with bind():
                        event = await anext(events)
                    if isinstance(event, RunContentEvent) and event.content:
                        started.set()

            task = asyncio.create_task(consume())
            async with asyncio.timeout(5):
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        await held.closed.wait()

        assert _session_usage(storage) == {
            "input_tokens": 1200,
            "output_tokens": 1,
            "cache_read_tokens": 48000,
            "cache_write_tokens": 800,
        }
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        requests = project_usage(session.runs[-1].to_dict())["requests"]
        assert [
            (request["metrics"]["input_tokens"], request["metrics"]["cache_read_tokens"]) for request in requests
        ] == [
            (1200, 48000),
        ]
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_claude_reply_closed_from_another_task_keeps_its_start_usage(tmp_path: Path) -> None:
    """The streaming path reads the reply in one task and can close the suspended stream from another."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    held = _HeldStream()
    try:
        agent = _agent(
            storage,
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=held),
            tmp_path / "logs",
        )
        async with drain_agent_cancellation(agent, "run") as bind:
            with bind():
                events = agent.arun("Check status", run_id="run", session_id="session", stream=True)

            async def read_until_text() -> None:
                while True:
                    with bind():
                        event = await anext(events)
                    if isinstance(event, RunContentEvent) and event.content:
                        return

            async with asyncio.timeout(5):
                await asyncio.create_task(read_until_text())
                with bind():
                    await events.aclose()
        await held.closed.wait()

        assert _session_usage(storage) == {
            "input_tokens": 1200,
            "output_tokens": 1,
            "cache_read_tokens": 48000,
            "cache_write_tokens": 800,
        }
    finally:
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delta_usage", [_FINAL_USAGE, {"output_tokens": 50}], ids=["cumulative", "output_only"])
async def test_completed_claude_stream_counts_its_usage_once(tmp_path: Path, delta_usage: dict[str, int]) -> None:
    """Usage known at the start of the stream is not counted again when the final usage arrives."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    events = _start_and_text("Ready") + _event("content_block_stop", index=0)
    events += _event("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None}, usage=delta_usage)
    events += _event("message_stop")
    try:
        agent = _agent(
            storage,
            httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events),
            tmp_path / "logs",
        )
        async for _ in agent.arun("Check status", session_id="session", stream=True):
            pass

        assert _session_usage(storage) == {
            "input_tokens": 1200,
            "output_tokens": 50,
            "cache_read_tokens": 48000,
            "cache_write_tokens": 800,
        }
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_claude_stream_still_retries_an_overload_after_it_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that has only started is retried after an overload, and the retry's usage counts once."""
    monkeypatch.setattr("mindroom.provider_stream_retry._retry_delay_seconds", lambda _attempt: 0)
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    overloaded = _start_and_text() + _event("error", error={"type": "overloaded_error", "message": "Overloaded"})
    completed = _start_and_text("Ready") + _event("content_block_stop", index=0)
    completed += _event("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None}, usage=_FINAL_USAGE)
    completed += _event("message_stop")
    responses = iter([overloaded, completed])
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=next(responses))

    client = AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", async_client=client, max_tokens=1024)
    install_provider_stream_retry_hook(model)
    try:
        agent = Agent(id="status", model=model, db=storage, telemetry=False)
        content = "".join(
            [
                event.content
                async for event in agent.arun("Check status", session_id="session", stream=True)
                if isinstance(event, RunContentEvent) and event.content
            ],
        )

        assert content == "Ready"
        assert len(requests) == 2
        assert _session_usage(storage) == {
            "input_tokens": 1200,
            "output_tokens": 50,
            "cache_read_tokens": 48000,
            "cache_write_tokens": 800,
        }
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_claude_stream_that_fails_after_starting_counts_nothing(tmp_path: Path) -> None:
    """A failed attempt is retried or reported as an error, so only a stopped stream keeps its start usage."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    overloaded = _start_and_text() + _event("error", error={"type": "overloaded_error", "message": "Overloaded"})
    client = AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=overloaded),
            ),
        ),
    )
    model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", async_client=client, max_tokens=1024)
    try:
        agent = Agent(id="status", model=model, db=storage, telemetry=False)
        async for _ in agent.arun("Check status", session_id="session", stream=True):
            pass

        assert model.take_unfinished_stream_usage() is None
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        assert not session.session_data["session_metrics"].get("input_tokens")
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_stalled_claude_stream_counts_nothing(tmp_path: Path) -> None:
    """A stream that starts and then goes silent fails as a stall and leaves no start usage behind."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    held = _HeldStream()
    client = AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=held),
            ),
        ),
    )
    model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", async_client=client, max_tokens=1024)
    # The stream already sent text, so the stall is not retried and ends the run as an error.
    install_provider_stream_retry_hook(model, idle_timeout_seconds=0.2)
    try:
        agent = Agent(id="status", model=model, db=storage, telemetry=False)
        async with asyncio.timeout(10):
            async for _ in agent.arun("Check status", session_id="session", stream=True):
                pass

        assert model.take_unfinished_stream_usage() is None
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        assert not session.session_data["session_metrics"].get("input_tokens")
    finally:
        storage.close()
