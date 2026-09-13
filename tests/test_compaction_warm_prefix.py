"""Compaction reuses reply prefixes without executing tools or losing history."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.learn import LearningMachine
from agno.media import Image
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.openai import OpenAIResponses
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from anthropic import AsyncAnthropic

from mindroom.agents import create_agent
from mindroom.claude_prompt_cache import install_claude_deferred_tool_search, install_claude_prompt_cache_hook
from mindroom.codex_model import CodexResponses
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.history.compaction import compact_scope_history
from mindroom.history.storage import read_scope_state, write_scope_state
from mindroom.history.summary_call import generate_compaction_summary
from mindroom.history.types import HistoryPolicy, HistoryScope, HistoryScopeState, ResolvedHistorySettings
from mindroom.history.warm_prefix import build_warm_prefix_request
from mindroom.prompts import COMPACTION_MODE_INSTRUCTION, COMPACTION_SUMMARY_PROMPT
from mindroom.system_prompt import render_session_context
from tests.conftest import prepare_history_for_run_for_test, seed_session, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

_SUMMARY = "\n".join(
    f"## {heading}\nProject Atlas."
    for heading in ("Goal", "Constraints", "Progress", "Decisions", "Next Steps", "Critical Context")
)


def _unmarked(value: object) -> object:
    """Remove movable cache annotations when comparing the underlying prefix."""
    if isinstance(value, dict):
        return {key: _unmarked(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_unmarked(item) for item in value]
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_compaction_reuses_reply_wire_prefix_and_preserves_live_state(*, deferred: bool) -> None:
    """Real Agno assembly must preserve tools, system, history, and model settings."""
    requests: list[dict] = []
    calls: list[str] = []

    def write_file(text: str) -> str:
        """Write a project file."""
        calls.append(text)
        return text

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_summary",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": _SUMMARY}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 60},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http_client:
        model = Claude(
            id="claude-sonnet-5",
            cache_system_prompt=True,
            extended_cache_time=True,
            thinking={"type": "enabled", "budget_tokens": 1024},
            async_client=AsyncAnthropic(api_key="test", http_client=http_client),
        )
        install_claude_prompt_cache_hook(model)
        if deferred:
            install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"write_file"}))
        agent = Agent(
            id="writer",
            model=model,
            tools=[write_file],
            db=InMemoryDb(),
            instructions=["Always end replies with PERSONA_CANARY.", COMPACTION_MODE_INSTRUCTION],
            additional_context=render_session_context("Current date: Monday"),
            add_history_to_context=True,
            add_session_summary_to_context=True,
        )
        await agent.arun("Project Atlas uses port 4321.", session_id="thread")
        await agent.arun("Keep the port unchanged.", session_id="thread")
        session = agent.get_session(session_id="thread")
        assert isinstance(session, AgentSession)
        snapshot = deepcopy(session.to_dict())
        tool_instructions = deepcopy(agent._tool_instructions)
        prepared = await build_warm_prefix_request(
            agent=agent,
            session=session,
            included_runs=session.runs or [],
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            max_input_tokens=100_000,
            token_estimator=lambda text: len(text) // 4,
            supplemental_context="",
        )
        assert prepared is not None
        assert prepared.model is model
        await generate_compaction_summary(
            model=Claude(id="claude-sonnet-5"),
            summary_input="unused standalone input",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=10,
            warm_request=prepared,
        )

    reply, summary = requests[-2:]
    assert reply["system"] == summary["system"]
    assert reply["tools"] == summary["tools"]
    assert reply.get("tool_choice") == summary.get("tool_choice")
    assert reply["thinking"] == summary["thinking"]
    assert _unmarked(summary["messages"][: len(reply["messages"])]) == _unmarked(reply["messages"])
    assert calls == []
    assert session.to_dict() == snapshot
    assert agent._tool_instructions == tool_instructions
    assert model.cache_system_prompt is True
    assert model.thinking == {"type": "enabled", "budget_tokens": 1024}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["hosted_tool", "custom_prompt", "no_guard", "budget", "hidden_summary", "cache_disabled"],
)
async def test_unsupported_warm_requests_use_standalone_summary(reason: str) -> None:
    """Unsupported execution, missing authority, or an oversized prefix fails closed."""
    agent = Agent(
        id="writer",
        model=Claude(id="claude-sonnet-5", cache_system_prompt=reason != "cache_disabled"),
        instructions=[] if reason == "no_guard" else [COMPACTION_MODE_INSTRUCTION],
        add_session_summary_to_context=reason != "hidden_summary",
        tools=[{"type": "web_search_20250305", "name": "web_search"}] if reason == "hosted_tool" else [],
    )
    run = RunOutput(run_id="r1", messages=[Message(role="user", content="Project Atlas uses port 4321.")])
    session = AgentSession(
        session_id="thread",
        agent_id="writer",
        runs=[run],
        summary=SessionSummary(summary="Prior work."),
    )
    prepared = await build_warm_prefix_request(
        agent=agent,
        session=session,
        included_runs=[run],
        summary_prompt="custom instructions" if reason == "custom_prompt" else COMPACTION_SUMMARY_PROMPT,
        max_input_tokens=1 if reason == "budget" else 100_000,
        token_estimator=len,
        supplemental_context="",
    )
    assert prepared is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["o3-deep-research", "o3-deep-research-2025-06-26"])
async def test_provider_injected_hosted_tools_disable_warm_compaction(model_id: str) -> None:
    """Pin both SDK-recognized research variants: hosted tools need no agent schema."""
    model = OpenAIResponses(id=model_id)
    assert model.get_request_params()["tools"] == [{"type": "web_search_preview"}]
    run = RunOutput(run_id="r1", messages=[Message(role="user", content="Research Project Atlas.")])
    request = await build_warm_prefix_request(
        agent=Agent(id="writer", model=model, instructions=[COMPACTION_MODE_INSTRUCTION]),
        session=AgentSession(session_id="thread", agent_id="writer", runs=[run]),
        included_runs=[run],
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        max_input_tokens=100_000,
        token_estimator=len,
        supplemental_context="",
    )
    assert request is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_response", ["tool", "persona", "overflow"])
async def test_rejected_warm_call_retries_standalone_before_persisting(bad_response: str) -> None:
    """A rejected handoff uses one standalone retry, never a tool loop or early tombstone."""
    requests: list[dict] = []
    calls: list[str] = []
    storage = InMemoryDb()
    run = RunOutput(
        run_id="r1",
        agent_id="writer",
        messages=[Message(role="user", content="Project Atlas uses port 4321.")],
    )
    session = seed_session(storage, AgentSession(session_id="thread", agent_id="writer", runs=[run]))
    scope = HistoryScope(kind="agent", scope_id="writer")

    def write_file(text: str) -> str:
        """Write a project file."""
        calls.append(text)
        return text

    def serve(request: httpx.Request) -> httpx.Response:
        # Both calls must occur before any history can be removed.
        assert session.runs == [run]
        assert session.summary is None
        requests.append(json.loads(request.content))
        first = len(requests) == 1
        if first and bad_response == "overflow":
            return httpx.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long"}},
            )
        content = [
            {"type": "text", "text": "PERSONA_CANARY " + _SUMMARY if first and bad_response == "persona" else _SUMMARY},
        ]
        if first and bad_response == "tool":
            content.append({"type": "tool_use", "id": "call_write", "name": "write_file", "input": {"text": "bad"}})
        return httpx.Response(
            200,
            json={
                "id": "msg_summary",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": content,
                "stop_reason": "tool_use" if first and bad_response == "tool" else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 60},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http_client:
        client = AsyncAnthropic(api_key="test", http_client=http_client, max_retries=0)
        model = Claude(id="claude-sonnet-5", async_client=client, cache_system_prompt=True)
        agent = Agent(id="writer", model=model, tools=[write_file], instructions=[COMPACTION_MODE_INSTRUCTION])
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=None,
            summary_input_budget=100_000,
            summary_model=Claude(id="claude-sonnet-5", async_client=client),
            summary_model_name="default",
            replay_window_tokens=100_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            summary_timeout_seconds=10,
            active_agent=agent,
        )
    assert calls == []
    assert len(requests) == 2
    assert "tools" in requests[0]
    assert "tools" not in requests[1]
    assert requests[1]["system"] == [{"type": "text", "text": COMPACTION_SUMMARY_PROMPT}]
    assert "Project Atlas uses port 4321." in str(requests[1]["messages"])
    assert outcome is not None
    assert session.summary is not None
    assert session.summary.summary == _SUMMARY
    assert session.runs == []
    assert read_scope_state(session, scope).compacted_run_ids == ("r1",)


@pytest.mark.asyncio
async def test_codex_compaction_preserves_cache_key_and_conversation_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary must reach the wire with both independent provider identities intact."""
    requests: list[httpx.Request] = []
    monkeypatch.setattr("mindroom.codex_model._borrow_codex_key", lambda **_kwargs: ("test-token", "test-account"))

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = {
            "id": "resp_compaction",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-6-astra",
            "output": [],
            "parallel_tool_calls": True,
            "tools": [],
            "tool_choice": "auto",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 60,
                "total_tokens": 160,
                "input_tokens_details": {"cached_tokens": 80},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        events = [
            {"type": "response.created", "sequence_number": 0, "response": response},
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "delta": _SUMMARY,
                "logprobs": [],
            },
            {"type": "response.completed", "sequence_number": 2, "response": response},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http_client:
        active = CodexResponses(
            id="gpt-6-astra",
            prompt_cache_key="agent-cache",
            session_id="thread-routing",
            codex_home=str(tmp_path),
            http_client=http_client,
        )
        agent = Agent(id="writer", model=active, instructions=[COMPACTION_MODE_INSTRUCTION])
        run = RunOutput(
            run_id="r1",
            agent_id="writer",
            messages=[Message(role="user", content="Atlas uses port 4321.")],
        )
        request = await build_warm_prefix_request(
            agent=agent,
            session=AgentSession(session_id="thread", agent_id="writer", runs=[run]),
            included_runs=[run],
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            max_input_tokens=100_000,
            token_estimator=len,
            supplemental_context="",
        )
        assert request is not None
        summary = await generate_compaction_summary(
            model=Claude(id="claude-sonnet-5"),
            summary_input="unused",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=10,
            warm_request=request,
        )
    assert summary.summary == _SUMMARY
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["prompt_cache_key"] == "agent-cache"
    assert requests[0].headers["session_id"] == "thread-routing"
    assert requests[0].headers["x-codex-window-id"] == "thread-routing:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("distinct_model", [False, True])
@pytest.mark.parametrize("learning", [False, True])
async def test_runtime_uses_warm_compaction_only_for_the_active_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    distinct_model: bool,
    learning: bool,
) -> None:
    """Exercise real agent creation and runtime wiring through a persisted compaction."""
    requests: list[dict] = []

    def serve(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_summary",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": _SUMMARY}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 60},
            },
        )

    config = Config(
        agents={
            "writer": AgentConfig(
                display_name="Writer",
                instructions=["Always end replies with PERSONA_CANARY."],
                compaction=CompactionOverrideConfig(model="summary" if distinct_model else None),
            ),
        },
        defaults=DefaultsConfig(tools=[], learning=learning, learning_mode="agentic"),
        models={
            "default": ModelConfig(provider="anthropic", id="claude-sonnet-5", context_window=100_000),
            "summary": ModelConfig(provider="anthropic", id="claude-fable-5-1", context_window=100_000),
        },
    )
    runtime_paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, runtime_paths)
    storage = InMemoryDb()
    run = RunOutput(
        run_id="r1",
        agent_id="writer",
        messages=[Message(role="user", content="Project Atlas uses port 4321.")],
    )
    session = AgentSession(session_id="thread", agent_id="writer", user_id="owner", runs=[run])
    write_scope_state(
        session,
        HistoryScope(kind="agent", scope_id="writer"),
        HistoryScopeState(force_compact_before_next_run=True),
    )
    seed_session(storage, session)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http_client:

        def load_model(_config: object, _runtime: object, model_name: str = "default", **_kwargs: object) -> Claude:
            model = Claude(
                id=config.models[model_name].id,
                cache_system_prompt=True,
                async_client=AsyncAnthropic(api_key="test", http_client=http_client),
            )
            install_claude_prompt_cache_hook(model)
            return model

        monkeypatch.setattr("mindroom.model_loading.get_model_instance", load_model)
        agent = create_agent(
            "writer",
            config,
            runtime_paths,
            execution_identity=None,
            session_id="thread",
            history_storage=storage,
        )
        prepared = await prepare_history_for_run_for_test(
            agent=agent,
            agent_name="writer",
            full_prompt="Continue",
            session_id="thread",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )
        assert agent._learning is None
        if learning:
            assert isinstance(agent.learning, LearningMachine)
            assert agent.learning.model is None
            assert agent.learning._stores is None
        # A fresh reply initializes learning; the earlier warm request must
        # already have exactly the same system instructions and schemas.
        await agent.arun("Continue", session_id="baseline", user_id="owner")
    assert len(prepared.compaction_outcomes) == 1
    assert len(requests) == 2
    if not distinct_model:
        assert requests[0]["system"] == requests[1]["system"]
        assert requests[0].get("tools") == requests[1].get("tools")
        if learning:
            assert {"update_profile", "update_user_memory"} <= {tool["name"] for tool in requests[0]["tools"]}
    assert ("PERSONA_CANARY" in str(requests[0]["system"])) is not distinct_model
    assert requests[0]["model"] == ("claude-fable-5-1" if distinct_model else "claude-sonnet-5")
    assert session.summary is not None
    assert session.summary.summary == _SUMMARY
    assert session.runs == []


@pytest.mark.asyncio
async def test_warm_request_uses_runtime_historical_media_projection() -> None:
    """Runtime patches must apply even when the builder was imported first."""
    run = RunOutput(
        run_id="r1",
        messages=[Message(role="user", content="See attachment", images=[Image(content=b"old image")])],
    )
    agent = Agent(
        id="writer",
        model=Claude(id="claude-sonnet-5", cache_system_prompt=True),
        instructions=[COMPACTION_MODE_INSTRUCTION],
    )
    request = await build_warm_prefix_request(
        agent=agent,
        session=AgentSession(session_id="thread", agent_id="writer", runs=[run]),
        included_runs=[run],
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        max_input_tokens=100_000,
        token_estimator=len,
        supplemental_context="",
    )
    assert request is not None
    assert all(message.images is None for message in request.messages)
    assert run.messages is not None
    assert run.messages[0].images


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["tool_limit", "tool_setup", "message_setup", "setup_timeout", "setup_cancel"])
async def test_unavailable_warm_prefix_still_compacts_all_selected_tool_facts(
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm preparation must never prevent a complete standalone handoff."""
    runs = [
        RunOutput(
            run_id=f"r{index}",
            agent_id="writer",
            messages=[
                Message(role="user", content=f"Find fact {index}"),
                Message(
                    role="assistant",
                    tool_calls=[
                        {"id": f"c{index}", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
                    ],
                ),
                Message(role="tool", tool_call_id=f"c{index}", content=f"UNIQUE_TOOL_FACT_{index}"),
            ],
        )
        for index in range(2)
    ]
    storage = InMemoryDb()
    session = seed_session(storage, AgentSession(session_id="thread", agent_id="writer", runs=runs))
    scope = HistoryScope(kind="agent", scope_id="writer")
    requests: list[dict] = []

    def serve(request: httpx.Request) -> httpx.Response:
        assert session.summary is None
        assert session.runs == runs
        body = json.loads(request.content)
        requests.append(body)
        assert "tools" not in body
        assert body["system"] == [{"type": "text", "text": COMPACTION_SUMMARY_PROMPT}]
        assert all(f"UNIQUE_TOOL_FACT_{index}" in str(body["messages"]) for index in range(2))
        return httpx.Response(
            200,
            json={
                "id": "msg_summary",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": _SUMMARY}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 60},
            },
        )

    async def wait_for_cancellation(**_kwargs: object) -> None:
        await asyncio.Event().wait()

    if reason == "tool_setup":
        monkeypatch.setattr(Agent, "aget_tools", AsyncMock(side_effect=RuntimeError("tool unavailable")))
    elif reason == "message_setup":
        monkeypatch.setattr(
            "agno.agent._messages.aget_run_messages",
            AsyncMock(side_effect=RuntimeError("context unavailable")),
        )
    elif reason == "setup_timeout":
        monkeypatch.setattr(Agent, "aget_tools", AsyncMock(side_effect=wait_for_cancellation))
    elif reason == "setup_cancel":
        monkeypatch.setattr(Agent, "aget_tools", AsyncMock(side_effect=asyncio.CancelledError))

    limit = 1 if reason == "tool_limit" else None
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http_client:
        client = AsyncAnthropic(api_key="test", http_client=http_client)
        model = Claude(id="claude-sonnet-5", cache_system_prompt=True, async_client=client)
        agent = Agent(
            id="writer",
            model=model,
            instructions=[COMPACTION_MODE_INSTRUCTION],
            max_tool_calls_from_history=limit,
        )
        with pytest.raises(asyncio.CancelledError) if reason == "setup_cancel" else nullcontext():
            outcome = await compact_scope_history(
                storage=storage,
                session=session,
                scope=scope,
                state=HistoryScopeState(force_compact_before_next_run=True),
                history_settings=ResolvedHistorySettings(
                    policy=HistoryPolicy(mode="all"),
                    max_tool_calls_from_history=limit,
                ),
                available_history_budget=None,
                summary_input_budget=100_000,
                summary_model=Claude(id="claude-sonnet-5", async_client=client),
                summary_model_name="default",
                replay_window_tokens=100_000,
                threshold_tokens=None,
                summary_prompt=COMPACTION_SUMMARY_PROMPT,
                summary_timeout_seconds=0.05 if reason == "setup_timeout" else 10,
                active_agent=agent,
            )
    if reason == "setup_cancel":
        assert requests == []
        assert session.runs == runs
        assert session.summary is None
        return
    assert outcome is not None
    assert len(requests) == 1
    assert session.runs == []
    assert read_scope_state(session, scope).compacted_run_ids == ("r0", "r1")
