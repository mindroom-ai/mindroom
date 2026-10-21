"""Ordinary responses retain provider, tool, delivery, and history behavior."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.agent import Agent
from openai import AsyncOpenAI

from mindroom.providers.openai_models import MindRoomOpenAIChat
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import TurnRecord, canonicalize_turn_record
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
    from pathlib import Path

    from agno.db.base import BaseDb


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
    await bot._reply_runtime.start()
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
            **_kwargs: object,
        ) -> Agent:
            return Agent(
                model=model,
                name=bot.agent_name,
                tools=[multiply],
                db=history_storage,
                telemetry=False,
                retries=0,
            )

        monkeypatch.setattr("mindroom.ai.create_agent", create_agent)
        task = asyncio.create_task(bot._response_runner.generate_response(request))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                stop = None
                if scenario == "cancel":
                    # A Stop reaction on the visible reply: the turn and the reply record it together.
                    turn = bot._turn_store.attach_response_context(
                        TurnRecord.create(["$event"], requester_id="@user:localhost"),
                        history_scope=bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
                        conversation_target=envelope.target,
                    )
                    await bot._turn_store.record_pending_turn(
                        canonicalize_turn_record(turn, response_event_id="$response"),
                    )
                    stop = asyncio.create_task(
                        bot._user_stop_reconciler.finalize(
                            "$response",
                            1,
                            room_id=envelope.target.room_id,
                        ),
                    )
                assert await task == "$response"
                if stop is not None:
                    assert await stop
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
    session = storage.load_session(envelope.target.session_id, "agent")
    assert session is not None
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
