"""Tests for native Agno history replay and destructive compaction."""
# ruff: noqa: D102, D103, ANN201, TC003

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.agents import create_agent, create_session_storage, get_agent_session
from mindroom.ai import _prepare_agent_and_prompt, build_prompt_with_thread_history
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.history import PreparedHistoryState, prepare_bound_agents_for_run, prepare_history_for_run
from mindroom.history.compaction import (
    _build_summary_input,
    estimate_history_messages_tokens,
    estimate_prompt_visible_history_tokens,
)
from mindroom.history.runtime import (
    estimate_preparation_static_tokens,
    load_scope_session_context,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import HistoryPolicy, HistoryScope, HistoryScopeState, ResolvedHistorySettings
from mindroom.teams import TeamMode, _create_team_instance
from tests.conftest import bind_runtime_paths


@dataclass
class FakeModel(Model):
    """Minimal model for deterministic agent creation tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


@dataclass
class RecordingModel(Model):
    """Model that records the final prompt message list."""

    seen_messages: list[Message] = field(default_factory=list)

    def invoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _make_config(
    tmp_path: Path,
    *,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
    compaction: CompactionOverrideConfig | None = None,
    context_window: int | None = 48_000,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    num_history_runs=num_history_runs,
                    num_history_messages=num_history_messages,
                    compaction=compaction,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=context_window,
                ),
            },
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _completed_run(
    run_id: str,
    *,
    agent_id: str = "test_agent",
    messages: list[Message] | None = None,
) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id=agent_id,
        status=RunStatus.completed,
        messages=messages
        or [
            Message(role="user", content=f"{run_id} question"),
            Message(role="assistant", content=f"{run_id} answer"),
        ],
    )


def _completed_team_run(
    run_id: str,
    *,
    team_id: str,
    messages: list[Message] | None = None,
) -> TeamRunOutput:
    return TeamRunOutput(
        run_id=run_id,
        team_id=team_id,
        status=RunStatus.completed,
        messages=messages
        or [
            Message(role="user", content=f"{run_id} team question"),
            Message(role="assistant", content=f"{run_id} team answer"),
        ],
    )


def _session(
    session_id: str,
    *,
    agent_id: str = "test_agent",
    runs: list[RunOutput | TeamRunOutput] | None = None,
    metadata: dict[str, object] | None = None,
    summary: SessionSummary | None = None,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        agent_id=agent_id,
        runs=runs or [],
        metadata=metadata,
        summary=summary,
        created_at=1,
        updated_at=1,
    )


def _team_session(
    session_id: str,
    *,
    team_id: str,
    runs: list[RunOutput | TeamRunOutput] | None = None,
    metadata: dict[str, object] | None = None,
    summary: SessionSummary | None = None,
) -> TeamSession:
    return TeamSession(
        session_id=session_id,
        team_id=team_id,
        runs=runs or [],
        metadata=metadata,
        summary=summary,
        created_at=1,
        updated_at=1,
    )


def _agent(
    *,
    agent_id: str = "test_agent",
    name: str = "Test Agent",
    model: Model | None = None,
    db: object | None = None,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
) -> Agent:
    return Agent(
        id=agent_id,
        name=name,
        model=model or FakeModel(id="fake-model", provider="fake"),
        db=db,
        add_history_to_context=True,
        add_session_summary_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
        store_history_messages=False,
    )


def test_create_agent_enables_agno_native_history_replay(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=2)

    with patch("mindroom.ai.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        agent = create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            include_interactive_questions=False,
        )

    assert agent.add_history_to_context is True
    assert agent.add_session_summary_to_context is True
    assert agent.num_history_runs == 2
    assert agent.num_history_messages is None
    assert agent.store_history_messages is False


@pytest.mark.asyncio
async def test_prepare_history_for_run_detects_persisted_team_history(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    agent = _agent()
    agent.team_id = "team-123"
    scope_context = load_scope_session_context(
        agent=agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    )
    assert scope_context is not None
    assert scope_context.scope == HistoryScope(kind="team", scope_id="team-123")
    session = _team_session(
        "session-1",
        team_id="team-123",
        runs=[_completed_team_run("team-1", team_id="team-123")],
        summary=SessionSummary(summary="team summary", updated_at=datetime.now(UTC)),
    )
    scope_context.storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=scope_context.storage,
        session=session,
    )

    assert prepared.has_persisted_history is True
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_rewrites_session(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.history.compaction.resolve_compaction_model",
            return_value=(FakeModel(id="summary-model", provider="fake"), 64_000),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(
                    summary="merged summary",
                    updated_at=datetime.now(UTC),
                ),
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert [run.run_id for run in persisted.runs] == ["run-3", "run-4"]

    state = read_scope_state(persisted, scope)
    assert state.last_summary_model == "summary-model"
    assert state.last_compacted_run_count == 2
    assert state.force_compact_before_next_run is False
    assert state.last_compacted_at is not None

    assert prepared.has_persisted_history is True
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].summary == "merged summary"


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_failure_clears_force_flag(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.history.compaction.resolve_compaction_model",
            return_value=(FakeModel(id="summary-model", provider="fake"), 64_000),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(side_effect=RuntimeError("summary failed")),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3", "run-4"]

    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert state.last_summary_model is None
    assert state.last_compacted_run_count is None

    assert prepared.compaction_outcomes == []
    assert prepared.has_persisted_history is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_context_window_skips_auto_compaction(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, threshold_tokens=10),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
        ],
    )
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_outcomes == []
    assert prepared.has_persisted_history is True


def test_build_summary_input_advances_past_oversized_oldest_run() -> None:
    big_run = _completed_run(
        "run-big",
        messages=[
            Message(role="user", content="u" * 800),
            Message(role="assistant", content="a" * 800),
        ],
    )
    small_run = _completed_run("run-small")

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[big_run, small_run],
        max_input_tokens=100,
    )

    assert [run.run_id for run in included_runs] == ["run-big"]
    assert "Run truncated to fit compaction budget." in summary_input
    assert 'run_id="run-big"' in summary_input


def test_estimate_prompt_visible_history_tokens_uses_agno_message_limit_selection() -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="system", content="Persisted system"),
                    Message(role="user", content="old user"),
                    Message(
                        role="assistant",
                        content="old assistant",
                        tool_calls=[
                            {"id": "call-1", "type": "function", "function": {"name": "tool", "arguments": "{}"}},
                        ],
                    ),
                    Message(role="tool", content="old tool"),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="new user"),
                    Message(role="assistant", content="new assistant"),
                ],
            ),
        ],
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="messages", limit=3),
        max_tool_calls_from_history=None,
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_messages = [
        Message(role="system", content="Persisted system"),
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == estimate_history_messages_tokens(expected_messages)


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_prepares_team_scope_once(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")

    with patch(
        "mindroom.history.runtime.prepare_history_for_run",
        new=AsyncMock(return_value=PreparedHistoryState(has_persisted_history=True)),
    ) as mock_prepare:
        prepared = await prepare_bound_agents_for_run(
            agents=[peer_agent, owner_agent],
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        )

    assert prepared.has_persisted_history is True
    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["agent"] is owner_agent
    assert mock_prepare.await_args.kwargs["agent_name"] == "alpha"
    assert mock_prepare.await_args.kwargs["scope"] == HistoryScope(kind="team", scope_id="team_alpha+beta")


def test_create_team_instance_enables_native_team_history_and_disables_members(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha", num_history_messages=100),
                "zeta": AgentConfig(display_name="Zeta", num_history_messages=1),
            },
            teams={
                "pair": TeamConfig(
                    display_name="Pair",
                    role="Test team",
                    agents=["alpha", "zeta"],
                    num_history_messages=2,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=2_000,
                ),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    alpha = _agent(agent_id="alpha", name="Alpha")
    zeta = _agent(agent_id="zeta", name="Zeta")

    with patch("mindroom.teams.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        team = _create_team_instance(
            [alpha, zeta],
            ["alpha", "zeta"],
            TeamMode.COORDINATE,
            orchestrator,
            configured_team_name="pair",
        )

    assert alpha.add_history_to_context is False
    assert alpha.add_session_summary_to_context is False
    assert zeta.add_history_to_context is False
    assert zeta.add_session_summary_to_context is False
    assert team.add_history_to_context is True
    assert team.add_session_summary_to_context is True
    assert team.num_history_messages == 2
    assert team.store_history_messages is False


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_budgets_against_thread_history_fallback(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    live_agent.role = "Verbose role " + ("r" * 200)
    thread_history = [
        {"sender": "alice", "body": "Earlier context"},
        {"sender": "bob", "body": "More context"},
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_enhanced_prompt", new=AsyncMock(return_value="Current prompt")),
        patch(
            "mindroom.ai.prepare_history_for_run",
            new=AsyncMock(return_value=PreparedHistoryState()),
        ) as mock_prepare,
    ):
        await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    expected_fallback_prompt = build_prompt_with_thread_history("Current prompt", thread_history)
    assert mock_prepare.await_args is not None
    assert mock_prepare.await_args.kwargs["static_prompt_tokens"] == estimate_preparation_static_tokens(
        live_agent,
        full_prompt="Current prompt",
        fallback_full_prompt=expected_fallback_prompt,
    )


def test_scope_seen_event_ids_survive_scope_state_writes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    assert update_scope_seen_event_ids(session, scope, ["event-1"]) is True
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))

    assert read_scope_seen_event_ids(session, scope) == {"event-1"}


def test_scope_seen_event_ids_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session(
        "session-1",
        runs=[
            RunOutput(
                run_id="agent-run",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["agent-event"]},
            ),
            TeamRunOutput(
                run_id="team-run",
                team_id="team-123",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["team-event"]},
            ),
        ],
    )
    update_scope_seen_event_ids(session, team_scope, ["preserved-team-event"])

    assert read_scope_seen_event_ids(session, agent_scope) == {"agent-event"}
    assert read_scope_seen_event_ids(session, team_scope) == {"team-event", "preserved-team-event"}


@pytest.mark.asyncio
async def test_native_agno_replays_summary_and_recent_raw_history_without_persisting_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(
        _session(
            "session-1",
            runs=[
                _completed_run("run-1"),
                _completed_run("run-2"),
            ],
            summary=SessionSummary(summary="stored summary", updated_at=datetime.now(UTC)),
        ),
    )
    model = RecordingModel(id="recording-model", provider="fake")
    agent = _agent(
        model=model,
        db=storage,
        num_history_runs=1,
    )

    response = await agent.arun("Current prompt", session_id="session-1")

    assert response.content == "ok"
    assert model.seen_messages[0].role == "system"
    assert "stored summary" in str(model.seen_messages[0].content)
    assert [message.content for message in model.seen_messages[1:3]] == [
        "run-2 question",
        "run-2 answer",
    ]
    assert [message.from_history for message in model.seen_messages[1:3]] == [True, True]
    assert model.seen_messages[-1].role == "user"
    assert model.seen_messages[-1].content == "Current prompt"

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    latest_run = persisted.runs[-1]
    assert isinstance(latest_run, RunOutput)
    assert [message.content for message in latest_run.messages or []] == [
        model.seen_messages[0].content,
        "Current prompt",
        "ok",
    ]
    assert all(message.from_history is False for message in latest_run.messages or [])
    assert latest_run.additional_input in (None, [])


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_native_history_with_unseen_thread_context(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=1)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[_completed_run("run-1"), _completed_run("run-2")],
        summary=SessionSummary(summary="stored summary", updated_at=datetime.now(UTC)),
    )
    update_scope_seen_event_ids(session, HistoryScope(kind="agent", scope_id="test_agent"), ["event-1"])
    storage.upsert_session(session)

    recording_model = RecordingModel(id="recording-model", provider="fake")
    live_agent = _agent(model=recording_model, db=storage, num_history_runs=1)

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_enhanced_prompt", new=AsyncMock(return_value="Current prompt")),
    ):
        agent, full_prompt, unseen_event_ids, prepared = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=[
                {"event_id": "event-1", "sender": "alice", "body": "Already seen"},
                {"event_id": "event-2", "sender": "alice", "body": "Fresh follow-up"},
                {"event_id": "event-3", "sender": "alice", "body": "Current message body"},
            ],
            session_id="session-1",
            reply_to_event_id="event-3",
        )

    assert unseen_event_ids == ["event-2"]
    assert prepared.has_persisted_history is True
    assert "Fresh follow-up" in full_prompt
    assert "Already seen" not in full_prompt
    assert "stored summary" not in full_prompt
    assert "<history_context>" not in full_prompt

    response = await agent.arun(full_prompt, session_id="session-1")

    assert response.content == "ok"
    assert recording_model.seen_messages[0].role == "system"
    assert "stored summary" in str(recording_model.seen_messages[0].content)
    assert [message.content for message in recording_model.seen_messages[1:3]] == [
        "run-2 question",
        "run-2 answer",
    ]

    final_user_message = recording_model.seen_messages[-1]
    assert final_user_message.role == "user"
    assert isinstance(final_user_message.content, str)
    assert "Fresh follow-up" in final_user_message.content
    assert "Already seen" not in final_user_message.content
    assert "Current prompt" in final_user_message.content
    assert "stored summary" not in final_user_message.content
