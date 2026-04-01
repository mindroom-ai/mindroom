"""Tests for native Agno history replay and destructive compaction."""
# ruff: noqa: D102, D103, ANN201, TC003

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.agents import create_agent, create_session_storage, get_agent_session
from mindroom.ai import _prepare_agent_and_prompt, build_prompt_with_thread_history
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, RuntimePaths, resolve_runtime_paths
from mindroom.history import PreparedHistoryState, prepare_bound_agents_for_run, prepare_history_for_run
from mindroom.history.compaction import (
    _build_summary_input,
    estimate_history_messages_tokens,
    estimate_prompt_visible_history_tokens,
    estimate_session_summary_tokens,
    estimate_static_tokens,
    estimate_tool_definition_tokens,
)
from mindroom.history.policy import resolve_history_execution_plan, should_attempt_destructive_compaction
from mindroom.history.runtime import (
    apply_replay_plan,
    estimate_preparation_static_tokens,
    estimate_preparation_static_tokens_for_team,
    load_scope_session_context,
    plan_replay_that_fits,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)
from mindroom.teams import TeamMode, _create_team_instance
from mindroom.thread_utils import create_session_id
from mindroom.token_budget import estimate_text_tokens, stable_serialize
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
    models: dict[str, ModelConfig] | None = None,
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
            models=(
                models
                if models is not None
                else {
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=context_window,
                    ),
                }
            ),
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


def test_estimate_static_tokens_includes_tool_definitions() -> None:
    def search_docs(query: str, limit: int = 5) -> str:
        """Search the engineering docs for a matching answer."""
        return f"{query}:{limit}"

    def export_notes(title: str, include_metadata: bool = False) -> str:
        """Export the current working notes as markdown with full metadata attached."""
        return f"{title}:{include_metadata}"

    toolkit = Toolkit(
        name="docs",
        tools=[search_docs],
        instructions="Always cite the relevant document section when using search_docs.",
        add_instructions=True,
    )
    export_tool = Function(
        name="export_notes",
        entrypoint=export_notes,
    )
    agent_with_tools = _agent()
    agent_with_tools.role = "Engineer"
    agent_with_tools.instructions = ["Stay concise."]
    agent_with_tools.tools = [toolkit, export_tool]

    baseline_agent = _agent()
    baseline_agent.role = agent_with_tools.role
    baseline_agent.instructions = list(agent_with_tools.instructions)

    expected_export_tool = export_tool.model_copy(deep=True)
    expected_export_tool.process_entrypoint(strict=False)
    expected_payloads = [
        {
            "name": "search_docs",
            "description": "Search the engineering docs for a matching answer.",
            "parameters": Function.from_callable(search_docs).parameters,
        },
        {
            "name": "export_notes",
            "description": "Export the current working notes as markdown with full metadata attached.",
            "parameters": expected_export_tool.parameters,
        },
    ]
    tool_tokens = estimate_tool_definition_tokens(agent_with_tools)
    assert tool_tokens == (
        len(stable_serialize(expected_payloads)) // 4
        + estimate_text_tokens("Always cite the relevant document section when using search_docs.")
    )
    assert estimate_tool_definition_tokens(baseline_agent) == 0
    assert estimate_static_tokens(agent_with_tools, "Current prompt") == (
        estimate_static_tokens(baseline_agent, "Current prompt") + tool_tokens
    )


def test_estimate_tool_definition_tokens_processes_functions_with_custom_parameters() -> None:
    def sync_calendar_event(title: str, include_attendees: bool = False) -> str:
        """Sync the current event draft into the shared calendar."""
        return f"{title}:{include_attendees}"

    custom_tool = Function(
        name="sync_calendar_event",
        entrypoint=sync_calendar_event,
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Calendar event title.",
                },
            },
            "required": ["title"],
        },
    )
    agent = _agent()
    agent.tools = [custom_tool]

    expected_tool = custom_tool.model_copy(deep=True)
    expected_tool.process_entrypoint(strict=False)

    assert expected_tool.description == "Sync the current event draft into the shared calendar."
    assert expected_tool.parameters["additionalProperties"] is False
    assert (
        estimate_tool_definition_tokens(agent)
        == len(
            stable_serialize(
                [
                    {
                        "name": "sync_calendar_event",
                        "description": expected_tool.description,
                        "parameters": expected_tool.parameters,
                    },
                ],
            ),
        )
        // 4
    )


def test_estimate_tool_definition_tokens_ignores_empty_toolkit() -> None:
    agent = _agent()
    agent.tools = [Toolkit(name="empty")]

    assert estimate_tool_definition_tokens(agent) == 0


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


def test_create_agent_uses_active_model_override(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model"),
            },
        ),
        runtime_paths,
    )
    with patch("mindroom.ai.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")) as mock_get:
        create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            active_model_name="large",
            include_interactive_questions=False,
        )

    assert mock_get.call_args is not None
    assert mock_get.call_args.args[2] == "large"


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

    assert prepared.replays_persisted_history is True
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

    agent = _agent(db=storage)
    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
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
            agent=agent,
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

    assert prepared.replays_persisted_history is True
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].summary == "merged summary"


@pytest.mark.asyncio
async def test_prepare_history_for_run_keeps_thread_session_compaction_isolated(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    room_session_id = create_session_id("!room:localhost", None)
    thread_session_id = create_session_id("!room:localhost", "$thread-1")
    room_session = _session(
        room_session_id,
        runs=[
            _completed_run("room-1"),
            _completed_run("room-2"),
            _completed_run("room-3"),
        ],
    )
    thread_session = _session(
        thread_session_id,
        runs=[
            _completed_run("thread-1"),
            _completed_run("thread-2"),
            _completed_run("thread-3"),
            _completed_run("thread-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(thread_session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(room_session)
    storage.upsert_session(thread_session)

    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(
                    summary="thread summary",
                    updated_at=datetime.now(UTC),
                ),
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id=thread_session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=thread_session,
        )

    persisted_room = get_agent_session(storage, room_session_id)
    persisted_thread = get_agent_session(storage, thread_session_id)
    assert persisted_room is not None
    assert persisted_thread is not None
    assert persisted_room.summary is None
    assert [run.run_id for run in persisted_room.runs] == ["room-1", "room-2", "room-3"]
    assert persisted_thread.summary is not None
    assert persisted_thread.summary.summary == "thread summary"
    assert [run.run_id for run in persisted_thread.runs] == ["thread-3", "thread-4"]
    assert len(prepared.compaction_outcomes) == 1
    outcome = prepared.compaction_outcomes[0]
    assert outcome.session_id == thread_session_id
    assert outcome.scope == scope.key
    assert outcome.to_notice_metadata()["session_id"] == thread_session_id
    assert outcome.to_notice_metadata()["scope"] == scope.key


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_compaction_rechecks_after_merged_summary_growth(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    storage.upsert_session(session)

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary="expanded summary " * 20, updated_at=datetime.now(UTC)),
            SessionSummary(summary="final summary", updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
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
            available_history_budget=160,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "final summary"
    assert [run.run_id for run in persisted.runs] == ["run-3"]
    assert summary_mock.await_count == 2
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].runs_after == 1


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_context_window_guard_without_authored_compaction(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=600)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    agent = _agent(db=storage)
    prepared = await prepare_history_for_run(
        agent=agent,
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
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.add_session_summary_to_context is True
    assert prepared.replay_plan.num_history_runs == 2
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_context_window_guard_preserves_custom_system_message_role(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=40)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="developer", content="d" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="developer", content="d" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    agent = _agent(db=storage)

    prepared = await prepare_history_for_run(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
            system_message_role="developer",
        ),
        static_prompt_tokens=0,
        available_history_budget=10,
    )

    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.add_session_summary_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


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
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
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
    assert prepared.replays_persisted_history is True


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
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_authored_compaction_still_plans_safe_replay_when_compaction_unavailable(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=600,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )
    storage.upsert_session(session)

    agent = _agent(db=storage)
    prepared = await prepare_history_for_run(
        agent=agent,
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
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_outcomes == []
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.add_session_summary_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_authored_compaction_and_no_window_skips_warning(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=None)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
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
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True
    assert mock_warning.call_args_list == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_with_disabled_compaction_and_no_window_skips_warning(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=False),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
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
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True
    assert mock_warning.call_args_list == []


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
        max_input_tokens=220,
    )

    assert [run.run_id for run in included_runs] == ["run-big"]
    assert "Run truncated to fit compaction budget." in summary_input
    assert 'run_id="run-big"' in summary_input


def test_build_summary_input_skips_when_previous_summary_cannot_be_preserved() -> None:
    run = _completed_run("run-1")

    summary_input, included_runs = _build_summary_input(
        previous_summary="existing durable summary " * 50,
        compacted_runs=[run],
        max_input_tokens=50,
    )

    assert included_runs == []
    assert "<previous_summary>" in summary_input


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
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == estimate_history_messages_tokens(expected_messages)


def test_estimate_prompt_visible_history_tokens_honors_custom_system_message_role() -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="developer", content="Persisted developer prompt"),
                    Message(role="user", content="old user"),
                    Message(role="assistant", content="old assistant"),
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
        system_message_role="developer",
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_messages = [
        Message(role="assistant", content="old assistant"),
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == estimate_history_messages_tokens(expected_messages)


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_prepares_team_scope_once(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")

    def team_lookup(topic: str, include_links: bool = False) -> str:
        """Look up team context for a topic before delegating work."""
        return f"{topic}:{include_links}"

    toolkit = Toolkit(
        name="team_docs",
        tools=[team_lookup],
        instructions="Use the team docs tool before delegating factual questions.",
        add_instructions=True,
    )
    team = Team(
        members=[owner_agent, peer_agent],
        model=FakeModel(id="fake-model", provider="fake"),
        id="team_alpha+beta",
        name="Pair",
        role="Verbose team role",
        tools=[toolkit],
        get_member_information_tool=True,
    )

    prepared_tools = team._determine_tools_for_model(
        model=team.model,
        run_response=TeamRunOutput(
            run_id="history-budget",
            team_id=team.id,
            session_id="history-budget",
            session_state={},
        ),
        run_context=RunContext(run_id="history-budget", session_id="history-budget", session_state={}),
        team_run_context={},
        session=TeamSession(session_id="history-budget", team_id=team.id),
        check_mcp_tools=False,
    )
    expected_payloads = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters,
        }
        for tool in prepared_tools
    ]
    previous_tool_instructions = team._tool_instructions
    try:
        team._tool_instructions = [toolkit.instructions]
        system_message = team.get_system_message(
            session=TeamSession(session_id="history-budget", team_id=team.id),
            tools=prepared_tools,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions
    expected_static_prompt_tokens = estimate_text_tokens("Current prompt")
    if system_message is not None and system_message.content is not None:
        expected_static_prompt_tokens += estimate_text_tokens(str(system_message.content))
    expected_static_prompt_tokens += len(stable_serialize(expected_payloads)) // 4

    with patch(
        "mindroom.history.runtime.prepare_history_for_run",
        new=AsyncMock(return_value=PreparedHistoryState(replays_persisted_history=True)),
    ) as mock_prepare:
        prepared = await prepare_bound_agents_for_run(
            agents=[peer_agent, owner_agent],
            team=team,
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        )

    assert prepared.replays_persisted_history is True
    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["agent"] is owner_agent
    assert mock_prepare.await_args.kwargs["agent_name"] == "alpha"
    assert mock_prepare.await_args.kwargs["scope"] == HistoryScope(kind="team", scope_id="team_alpha+beta")
    assert (
        estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt") == expected_static_prompt_tokens
    )
    assert mock_prepare.await_args.kwargs["static_prompt_tokens"] == expected_static_prompt_tokens


def test_estimate_preparation_static_tokens_for_team_includes_agentic_state_tool() -> None:
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")
    team = Team(
        members=[owner_agent, peer_agent],
        model=FakeModel(id="fake-model", provider="fake"),
        id="team_alpha+beta",
        name="Pair",
        role="Stateful team role",
        enable_agentic_state=True,
    )
    budget_session_id = "history-budget"
    session = TeamSession(session_id=budget_session_id, team_id=team.id)
    prepared_tools = team._determine_tools_for_model(
        model=team.model,
        run_response=TeamRunOutput(
            run_id=budget_session_id,
            team_id=team.id,
            session_id=budget_session_id,
            session_state={},
        ),
        run_context=RunContext(
            run_id=budget_session_id,
            session_id=budget_session_id,
            session_state={},
        ),
        team_run_context={},
        session=session,
        check_mcp_tools=False,
    )
    expected_payloads = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters,
        }
        for tool in prepared_tools
    ]
    assert any(tool["name"] == "update_session_state" for tool in expected_payloads)

    previous_tool_instructions = team._tool_instructions
    try:
        team._tool_instructions = []
        system_message = team.get_system_message(
            session=session,
            tools=prepared_tools,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions

    expected_static_prompt_tokens = estimate_text_tokens("Current prompt")
    if system_message is not None and system_message.content is not None:
        expected_static_prompt_tokens += estimate_text_tokens(str(system_message.content))
    expected_static_prompt_tokens += len(stable_serialize(expected_payloads)) // 4

    assert (
        estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt") == expected_static_prompt_tokens
    )


def test_estimate_preparation_static_tokens_for_team_preserves_tool_instructions() -> None:
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")
    team = Team(
        members=[owner_agent, peer_agent],
        model=FakeModel(id="fake-model", provider="fake"),
        id="team_alpha+beta",
        name="Pair",
        role="Stateful team role",
        enable_agentic_state=True,
    )
    team._tool_instructions = ["keep me"]

    estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt")

    assert team._tool_instructions == ["keep me"]


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
    alpha = _agent(agent_id="alpha", name="Alpha")
    zeta = _agent(agent_id="zeta", name="Zeta")

    with patch("mindroom.teams.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        team = _create_team_instance(
            agents=[alpha, zeta],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-alpha-zeta",
            fallback_team_id="Team-alpha-zeta",
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


def test_create_team_instance_preserves_all_history_mode(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha"),
                "zeta": AgentConfig(display_name="Zeta"),
            },
            teams={
                "pair": TeamConfig(
                    display_name="Pair",
                    role="Test team",
                    agents=["alpha", "zeta"],
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
    alpha = _agent(agent_id="alpha", name="Alpha")
    zeta = _agent(agent_id="zeta", name="Zeta")

    with patch("mindroom.teams.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        team = _create_team_instance(
            agents=[alpha, zeta],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-alpha-zeta",
            fallback_team_id="Team-alpha-zeta",
            configured_team_name="pair",
        )

    assert team.num_history_runs is None
    assert team.num_history_messages is None


def test_get_entity_compaction_config_merges_authored_overrides(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(
                        threshold_percent=0.6,
                    ),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    threshold_tokens=12_000,
                    reserve_tokens=2_048,
                    model="summary-model",
                    notify=True,
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    resolved = config.get_entity_compaction_config("test_agent")

    assert resolved.enabled is True
    assert resolved.threshold_tokens is None
    assert resolved.threshold_percent == 0.6
    assert resolved.reserve_tokens == 2_048
    assert resolved.model == "summary-model"
    assert resolved.notify is True


def test_validate_compaction_model_references_does_not_emit_availability_warnings(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    with patch("mindroom.config.main.logger.warning") as mock_warning:
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(enabled=True),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )

    assert mock_warning.call_args_list == []


def test_validate_compaction_model_references_rejects_explicit_model_without_context_window(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"Explicit compaction\.model requires a model with context_window: agents\.test_agent\.compaction\.model -> summary-model",
    ):
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(enabled=True, model="summary-model"),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=48_000,
                    ),
                    "summary-model": ModelConfig(
                        provider="openai",
                        id="summary-model-id",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )


def test_resolve_history_execution_plan_uses_compaction_model_window_only_for_summary_budget(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(model="summary-model"),
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=None,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=None,
        static_prompt_tokens=2_000,
    )

    assert execution_plan.compaction_context_window == 32_000
    assert execution_plan.replay_window_tokens is None
    assert execution_plan.summary_input_budget_tokens is not None
    assert execution_plan.replay_budget_tokens is None
    assert execution_plan.destructive_compaction_available is True


def test_resolve_runtime_model_uses_room_override_for_team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            teams={
                "team_123": TeamConfig(
                    display_name="Test Team",
                    role="Coordinate work",
                    agents=["test_agent"],
                    model="default",
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")

    runtime_model = config.resolve_runtime_model(
        entity_name="team_123",
        room_id="!room:localhost",
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000


def test_resolve_runtime_model_uses_room_override_for_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id="!room:localhost",
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 48_000


def test_resolve_history_execution_plan_marks_non_positive_summary_budget_unavailable(tmp_path: Path) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=4_096,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=4_096,
        static_prompt_tokens=500,
    )

    assert execution_plan.summary_input_budget_tokens == 0
    assert execution_plan.destructive_compaction_available is False
    assert execution_plan.unavailable_reason == "non_positive_summary_input_budget"


def test_should_attempt_destructive_compaction_forced_compaction_takes_priority() -> None:
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=32_000,
        replay_window_tokens=32_000,
        trigger_threshold_tokens=24_000,
        reserve_tokens=16_384,
        static_prompt_tokens=2_000,
        replay_budget_tokens=10_000,
        summary_input_budget_tokens=5_000,
    )

    assert should_attempt_destructive_compaction(
        plan=execution_plan,
        force_compact_before_next_run=True,
        current_history_tokens=None,
    )


def test_should_attempt_destructive_compaction_uses_authored_compaction_when_over_budget() -> None:
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=32_000,
        replay_window_tokens=32_000,
        trigger_threshold_tokens=24_000,
        reserve_tokens=16_384,
        static_prompt_tokens=2_000,
        replay_budget_tokens=10_000,
        summary_input_budget_tokens=5_000,
    )

    assert should_attempt_destructive_compaction(
        plan=execution_plan,
        force_compact_before_next_run=False,
        current_history_tokens=10_001,
    )


def test_plan_replay_that_fits_reduces_replay_for_non_authored_scope(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )

    replay_plan = plan_replay_that_fits(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="runs", limit=2),
            max_tool_calls_from_history=None,
        ),
        available_history_budget=250,
    )

    assert replay_plan.mode == "limited"
    assert replay_plan.history_limit_mode == "runs"
    assert replay_plan.history_limit == 1


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


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_room_resolved_agent_model_for_execution_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")
    live_agent = _agent()

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent) as mock_create_agent,
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
            room_id="!room:localhost",
        )

    assert mock_create_agent.call_args is not None
    assert mock_create_agent.call_args.kwargs["active_model_name"] == "large"
    assert mock_prepare.await_args is not None
    assert mock_prepare.await_args.kwargs["active_model_name"] == "large"
    assert mock_prepare.await_args.kwargs["active_context_window"] == 48_000


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_thread_history_when_persisted_replay_is_disabled(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    thread_history = [
        {"sender": "alice", "body": "Earlier context"},
        {"sender": "bob", "body": "More context"},
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_enhanced_prompt", new=AsyncMock(return_value="Current prompt")),
        patch(
            "mindroom.ai.prepare_history_for_run",
            new=AsyncMock(return_value=PreparedHistoryState(replays_persisted_history=False)),
        ),
    ):
        prepared_agent, full_prompt, _unseen_event_ids, prepared = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    assert prepared_agent is live_agent
    assert prepared.replays_persisted_history is False
    assert full_prompt == build_prompt_with_thread_history("Current prompt", thread_history)


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_without_budget_clears_flag(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
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
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_budget_returns_configured_replay_plan(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        num_history_runs=2,
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage, num_history_runs=2),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    assert prepared.replay_plan == ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            scope=HistoryScope(kind="agent", scope_id="test_agent"),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="runs", limit=2),
                max_tool_calls_from_history=None,
            ),
        ),
        add_history_to_context=True,
        add_session_summary_to_context=True,
        num_history_runs=2,
        num_history_messages=None,
    )
    assert prepared.replays_persisted_history is True
    assert prepared.requires_session_persistence is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_tracks_disabled_replay_separately_from_session_persistence(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        num_history_runs=2,
        context_window=500,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 800),
                    Message(role="assistant", content="a" * 800),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 800),
                    Message(role="assistant", content="a" * 800),
                ],
            ),
        ],
    )
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage, num_history_runs=2),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "disabled"
    assert prepared.replays_persisted_history is False
    assert prepared.requires_session_persistence is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_can_fall_back_to_summary_only(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    agent = _agent(db=storage)
    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                side_effect=[
                    SessionSummary(summary="first summary", updated_at=datetime.now(UTC)),
                    SessionSummary(summary="final summary", updated_at=datetime.now(UTC)),
                ],
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            available_history_budget=1,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "final summary"
    assert persisted.runs == []
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 2
    assert state.force_compact_before_next_run is False
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].runs_after == 0
    assert prepared.compaction_outcomes[0].summary == "final summary"
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "disabled"
    assert prepared.replay_plan.add_history_to_context is False
    assert prepared.replay_plan.add_session_summary_to_context is False


def test_plan_replay_that_fits_disables_summary_only_when_wrapped_summary_exceeds_budget() -> None:
    summary_text = "s" * 360
    available_history_budget = estimate_text_tokens(summary_text)
    assert estimate_session_summary_tokens(summary_text) > available_history_budget

    agent = _agent()
    session = _session(
        "session-1",
        summary=SessionSummary(summary=summary_text, updated_at=datetime.now(UTC)),
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 600),
                    Message(role="assistant", content="a" * 600),
                ],
            ),
        ],
    )

    replay_plan = plan_replay_that_fits(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
        ),
        available_history_budget=available_history_budget,
    )
    apply_replay_plan(target=agent, replay_plan=replay_plan)

    assert replay_plan.mode == "disabled"
    assert agent.add_history_to_context is False
    assert agent.add_session_summary_to_context is False
    assert agent.num_history_runs is None
    assert agent.num_history_messages is None


def test_scope_seen_event_ids_survive_scope_state_writes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    assert update_scope_seen_event_ids(session, scope, ["event-1"]) is True
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))

    assert read_scope_seen_event_ids(session, scope) == {"event-1"}


def test_scope_states_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    write_scope_state(session, agent_scope, HistoryScopeState(force_compact_before_next_run=True))
    write_scope_state(session, team_scope, HistoryScopeState(last_summary_model="summary-model"))

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is True
    assert read_scope_state(session, agent_scope).last_summary_model is None
    assert read_scope_state(session, team_scope).force_compact_before_next_run is False
    assert read_scope_state(session, team_scope).last_summary_model == "summary-model"


def test_legacy_scope_state_metadata_is_ignored(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session(
        "session-1",
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "version": 1,
                "force_compact_before_next_run": True,
            },
        },
    )

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is False

    write_scope_state(session, agent_scope, HistoryScopeState(force_compact_before_next_run=True))

    assert session.metadata == {
        MINDROOM_COMPACTION_METADATA_KEY: {
            "version": 2,
            "states": {
                agent_scope.key: {
                    "force_compact_before_next_run": True,
                },
            },
        },
    }


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
async def test_prepare_history_for_run_compaction_preserves_seen_event_ids(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            RunOutput(
                run_id="run-1",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["event-1", "event-2"]},
            ),
            RunOutput(
                run_id="run-2",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["event-3"]},
            ),
            RunOutput(
                run_id="run-3",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["event-4"]},
            ),
            RunOutput(
                run_id="run-4",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["event-5"]},
            ),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
            ),
        ),
    ):
        await prepare_history_for_run(
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
    assert read_scope_seen_event_ids(persisted, scope) == {
        "event-1",
        "event-2",
        "event-3",
        "event-4",
        "event-5",
    }


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
    assert prepared.replays_persisted_history is True
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
