"""Ordinary responses retain provider, tool, delivery, and history behavior."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from openai import AsyncOpenAI

from mindroom import agno_session_persistence_patch as persistence
from mindroom.agent_storage import get_agent_session
from mindroom.cancellation import request_task_cancel
from mindroom.openai_models import MindRoomOpenAIChat
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from tests.bot_helpers import (
    AgentBotTestBase,
    _make_matrix_client_mock,
    _room_send_response,
    make_mock_agent_user,
    make_test_agent_bot,
)
from tests.conftest import request_envelope, runtime_paths_for
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.db.base import BaseDb


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_cancelled_response_retains_canonical_history_after_detached_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """A delayed real Agno save cannot overwrite the response runner's canonical replay."""
    release = asyncio.Event()
    waiting: set[asyncio.Task[object]] = set()
    captured: list[tuple[asyncio.Task[object], SqliteDb, str]] = []
    original_save = persistence._offload_sync_save
    original_wait = persistence.wait_for_future_until_complete

    async def delayed_save(
        lane: persistence._PersistenceLane,
        save: Callable[..., object],
        owner: object,
        payload: object,
        *args: object,
    ) -> None:
        if isinstance(payload, RunOutput) and payload.status == RunStatus.cancelled:
            task = asyncio.current_task()
            assert task is not None
            assert isinstance(owner, Agent)
            assert isinstance(owner.db, SqliteDb)
            assert payload.run_id is not None
            captured.append((task, owner.db, payload.run_id))
            if waiting - {task}:
                release.set()
            await release.wait()
        await original_save(lane, save, owner, payload, *args)

    async def controlled_wait(
        future: asyncio.Future[object],
        *,
        on_cancel: Callable[[], None] | None = None,
        chain_cancelled_result: bool = True,
    ) -> object:
        task = asyncio.current_task()
        assert task is not None
        waiting.add(task)
        try:
            # Release when the response owns the save, or after it has already
            # finalized below. Both orderings check the same durable outcome.
            if captured and task is not captured[0][0]:
                release.set()
            return await original_wait(future, on_cancel=on_cancel, chain_cancelled_result=chain_cancelled_result)
        finally:
            waiting.discard(task)

    monkeypatch.setattr(persistence, "_offload_sync_save", delayed_save)
    monkeypatch.setattr(persistence, "wait_for_future_until_complete", controlled_wait)
    try:
        await test_disabled_participation_preserves_ordinary_response(tmp_path, monkeypatch, streaming, "cancel")
    finally:
        release.set()
        async with asyncio.timeout(5):
            await asyncio.gather(*(task for task, _, _ in captured))
    assert len(captured) == 1
    _, original_storage, run_id = captured[0]
    storage = SqliteDb(db_file=original_storage.db_file, session_table=original_storage.session_table_name)
    try:
        run = storage.get_run(run_id)
        assert run is not None
        assert run.metadata is not None
        assert run.metadata.get("mindroom_original_status") == "cancelled"
        assert run.metadata.get("mindroom_replay_state") == "interrupted"
    finally:
        storage.close()


def _provider_response(payload: dict[str, Any], message: dict[str, Any]) -> httpx.Response:
    """Return a controlled response through the real SDK in either transport mode."""
    finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
    if payload.get("stream"):
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{"index": 0, **call} for call in delta["tool_calls"]]
        chunk = {
            "id": "completion",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )
    return httpx.Response(
        200,
        json={
            "id": "completion",
            "object": "chat.completion",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("scenario", ["reply", "tool", "error", "cancel"])
async def test_disabled_participation_preserves_ordinary_response(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    scenario: str,
) -> None:
    """No decision may add calls, disable tools, suppress errors, or discard history."""
    config = AgentBotTestBase._config_for_storage(tmp_path)
    config.memory.backend = "none"
    config.defaults.show_tool_calls = False
    bot = make_test_agent_bot(
        make_mock_agent_user(),
        tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=streaming,
    )
    install_direct_response_admission(bot)
    bot.client = _make_matrix_client_mock()
    bot.client.room_send.return_value = _room_send_response("$response")
    bot.client.get_presence.return_value.presence = "online"
    bot.client.get_presence.return_value.last_active_ago = 0
    requests: list[dict[str, Any]] = []
    executions: list[tuple[int, int]] = []
    entered = asyncio.Event()
    answer = "42" if scenario == "tool" else "Ordinary answer."

    def multiply(a: int, b: int) -> str:
        """Multiply two integers."""
        executions.append((a, b))
        return str(a * b)

    async def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        entered.set()
        if scenario == "cancel":
            await asyncio.Future()
        if scenario == "error":
            return httpx.Response(
                400,
                json={"error": {"message": "Controlled provider failure", "type": "invalid_request_error"}},
            )
        message: dict[str, Any] = {"role": "assistant", "content": answer}
        if scenario == "tool" and len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-multiply",
                        "type": "function",
                        "function": {"name": "multiply", "arguments": '{"a":6,"b":7}'},
                    },
                ],
            }
        return _provider_response(payload, message)

    envelope = request_envelope(
        room_id="!test:localhost",
        reply_to_event_id="$event",
        thread_id="$thread",
        prompt="Please help with this task.",
        user_id="@user:localhost",
        agent_name=bot.agent_name,
    )
    request = ResponseRequest(
        prompt="Please help with this task.",
        sources=ResponseSources(pending_event_ids=("$event",), logical_source_event_ids=("$event",)),
        thread_history=[],
        user_id="@user:localhost",
        response_envelope=envelope,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        model = MindRoomOpenAIChat(
            id="test",
            api_key="test-key",
            async_client=AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0),
        )

        def create_agent(
            *_args: object,
            history_storage: BaseDb | None,
            session_id: str | None,
            **_kwargs: object,
        ) -> Agent:
            return Agent(
                model=model,
                name=bot.agent_name,
                tools=[multiply],
                db=history_storage,
                session_id=session_id,
                telemetry=False,
                retries=0,
            )

        monkeypatch.setattr("mindroom.ai.create_agent", create_agent)
        task = asyncio.create_task(bot._response_runner.generate_response(request))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                if scenario == "cancel":
                    request_task_cancel(task, cancel_source="user_stop")
                assert await task == "$response"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert len(requests) == (2 if scenario == "tool" else 1)
    assert all(bool(payload.get("stream")) == streaming for payload in requests)
    assert all(payload.get("tool_choice") != "none" for payload in requests)
    assert all("Decide whether to participate" not in json.dumps(payload["messages"]) for payload in requests)
    assert executions == ([(6, 7)] if scenario == "tool" else [])
    if scenario == "tool":
        assert any(message["role"] == "tool" and message["content"] == "42" for message in requests[1]["messages"])
    bodies = [call.kwargs["content"].get("body", "") for call in bot.client.room_send.await_args_list]
    if scenario in {"reply", "tool"}:
        assert any(answer in body for body in bodies)
    elif scenario == "error":
        assert any("error" in body.lower() for body in bodies)
    else:
        assert any("cancelled" in body.lower() for body in bodies)

    storage = bot._conversation_state_writer.create_storage(None)
    try:
        session = get_agent_session(storage, envelope.target.session_id)
        assert session is not None
        assert session.runs is not None
        assert len(session.runs) == 1
        run = session.runs[0]
        if scenario in {"reply", "tool"}:
            assert run.content == answer
            if scenario == "tool":
                assert any(message.role == "tool" and message.content == "42" for message in run.messages or [])
        else:
            assert run.metadata is not None
            assert run.metadata["mindroom_original_status"] == ("error" if scenario == "error" else "cancelled")
            assert any("Please help with this task." in str(message.content) for message in run.messages or [])
    finally:
        storage.close()
