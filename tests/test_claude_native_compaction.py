"""Claude native compaction across direct and Vertex SDK request paths."""
# ruff: noqa: S106

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.exceptions import ModelProviderError
from agno.metrics import ModelMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunCompletedEvent as TeamRunCompletedEvent
from agno.run.team import TeamRunOutput
from anthropic import Anthropic, AnthropicVertex, AsyncAnthropic, AsyncAnthropicVertex
from anthropic.types.beta import BetaMessage
from google.oauth2.credentials import Credentials
from openai import AsyncOpenAI, OpenAI

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.claude_prompt_cache import install_claude_prompt_cache_hook
from mindroom.config.models import ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.history.types import PreparedHistoryState
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.teams import (
    _build_streamed_team_run_metadata_content,
    _build_team_run_metadata_content,
    _PreparedMaterializedTeamExecution,
    _TeamStreamUsage,
)
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.history_helpers import _make_config

if TYPE_CHECKING:
    from pathlib import Path

_CHECKPOINT = {"type": "compaction", "content": "Launch port 4321.", "encrypted_content": "opaque"}
_TEXT = {"type": "text", "text": "Ready"}
_USAGE = {
    "input_tokens": 2000,
    "output_tokens": 100,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 20,
    "iterations": [
        {
            "type": "compaction",
            "input_tokens": 60000,
            "output_tokens": 1000,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 100,
        },
        {
            "type": "message",
            "model": "claude-sonnet-5",
            "input_tokens": 2000,
            "output_tokens": 100,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 20,
        },
    ],
}


def _response(blocks: list[dict[str, Any]], *, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_done",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _USAGE,
    }


def _event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("vertex", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_claude_replay_after_restart(tmp_path: Path, *, vertex: bool, stream: bool) -> None:
    """A lost beta edit, checkpoint, or replay boundary fails the actual outgoing request."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        blocks = [_CHECKPOINT, _TEXT] if len(requests) == 1 else [_TEXT]
        if not payload.get("stream"):
            return httpx.Response(200, json=_response(blocks))
        events = _event("message_start", message={**_response([]), "stop_reason": None})
        for index, block in enumerate(blocks):
            empty = {**block, ("content" if block["type"] == "compaction" else "text"): ""}
            events += _event("content_block_start", index=index, content_block=empty)
            delta = (
                {"type": "compaction_delta", "content": block["content"]}
                if block["type"] == "compaction"
                else {"type": "text_delta", "text": block["text"]}
            )
            events += _event("content_block_delta", index=index, delta=delta)
            events += _event("content_block_stop", index=index)
        events += _event("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None}, usage=_USAGE)
        events += _event("message_stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = (
            AsyncAnthropicVertex(
                project_id="test-project",
                region="global",
                credentials=Credentials(token="test-token"),
                http_client=http_client,
            )
            if vertex
            else AsyncAnthropic(api_key="test-key", http_client=http_client)
        )
        model_type = MindroomVertexAIClaude if vertex else MindRoomAnthropicClaude
        for prompt in ("Remember the launch port is 4321.", "What port?"):
            model = model_type(id="claude-sonnet-5", async_client=client, max_tokens=4096)
            model.configure_native_compaction(threshold=60000)
            install_claude_prompt_cache_hook(model)
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
            if stream:
                async for _ in agent.arun(prompt, stream=True):
                    pass
            else:
                await agent.arun(prompt)
            db.close()

    edit = requests[0]["context_management"]["edits"][0]
    assert edit["type"] == "compact_20260112"
    assert edit["trigger"] == {"type": "input_tokens", "value": 60000}
    assert edit.get("pause_after_compaction", False) is False
    replay = requests[1]["messages"]
    assert replay[0]["role"] == "assistant"
    assert replay[0]["content"][0]["type"] == "compaction"
    assert replay[0]["content"][0]["content"] == "Launch port 4321."
    assert "Remember the launch port" not in json.dumps(replay)
    assert "What port?" in json.dumps(replay)


def test_claude_billing_includes_compaction_iterations() -> None:
    """Compaction costs must not disappear from request and run metrics."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5")
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    assert parsed.response_usage is not None
    assert parsed.response_usage.input_tokens == 62000
    assert parsed.response_usage.output_tokens == 1100
    assert parsed.response_usage.cache_read_tokens == 530
    assert parsed.response_usage.cache_write_tokens == 120
    assert parsed.response_usage.total_tokens == 63100
    assert parsed.response_usage.provider_metrics["context_usage"] == {
        "input_tokens": 2000,
        "cache_read_tokens": 30,
        "cache_write_tokens": 20,
    }


@pytest.mark.parametrize("content", [None, ""])
def test_null_compaction_keeps_canonical_history(content: str | None) -> None:
    """An unsuccessful summary cannot replace the original conversation."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5")
    model.configure_native_compaction(threshold=60000)
    parsed = model._parse_provider_response(
        BetaMessage.model_validate(_response([{**_CHECKPOINT, "content": content}, _TEXT])),
    )
    messages = [
        Message(role="user", content="Original facts."),
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
    ]
    assert model.native_replay_messages(messages)[0].content == "Original facts."


def test_switching_claude_model_restores_canonical_history() -> None:
    """Provider-specific checkpoints must not survive an incompatible model selection."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5")
    model.configure_native_compaction(threshold=60000)
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    messages = [
        Message(role="user", content="Original facts."),
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
    ]
    model.id = "claude-opus-5"
    model.configure_native_compaction(threshold=60000)
    replay = model.native_replay_messages(messages)
    assert replay[0].content == "Original facts."
    assert all(block["type"] != "compaction" for block in replay[1].provider_data["content_blocks"])
    assert messages[1].provider_data["content_blocks"][0]["type"] == "compaction"


@pytest.mark.asyncio
@pytest.mark.parametrize("vertex", [False, True])
@pytest.mark.parametrize("native", [False, True])
async def test_canonical_fallback_removes_thinking_bound_to_checkpoint(*, vertex: bool, native: bool) -> None:
    """Discarding a checkpoint must not send its signed thinking against the restored prefix."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_response([_TEXT]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = (
            AsyncAnthropicVertex(
                project_id="test-project",
                region="global",
                credentials=Credentials(token="test-token"),
                http_client=http_client,
            )
            if vertex
            else AsyncAnthropic(api_key="test-key", http_client=http_client)
        )
        model_type = MindroomVertexAIClaude if vertex else MindRoomAnthropicClaude
        model = model_type(id="claude-fable-5-1", async_client=client)
        model.configure_native_compaction(threshold=60000)
        thinking = {"type": "thinking", "thinking": "Internal reasoning.", "signature": "checkpoint-bound"}
        redacted = {"type": "redacted_thinking", "data": "opaque-thinking"}
        anchor = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, thinking, _TEXT])))
        later = model._parse_provider_response(BetaMessage.model_validate(_response([redacted, _TEXT])))
        messages = [
            Message(role="user", content="Original facts."),
            Message(
                role="assistant",
                content=anchor.content,
                provider_data=anchor.provider_data,
                reasoning_content=anchor.reasoning_content,
            ),
            Message(role="user", content="Continue."),
            Message(
                role="assistant",
                content=later.content,
                provider_data=later.provider_data,
                redacted_reasoning_content=later.redacted_reasoning_content,
            ),
            Message(role="user", content="Continue again."),
            Message(
                role="assistant",
                content="Later reply.",
                reasoning_content="Later reasoning.",
                redacted_reasoning_content="later-opaque",
                provider_data={"signature": "later-bound"},
            ),
            Message(role="user", content="Finish."),
        ]
        original = [message.model_dump() for message in messages]
        if not native:
            model.configure_native_compaction(threshold=None)
        await model.ainvoke(messages, Message(role="assistant"))
        assert [message.model_dump() for message in messages] == original

    blocks = [block for message in requests[0]["messages"] for block in message["content"]]
    if native:
        assert _CHECKPOINT in blocks
        assert thinking in blocks
        assert redacted in blocks
    else:
        assert all(block["type"] not in {"compaction", "thinking", "redacted_thinking"} for block in blocks)
        assert {"type": "text", "text": "Original facts."} in blocks
        assert _TEXT in blocks
        assert {"type": "text", "text": "Later reply."} in blocks


def test_pause_after_compaction_is_rejected() -> None:
    """The automatic path must not silently discard a checkpoint-only paused run."""
    model = MindRoomAnthropicClaude(
        id="claude-sonnet-5",
        context_management={"edits": [{"type": "compact_20260112", "pause_after_compaction": True}]},
    )
    with pytest.raises(ValueError, match="pause_after_compaction"):
        model.get_request_params()


@pytest.mark.asyncio
@pytest.mark.parametrize("vertex", [False, True])
@pytest.mark.parametrize("pause", [False, True])
async def test_raw_context_management_owns_effective_claude_policy(*, vertex: bool, pause: bool) -> None:
    """SDK body overrides must disable automatic edits and cannot bypass pause validation."""
    requests: list[dict[str, Any]] = []
    policy = {"edits": [{"type": "compact_20260112", "pause_after_compaction": pause}]}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_response([_TEXT]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = (
            AsyncAnthropicVertex(
                project_id="test-project",
                region="global",
                credentials=Credentials(token="test-token"),
                http_client=http_client,
            )
            if vertex
            else AsyncAnthropic(api_key="test-key", http_client=http_client)
        )
        model_type = MindroomVertexAIClaude if vertex else MindRoomAnthropicClaude
        model = model_type(
            id="claude-sonnet-5",
            async_client=client,
            betas=["compact-2026-01-12"],
            request_params={"extra_body": {"context_management": policy}},
        )
        model.configure_native_compaction(threshold=60000)
        if pause:
            with pytest.raises(ModelProviderError, match="pause_after_compaction"):
                await model.ainvoke([Message(role="user", content="Continue.")], Message(role="assistant"))
            assert requests == []
        else:
            await model.ainvoke([Message(role="user", content="Continue.")], Message(role="assistant"))
            assert requests[0]["context_management"] == policy
        assert model.native_compaction is None


@pytest.mark.asyncio
async def test_vertex_guard_counts_checkpoint_replay_with_beta() -> None:
    """Vertex must not trim a checkpoint because it counted the replaced transcript."""
    requests: list[dict[str, Any]] = []
    headers: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        headers.append(request.headers.get("anthropic-beta", ""))
        return httpx.Response(200, json={"input_tokens": 100})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncAnthropicVertex(
            project_id="test-project",
            region="global",
            credentials=Credentials(token="test-token"),
            http_client=http_client,
        )
        model = MindroomVertexAIClaude(
            id="claude-sonnet-5",
            async_client=client,
            context_window=200000,
            max_tokens=4096,
        )
        model.configure_native_compaction(threshold=60000)
        parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
        messages = [
            Message(role="system", content="Stable rules."),
            Message(role="user", content="Old facts " * 150000, from_history=True),
            Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data, from_history=True),
            Message(role="user", content="Continue."),
        ]
        fitted = await model._fit_request_messages(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )
        assert all(message.content != messages[1].content for message in fitted)
        count = await model._count_request_input_tokens(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )
        assert count == 100

    assert requests[-1]["messages"][0]["content"][0]["type"] == "compaction"
    assert requests[-1]["context_management"]["edits"][0]["type"] == "compact_20260112"
    assert "compact-2026-01-12" in headers[-1] or "compact-2026-01-12" in requests[-1].get("anthropic_beta", [])


@pytest.mark.parametrize(
    ("configured_provider", "reported_provider"),
    [("anthropic", "Anthropic"), ("vertexai_claude", "VertexAI")],
)
@pytest.mark.parametrize("reported", [False, True])
def test_native_usage_metadata_separates_billing_from_context(
    tmp_path: Path,
    configured_provider: str,
    reported_provider: str,
    *,
    reported: bool,
) -> None:
    """Billed summary iterations must not inflate displayed context occupancy."""
    config, _ = _make_config(
        tmp_path,
        models={
            "default": ModelConfig(provider=configured_provider, id="claude-sonnet-5", context_window=200000),
        },
    )
    model = MindRoomAnthropicClaude(id="claude-sonnet-5")
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    metrics = RunMetrics(
        input_tokens=62000,
        output_tokens=1100,
        total_tokens=63100,
        cache_read_tokens=530,
        cache_write_tokens=120,
        details={
            "model": [
                ModelMetrics(
                    id="claude-sonnet-5",
                    provider=reported_provider,
                    provider_metrics=parsed.response_usage.provider_metrics,
                ),
            ],
        },
    )
    metadata = build_ai_run_metadata_content(
        config=config,
        model_name="default",
        run_id="run",
        session_id="session",
        status="COMPLETED",
        model="claude-sonnet-5",
        model_provider=reported_provider if reported else None,
        metrics=metrics,
        context_metrics=metrics,
        context_raw_input_tokens=62000,
        context_cache_read_tokens=530,
        context_cache_write_tokens=120,
    )[AI_RUN_METADATA_KEY]
    assert metadata["usage"]["input_tokens"] == 62000
    assert metadata["context"]["input_tokens"] == 2050


@pytest.mark.asyncio
async def test_oversized_vertex_checkpoint_restores_canonical_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact guard must restore canonical input before trimming native replay."""
    model = MindroomVertexAIClaude(id="claude-sonnet-5", context_window=200000, max_tokens=4096)
    model.configure_native_compaction(threshold=60000)
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    messages = [
        Message(role="user", content="Original facts.", from_history=True),
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data, from_history=True),
        Message(role="user", content="Continue."),
    ]
    monkeypatch.setattr(model, "_estimate_request_input_tokens", lambda *_args, **_kwargs: None)
    counter = AsyncMock(side_effect=[201000, 100])
    monkeypatch.setattr(model, "_count_request_input_tokens", counter)
    fitted = await model._fit_request_messages(
        messages,
        tools=None,
        response_format=None,
        compress_tool_results=False,
    )
    assert model.native_compaction is None
    assert [message.content for message in fitted] == [message.content for message in messages]
    assert fitted[1].provider_data["content_blocks"] == [_TEXT]
    assert messages[1].provider_data["content_blocks"][0] == _CHECKPOINT
    assert counter.await_count == 2


def test_plain_claude_request_refreshes_active_context_metrics() -> None:
    """A later tool-loop request must replace the earlier compacted context count."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5")
    compacted = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    plain_response = _response([_TEXT])
    plain_response["usage"] = {key: value for key, value in _USAGE.items() if key != "iterations"}
    plain_response["usage"]["input_tokens"] = 3000
    plain = model._parse_provider_response(BetaMessage.model_validate(plain_response))
    metrics = ModelMetrics(provider_metrics=compacted.response_usage.provider_metrics)
    metrics.accumulate(ModelMetrics(provider_metrics=plain.response_usage.provider_metrics))
    assert metrics.provider_metrics["context_usage"]["input_tokens"] == 3000


@pytest.mark.parametrize("region", ["global", "us", "eu", "us-central1"])
def test_vertex_checkpoint_route_survives_lazy_client_creation(region: str) -> None:
    """An unchanged endpoint must keep its checkpoint across SDK initialization."""
    model = MindroomVertexAIClaude(
        id="claude-sonnet-5",
        project_id="test-project",
        region=region,
        client_params={"credentials": Credentials(token="test-token")},
    )
    model.configure_native_compaction(threshold=60000)
    route = model.native_compaction.route
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    messages = [
        Message(role="user", content="Original facts."),
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
    ]
    client = model.get_client()
    model.configure_native_compaction(threshold=60000)
    assert model.native_compaction.route == route
    assert len(model.native_replay_messages(messages)) == 1
    client.close()


@pytest.mark.parametrize("stream", [False, True])
def test_team_context_uses_leader_metrics_for_shared_model(tmp_path: Path, *, stream: bool) -> None:
    """Same-model member billing must not replace the leader's active context."""
    config, _ = _make_config(
        tmp_path,
        models={
            "default": ModelConfig(provider="anthropic", id="claude-sonnet-5", context_window=200000),
        },
    )
    leader = RunMetrics(
        input_tokens=90000,
        details={
            "model": [
                ModelMetrics(
                    id="claude-sonnet-5",
                    provider="Anthropic",
                    input_tokens=90000,
                    provider_metrics={"context_usage": {"input_tokens": 90000}},
                ),
            ],
        },
    )
    member = RunOutput(
        metrics=RunMetrics(
            input_tokens=1000,
            details={
                "model": [
                    ModelMetrics(
                        id="claude-sonnet-5",
                        provider="Anthropic",
                        input_tokens=1000,
                        provider_metrics={"context_usage": {"input_tokens": 1000}},
                    ),
                ],
            },
        ),
    )
    prepared = _PreparedMaterializedTeamExecution(
        messages=(),
        run_metadata=None,
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
        runtime_model_name="default",
    )
    if stream:
        metadata = _build_streamed_team_run_metadata_content(
            config=config,
            prepared_execution=prepared,
            completed_run_event=TeamRunCompletedEvent(metrics=leader, member_responses=[member]),
            usage=_TeamStreamUsage(latest_model_id="claude-sonnet-5", latest_model_provider="Anthropic"),
            run_id="run",
            session_id="session",
            status=RunStatus.completed,
            tool_count=0,
        )
    else:
        metadata = _build_team_run_metadata_content(
            config=config,
            prepared_execution=prepared,
            response=TeamRunOutput(
                model="claude-sonnet-5",
                model_provider="Anthropic",
                metrics=leader,
                member_responses=[member],
            ),
            session_id="session",
            tool_count=0,
        )
    assert metadata[AI_RUN_METADATA_KEY]["usage"]["input_tokens"] == 91000
    assert metadata[AI_RUN_METADATA_KEY]["context"]["input_tokens"] == 90000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "mismatch"),
    [
        ("openai", "endpoint"),
        ("anthropic", "endpoint"),
        ("vertex", "endpoint"),
        ("vertex", "project"),
        ("vertex", "region"),
    ],
)
async def test_native_compaction_rejects_conflicting_client_routes(provider: str, mismatch: str) -> None:
    """One model cannot label checkpoints from different SDK routes as interchangeable."""
    if provider == "openai":
        model = MindRoomOpenAIResponses(
            id="gpt-6-astra",
            store=False,
            async_client=AsyncOpenAI(api_key="test-key"),
            client=OpenAI(api_key="test-key", base_url="https://other.test/v1"),
        )
    elif provider == "anthropic":
        model = MindRoomAnthropicClaude(
            id="claude-sonnet-5",
            async_client=AsyncAnthropic(api_key="test-key"),
            client=Anthropic(api_key="test-key", base_url="https://other.test"),
        )
    else:
        credentials = Credentials(token="test-token")
        model = MindroomVertexAIClaude(
            id="claude-sonnet-5",
            async_client=AsyncAnthropicVertex(project_id="project-one", region="global", credentials=credentials),
            client=AnthropicVertex(
                project_id="project-two" if mismatch == "project" else "project-one",
                region="us" if mismatch == "region" else "global",
                base_url="https://other.test/v1" if mismatch == "endpoint" else None,
                credentials=credentials,
            ),
        )
    try:
        model.configure_native_compaction(threshold=60000)
        assert model.native_compaction is None
    finally:
        model.client.close()
        await model.async_client.close()
