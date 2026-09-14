"""Native Responses checkpoints survive the actual SDK, Agent, and SQLite boundaries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from openai import AsyncOpenAI, OpenAI
from openai.types.responses import Response

from mindroom.codex_model import CodexResponses
from mindroom.openai_models import MindRoomOpenAIResponses

if TYPE_CHECKING:
    from pathlib import Path

_CHECKPOINT = {"type": "compaction", "id": "cmp_latest", "encrypted_content": "opaque-checkpoint"}
_ANSWER = {
    "type": "message",
    "id": "msg_answer",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Ready", "annotations": []}],
}


def _response(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_done",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-astra",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 6000,
            "output_tokens": 10,
            "total_tokens": 6010,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "error": None,
        "incomplete_details": None,
    }


def _event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': 0, **fields})}\n\n"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("codex", [False, True])
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.asyncio
async def test_checkpoint_replay_after_sqlite_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
    codex: bool,
    sync: bool,
) -> None:
    """Dropping an item, replaying the old prefix, or deleting canonical runs breaks this."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        output = [_CHECKPOINT, _ANSWER] if len(requests) == 1 else [_ANSWER]
        if payload.get("stream"):
            events = _event("response.created", response={**_response([]), "status": "in_progress"})
            for index, item in enumerate(output):
                events += _event("response.output_item.done", output_index=index, item=item)
            events += _event(
                "response.output_text.delta",
                item_id="msg_answer",
                output_index=1,
                content_index=0,
                delta="Ready",
            )
            # Codex can omit every item from the terminal event.
            events += _event("response.completed", response=_response([]))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events)
        return httpx.Response(200, json=_response(output))

    transport = httpx.MockTransport(respond)
    with OpenAI(api_key="test", http_client=httpx.Client(transport=transport)) as client:
        async with AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=transport)) as async_client:
            if codex:
                monkeypatch.setattr(CodexResponses, "get_client", lambda _self: client)
                monkeypatch.setattr(CodexResponses, "get_async_client", lambda _self: async_client)
            model_type = CodexResponses if codex else MindRoomOpenAIResponses
            for prompt in ("Remember the launch port is 4321.", "What port?"):
                model = model_type(id="gpt-6-astra", store=False, client=client, async_client=async_client)
                model.configure_native_compaction(threshold=1024)
                db = SqliteDb(db_file=str(tmp_path / "history.db"))
                agent = Agent(
                    id="agent",
                    model=model,
                    db=db,
                    session_id="thread",
                    instructions="Stable rules.",
                    add_history_to_context=True,
                    num_history_runs=20,
                    store_history_messages=False,
                    telemetry=False,
                )
                if stream and sync:
                    list(agent.run(prompt, stream=True))
                elif stream:
                    async for _ in agent.arun(prompt, stream=True):
                        pass
                elif sync:
                    agent.run(prompt)
                else:
                    await agent.arun(prompt)
                db.close()

    assert requests[0]["context_management"] == [{"type": "compaction", "compact_threshold": 1024}]
    assert requests[1]["store"] is False
    assert "previous_response_id" not in requests[1]
    replay = requests[1]["input"]
    assert replay[0]["role"] == "developer"
    assert replay[1:3] == [_CHECKPOINT, _ANSWER]
    assert replay[-1] == {"role": "user", "content": "What port?"}
    assert "Remember the launch port" not in json.dumps(replay)

    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    session = db.get_session("thread", session_type=SessionType.AGENT)
    assert session is not None
    assert len(session.runs or []) == 2
    assert any(
        message.content == "Remember the launch port is 4321."
        for run in session.runs or []
        for message in run.messages or []
    )
    db.close()


def test_latest_checkpoint_replaces_only_its_native_prefix() -> None:
    """A checkpoint after visible output must not duplicate the summarized output."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    parsed = model._parse_provider_response(
        Response.model_validate(_response([_CHECKPOINT, _ANSWER, {**_CHECKPOINT, "id": "cmp_final"}])),
    )
    messages = [
        Message(role="system", content="Current instructions."),
        Message(role="user", content="Old context."),
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
        Message(role="user", content="Continue."),
    ]
    replay = model._format_messages(messages)
    assert replay == [
        {"role": "developer", "content": "Current instructions."},
        {**_CHECKPOINT, "id": "cmp_final"},
        {"role": "user", "content": "Continue."},
    ]
    assert messages[1].content == "Old context."
    assert messages[2].content == "Ready"


def test_tool_result_follows_native_call_without_duplication() -> None:
    """Native function calls must keep the exact call ID matched by their result."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    call = {
        "type": "function_call",
        "id": "fc_lookup",
        "call_id": "call_lookup",
        "name": "lookup",
        "arguments": "{}",
        "status": "completed",
    }
    parsed = model._parse_provider_response(Response.model_validate(_response([_CHECKPOINT, call])))
    messages = [
        Message(role="system", content="Rules."),
        Message(role="user", content="Old request."),
        Message(
            role="assistant",
            content=parsed.content,
            tool_calls=parsed.tool_calls,
            provider_data=parsed.provider_data,
        ),
        Message(role="tool", tool_call_id="fc_lookup", content="Found it"),
    ]
    replay = model._format_messages(messages)
    assert replay[1:] == [
        _CHECKPOINT,
        call,
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
    ]


@pytest.mark.parametrize("change", ["model", "endpoint", "summary", "disabled"])
def test_incompatible_checkpoint_uses_canonical_messages(change: str) -> None:
    """Changing a replay identity must not hide the original conversation."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    parsed = model._parse_provider_response(Response.model_validate(_response([_CHECKPOINT, _ANSWER])))
    messages = [
        Message(role="user", content="Original facts."),
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
    ]
    if change == "model":
        model.id = "gpt-6-astra-mini"
    elif change == "endpoint":
        model.base_url = "https://example.test/v1"
    model.configure_native_compaction(
        threshold=None if change == "disabled" else 1024,
        history_generation="new summary" if change == "summary" else "",
    )
    replay = model._format_messages(messages)
    assert replay[0] == {"role": "user", "content": "Original facts."}
    assert all(item.get("type") != "compaction" for item in replay)


@pytest.mark.parametrize("fresh_model", [False, True])
@pytest.mark.parametrize("compacted", [False, True])
def test_portable_replay_never_chains_to_unstored_native_response(*, fresh_model: bool, compacted: bool) -> None:
    """Disabling native compaction must replay canonical input without an unavailable response ID."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra")
    model.configure_native_compaction(threshold=1024)
    reasoning = {"type": "reasoning", "id": "rs_native", "summary": [], "encrypted_content": "opaque-reasoning"}
    output = [reasoning, _ANSWER]
    if compacted:
        output.insert(0, _CHECKPOINT)
    native = model._parse_provider_response(Response.model_validate(_response(output)))
    messages = [
        Message(role="assistant", content="Old stored answer", provider_data={"response_id": "resp_old_stored"}),
        Message(role="user", content="Original facts."),
        Message(role="assistant", content=native.content, provider_data=native.provider_data),
        Message(role="user", content="Continue."),
    ]
    if fresh_model:
        model = MindRoomOpenAIResponses(id="gpt-6-astra")
    else:
        model.configure_native_compaction(threshold=None)
    assert model.store is None
    assert "previous_response_id" not in model.get_request_params(messages=messages)
    replay = [
        item if isinstance(item, dict) else item.model_dump(exclude_none=True)
        for item in model._format_messages(messages)
    ]
    assert any(item.get("content") == "Original facts." for item in replay)
    assert all(item.get("type") != "compaction" for item in replay)
    assert messages[2].provider_data["response_id"] == "resp_done"
    assert reasoning in replay
    # A subsequent stored response establishes a valid server continuation.
    stored = model._parse_provider_response(Response.model_validate({**_response([_ANSWER]), "id": "resp_stored"}))
    messages += [
        Message(role="assistant", content=stored.content, provider_data=stored.provider_data),
        Message(role="user", content="Next."),
    ]
    assert model.get_request_params(messages=messages)["previous_response_id"] == "resp_stored"
