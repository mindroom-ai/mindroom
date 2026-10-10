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
from mindroom.anthropic_claude import MindRoomAnthropicClaude

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


def _agent(storage: object, response: httpx.Response) -> Agent:
    client = AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response)),
    )
    model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", async_client=client, max_tokens=1024)
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
        agent = _agent(storage, httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=held))
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
async def test_completed_claude_stream_counts_its_usage_once(tmp_path: Path) -> None:
    """Usage known at the start of the stream is not counted again when the final usage arrives."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    events = _start_and_text("Ready") + _event("content_block_stop", index=0)
    events += _event("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None}, usage=_FINAL_USAGE)
    events += _event("message_stop")
    try:
        agent = _agent(storage, httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events))
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
