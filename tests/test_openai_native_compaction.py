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
_REASONING = {"type": "reasoning", "id": "rs_first", "summary": [], "encrypted_content": "opaque-reasoning"}
_CALL = {
    "type": "function_call",
    "id": "fc_lookup",
    "call_id": "call_lookup",
    "name": "lookup",
    "arguments": "{}",
    "status": "completed",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disable_native", [False, True])
@pytest.mark.parametrize("portable", [False, True])
async def test_reasoning_survives_native_tool_loop(*, stream: bool, disable_native: bool, portable: bool) -> None:  # noqa: C901
    """Every reasoning item must precede its call, even with an empty terminal output."""
    requests: list[dict[str, Any]] = []
    executions: list[str] = []
    second_reasoning = {**_REASONING, "id": "rs_second"}
    second_call = {**_CALL, "id": "fc_second", "call_id": "call_second"}
    search = {
        "type": "tool_search_call",
        "id": "ts_search",
        "status": "completed",
        "execution": "server",
        "arguments": {"query": "lookup"},
    }
    output = [_REASONING, search, _CALL, second_reasoning, second_call]

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        items = output if len(requests) == 1 else [_ANSWER]
        if not payload.get("stream"):
            return httpx.Response(200, json=_response(items))
        events = _event("response.created", response={**_response([]), "status": "in_progress"})
        for index, item in enumerate(items):
            if item["type"] == "function_call":
                events += _event(
                    "response.output_item.added",
                    output_index=index,
                    item={**item, "arguments": "", "status": "in_progress"},
                )
                events += _event(
                    "response.function_call_arguments.delta",
                    output_index=index,
                    item_id=item["id"],
                    delta="{}",
                )
            events += _event("response.output_item.done", output_index=index, item=item)
        if len(requests) > 1:
            events += _event(
                "response.output_text.delta",
                output_index=0,
                item_id="msg_answer",
                content_index=0,
                delta="Ready",
            )
        events += _event("response.completed", response=_response([]))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events)

    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client, store=True if portable else None)
        if portable:
            model.configure_portable_replay()
        model.configure_native_compaction(threshold=1024)

        def lookup() -> str:
            """Look up the current status."""
            executions.append("lookup")
            if disable_native:
                model.configure_native_compaction(threshold=None)
            return "Found it"

        agent = Agent(model=model, tools=[lookup], telemetry=False)
        if stream:
            async for _ in agent.arun("Look up the status.", stream=True):
                pass
        else:
            await agent.arun("Look up the status.")

    replay = requests[1]["input"]
    assert replay[1:6] == output
    assert replay[6:] == [
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
        {"type": "function_call_output", "call_id": "call_second", "output": "Found it"},
    ]
    assert "previous_response_id" not in requests[1]
    assert executions == ["lookup", "lookup"]
    if portable:
        assert all(request["store"] is True for request in requests)
        assert all("reasoning.encrypted_content" in request["include"] for request in requests)
        assert all("context_management" not in request for request in requests)


@pytest.mark.parametrize("retain_call", [False, True])
def test_ordered_replay_respects_canonical_tool_filtering(*, retain_call: bool) -> None:
    """Native fallback must not resurrect calls removed by the configured history policy."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    removed = {**_CALL, "id": "fc_removed", "call_id": "call_removed"}
    parsed = model._parse_provider_response(Response.model_validate(_response([_REASONING, removed, _CALL])))
    messages = [
        Message(role="user", content="Original request."),
        Message(
            role="assistant",
            tool_calls=parsed.tool_calls[1:] if retain_call else [],
            provider_data=parsed.provider_data,
        ),
    ]
    if retain_call:
        messages.append(Message(role="tool", tool_call_id="fc_lookup", content="Found it"))
    # A new model has no in-memory compaction state.
    replay = MindRoomOpenAIResponses(id="gpt-6-astra")._format_messages(messages)
    expected = (
        [
            _REASONING,
            _CALL,
            {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
        ]
        if retain_call
        else [_REASONING]
    )
    assert replay[1:] == expected


def test_reasoning_tail_after_checkpoint_replays_once() -> None:
    """A later ordinary response must retain reasoning without duplicating the checkpoint tail."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    checkpoint = model._parse_provider_response(Response.model_validate(_response([_CHECKPOINT, _ANSWER])))
    ordinary = model._parse_provider_response(Response.model_validate(_response([_REASONING, _ANSWER])))
    messages = [
        Message(role="user", content="Original request."),
        Message(role="assistant", content=checkpoint.content, provider_data=checkpoint.provider_data),
        Message(role="user", content="Continue."),
        Message(role="assistant", content=ordinary.content, provider_data=ordinary.provider_data),
    ]
    assert model._format_messages(messages) == [
        _CHECKPOINT,
        _ANSWER,
        {"role": "user", "content": "Continue."},
        _REASONING,
        _ANSWER,
    ]


def test_ordered_output_does_not_restore_rewritten_text() -> None:
    """Request-local canonical text edits take precedence over captured provider output."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    parsed = model._parse_provider_response(Response.model_validate(_response([_REASONING, _ANSWER])))
    replay = model._format_messages(
        [
            Message(role="assistant", content="Rewritten answer.", provider_data=parsed.provider_data),
        ],
    )
    assert replay[0] == {"role": "assistant", "content": "Rewritten answer."}
    assert "Ready" not in json.dumps([item if isinstance(item, dict) else item.model_dump() for item in replay])


@pytest.mark.parametrize("key", ["context_management", "store", "previous_response_id", "background"])
def test_nested_openai_body_policy_disables_native_compaction(key: str) -> None:
    """A raw SDK override must not contradict automatic compaction's replay policy."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", request_params={"extra_body": {key: None}})
    model.configure_native_compaction(threshold=1024)
    assert model.native_compaction is None
