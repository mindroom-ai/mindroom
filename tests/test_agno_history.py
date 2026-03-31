"""Tests for MindRoom-owned history replay and compaction."""
# ruff: noqa: D102, D103, ANN201, ARG005, TC003

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.messages import RunMessages
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.agents import create_agent, create_session_storage, get_agent_session
from mindroom.ai import _prepare_agent_and_prompt, build_prompt_with_thread_history
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import (
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history import (
    PreparedHistory,
    clear_bound_agent_history_state,
    clear_prepared_history,
    prepare_bound_agents_for_run,
    prepare_history_for_run,
)
from mindroom.history.compaction import _build_summary_input
from mindroom.history.replay import build_replay_plan, is_replay_message
from mindroom.history.runtime import (
    estimate_preparation_static_tokens,
    load_bound_scope_session_context,
    load_scope_session_context,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import CompactionState, HistoryPolicy, HistoryScope
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
    context_window: int = 48_000,
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
    runs: list[RunOutput | TeamRunOutput] | None = None,
    metadata: dict[str, object] | None = None,
    summary: SessionSummary | None = None,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
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
    model: Model | None = None,
    db: object | None = None,
    additional_input: list[Message] | None = None,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
) -> Agent:
    return Agent(
        id="test_agent",
        model=model or FakeModel(id="fake-model", provider="fake"),
        db=db,
        additional_input=additional_input,
        add_history_to_context=False,
        add_session_summary_to_context=False,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
        store_history_messages=False,
    )


def test_create_agent_disables_agno_native_history_replay(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)

    with patch("mindroom.ai.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        agent = create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            include_interactive_questions=False,
        )

    assert agent.add_history_to_context is False
    assert agent.add_session_summary_to_context is False
    assert agent.num_history_runs is None


def test_message_limited_replay_keeps_newest_messages_from_single_run(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path, num_history_messages=2)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="old question"),
                    Message(role="assistant", content="old answer"),
                    Message(role="user", content="new question"),
                    Message(role="assistant", content="new answer"),
                ],
            ),
        ],
    )
    agent = _agent(num_history_messages=2)

    plan = build_replay_plan(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        state=CompactionState(),
        policy=HistoryPolicy(mode="messages", limit=2),
        max_tool_calls_from_history=agent.max_tool_calls_from_history,
    )

    assert [message.content for message in plan.history_messages] == ["new question", "new answer"]
    assert all(is_replay_message(message) for message in plan.history_messages)


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_team_scope_state_for_team_member(tmp_path: Path) -> None:
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
    assert scope_context.session is not None
    session = _session(
        "session-1",
        runs=[
            _completed_run("direct-run"),
            _completed_team_run("team-old", team_id="team-123"),
            _completed_team_run("team-new", team_id="team-123"),
        ],
    )
    write_scope_state(
        session,
        HistoryScope(kind="agent", scope_id="test_agent"),
        CompactionState(summary="direct summary", last_compacted_run_id="direct-run"),
    )
    write_scope_state(
        session,
        HistoryScope(kind="team", scope_id="team-123"),
        CompactionState(summary="team summary", last_compacted_run_id="team-old"),
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
        session=session,
    )

    assert "team summary" in prepared.summary_prompt_prefix
    assert "direct summary" not in prepared.summary_prompt_prefix
    assert [message.content for message in prepared.history_messages] == [
        "team-new team question",
        "team-new team answer",
    ]


@pytest.mark.asyncio
async def test_clear_prepared_history_restores_original_additional_input(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    storage.upsert_session(session)

    original_input = [Message(role="system", content="existing context")]
    agent = _agent(db=storage, additional_input=original_input)
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

    assert len(prepared.history_messages) == 2
    assert agent.additional_input is not None
    assert len(agent.additional_input) == 3

    clear_prepared_history(agent)

    assert agent.additional_input == original_input


@pytest.mark.asyncio
async def test_prepare_history_for_run_sanitizes_learning_and_persistence_inputs(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    storage.upsert_session(session)

    captured: dict[str, object] = {}
    agent = _agent(db=storage)
    agent._start_learning_future = lambda run_messages, session, user_id, existing_future=None: captured.update(
        learning_messages=list(run_messages.messages),
    )
    agent._cleanup_and_store = lambda run_response, session, run_context=None, user_id=None: captured.update(
        stored_messages=list(run_response.messages or []),
        stored_additional_input=list(run_response.additional_input or []),
    )

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
    replay_message = prepared.history_messages[0]

    run_messages = RunMessages(
        messages=[replay_message, Message(role="assistant", content="fresh answer")],
        extra_messages=[replay_message],
    )
    agent._start_learning_future(run_messages, session, None)

    run_response = RunOutput(
        content="ok",
        messages=[replay_message, Message(role="assistant", content="fresh answer")],
        additional_input=[replay_message],
    )
    agent._cleanup_and_store(run_response, session)

    assert [message.content for message in captured["learning_messages"]] == ["fresh answer"]
    assert [message.content for message in captured["stored_messages"]] == ["fresh answer"]
    assert captured["stored_additional_input"] == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_updates_scope_state(tmp_path: Path) -> None:
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
    write_scope_state(
        session,
        HistoryScope(kind="agent", scope_id="test_agent"),
        CompactionState(force_compact_before_next_run=True),
    )
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
    state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    assert state.summary == "merged summary"
    assert state.last_compacted_run_id == "run-2"
    assert state.force_compact_before_next_run is False
    assert "merged summary" in prepared.summary_prompt_prefix
    assert len(prepared.compaction_outcomes) == 1


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_failure_falls_back_and_clears_force_flag(tmp_path: Path) -> None:
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
    write_scope_state(session, scope, CompactionState(force_compact_before_next_run=True))
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
    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert state.summary is None
    assert prepared.compaction_outcomes == []
    assert prepared.history_messages != []


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


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_prepares_team_scope_once(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    owner_agent = _agent()
    owner_agent.id = "alpha"
    owner_agent.team_id = "team-123"
    peer_agent = _agent()
    peer_agent.id = "beta"
    peer_agent.team_id = "team-123"
    replay_message = Message(role="assistant", content="persisted replay")

    async def _fake_prepare(**kwargs: object) -> PreparedHistory:
        assert kwargs["agent"] is owner_agent
        owner_agent.additional_input = [replay_message]
        return PreparedHistory(
            summary_prompt_prefix="<history_context>\n<summary>\nTeam summary\n</summary>\n</history_context>\n\n",
            history_messages=[replay_message],
            has_stored_replay_state=True,
        )

    with patch(
        "mindroom.history.runtime.prepare_history_for_run",
        new=AsyncMock(side_effect=_fake_prepare),
    ) as mock_prepare:
        prepared = await prepare_bound_agents_for_run(
            agents=[peer_agent, owner_agent],
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        )

    assert mock_prepare.await_count == 1
    assert prepared.has_stored_replay_state is True
    assert peer_agent.additional_input is not None
    assert [message.content for message in peer_agent.additional_input] == ["persisted replay"]


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_uses_named_team_policy_not_owner_member(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(
                    display_name="Alpha",
                    num_history_messages=100,
                ),
                "zeta": AgentConfig(
                    display_name="Zeta",
                    num_history_messages=1,
                ),
            },
            teams={
                "pair": TeamConfig(
                    display_name="Pair",
                    role="Test team",
                    agents=["alpha", "zeta"],
                    num_history_messages=2,
                    compaction=CompactionOverrideConfig(
                        enabled=False,
                        threshold_tokens=1_000,
                        reserve_tokens=0,
                    ),
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
    owner_agent = _agent()
    owner_agent.id = "alpha"
    peer_agent = _agent()
    peer_agent.id = "zeta"
    scope_context = load_bound_scope_session_context(
        agents=[peer_agent, owner_agent],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        create_session_if_missing=True,
    )
    assert scope_context is not None
    assert scope_context.session is not None
    session = _team_session(
        "session-1",
        team_id="pair",
        runs=[
            _completed_team_run(
                "team-1",
                team_id="pair",
                messages=[
                    Message(role="user", content="old question"),
                    Message(role="assistant", content="old answer"),
                    Message(role="user", content="new question"),
                    Message(role="assistant", content="new answer"),
                ],
            ),
        ],
    )
    scope_context.storage.upsert_session(session)

    prepared = await prepare_bound_agents_for_run(
        agents=[peer_agent, owner_agent],
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        active_model_name="default",
        active_context_window=2_000,
    )

    assert [message.content for message in prepared.history_messages] == ["new question", "new answer"]
    assert peer_agent.additional_input is not None
    assert [message.content for message in peer_agent.additional_input] == ["new question", "new answer"]


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_uses_defaults_for_ad_hoc_team_policy(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(
                    display_name="Alpha",
                    num_history_messages=100,
                ),
                "zeta": AgentConfig(
                    display_name="Zeta",
                    num_history_messages=1,
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                num_history_messages=2,
                compaction=CompactionConfig(
                    enabled=False,
                    threshold_tokens=1_000,
                    reserve_tokens=0,
                ),
            ),
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
    owner_agent = _agent()
    owner_agent.id = "alpha"
    peer_agent = _agent()
    peer_agent.id = "zeta"
    scope_context = load_bound_scope_session_context(
        agents=[peer_agent, owner_agent],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    )
    assert scope_context is not None
    assert scope_context.session is not None
    assert scope_context.scope.kind == "team"
    session = _team_session(
        "session-1",
        team_id=scope_context.scope.scope_id,
        runs=[
            _completed_team_run(
                "team-1",
                team_id=scope_context.scope.scope_id,
                messages=[
                    Message(role="user", content="old question"),
                    Message(role="assistant", content="old answer"),
                    Message(role="user", content="new question"),
                    Message(role="assistant", content="new answer"),
                ],
            ),
        ],
    )
    scope_context.storage.upsert_session(session)

    prepared = await prepare_bound_agents_for_run(
        agents=[peer_agent, owner_agent],
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        active_model_name="default",
        active_context_window=2_000,
    )

    assert [message.content for message in prepared.history_messages] == ["new question", "new answer"]
    assert peer_agent.additional_input is not None
    assert [message.content for message in peer_agent.additional_input] == ["new question", "new answer"]


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_budget_uses_active_run_model(tmp_path: Path) -> None:
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
                    compaction=CompactionOverrideConfig(
                        enabled=False,
                        threshold_tokens=160,
                        reserve_tokens=0,
                    ),
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "small": ModelConfig(
                    provider="openai",
                    id="small-model",
                    context_window=60,
                ),
                "large": ModelConfig(
                    provider="openai",
                    id="large-model",
                    context_window=400,
                ),
                "default": ModelConfig(
                    provider="openai",
                    id="default-model",
                    context_window=400,
                ),
            },
        ),
        runtime_paths,
    )
    owner_agent = _agent()
    owner_agent.id = "alpha"
    peer_agent = _agent()
    peer_agent.id = "zeta"
    scope_context = load_bound_scope_session_context(
        agents=[peer_agent, owner_agent],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        create_session_if_missing=True,
    )
    assert scope_context is not None
    assert scope_context.session is not None
    session = _team_session(
        "session-1",
        team_id="pair",
        runs=[
            _completed_team_run(
                "run-1",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-1 question " + ("a" * 160)),
                    Message(role="assistant", content="run-1 answer " + ("b" * 160)),
                ],
            ),
            _completed_team_run(
                "run-2",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-2 question " + ("c" * 160)),
                    Message(role="assistant", content="run-2 answer " + ("d" * 160)),
                ],
            ),
            _completed_team_run(
                "run-3",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-3 question " + ("e" * 160)),
                    Message(role="assistant", content="run-3 answer " + ("f" * 160)),
                ],
            ),
        ],
    )
    scope_context.storage.upsert_session(session)

    prepared_small = await prepare_bound_agents_for_run(
        agents=[peer_agent, owner_agent],
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        active_model_name="small",
        active_context_window=60,
    )
    clear_bound_agent_history_state([peer_agent, owner_agent])

    prepared_large = await prepare_bound_agents_for_run(
        agents=[peer_agent, owner_agent],
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        active_model_name="large",
        active_context_window=400,
    )

    assert len(prepared_small.history_messages) < len(prepared_large.history_messages)


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_counts_owner_static_prompt_tokens(tmp_path: Path) -> None:
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
                    compaction=CompactionOverrideConfig(
                        enabled=False,
                        threshold_tokens=160,
                        reserve_tokens=0,
                    ),
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="default-model",
                    context_window=400,
                ),
            },
        ),
        runtime_paths,
    )
    owner_agent = _agent()
    owner_agent.id = "alpha"
    owner_agent.role = "Verbose owner role " + ("r" * 800)
    owner_agent.instructions = ["Instruction " + ("i" * 800)]
    peer_agent = _agent()
    peer_agent.id = "zeta"
    scope_context = load_bound_scope_session_context(
        agents=[peer_agent, owner_agent],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        create_session_if_missing=True,
    )
    assert scope_context is not None
    assert scope_context.session is not None
    session = _team_session(
        "session-1",
        team_id="pair",
        runs=[
            _completed_team_run(
                "run-1",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-1 question " + ("a" * 160)),
                    Message(role="assistant", content="run-1 answer " + ("b" * 160)),
                ],
            ),
            _completed_team_run(
                "run-2",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-2 question " + ("c" * 160)),
                    Message(role="assistant", content="run-2 answer " + ("d" * 160)),
                ],
            ),
            _completed_team_run(
                "run-3",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-3 question " + ("e" * 160)),
                    Message(role="assistant", content="run-3 answer " + ("f" * 160)),
                ],
            ),
        ],
    )
    scope_context.storage.upsert_session(session)

    prepared = await prepare_bound_agents_for_run(
        agents=[peer_agent, owner_agent],
        full_prompt="Now?",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        active_model_name="default",
        active_context_window=400,
    )

    assert prepared.history_messages == []


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_counts_most_constrained_member_static_prompt_tokens(
    tmp_path: Path,
) -> None:
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
                    compaction=CompactionOverrideConfig(
                        enabled=False,
                        threshold_tokens=160,
                        reserve_tokens=0,
                    ),
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="default-model",
                    context_window=400,
                ),
            },
        ),
        runtime_paths,
    )
    owner_agent = _agent()
    owner_agent.id = "alpha"
    peer_agent = _agent()
    peer_agent.id = "zeta"
    peer_agent.role = "Verbose peer role " + ("r" * 600)
    peer_agent.instructions = ["Instruction " + ("i" * 600)]
    scope_context = load_bound_scope_session_context(
        agents=[peer_agent, owner_agent],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        create_session_if_missing=True,
    )
    assert scope_context is not None
    assert scope_context.session is not None
    session = _team_session(
        "session-1",
        team_id="pair",
        runs=[
            _completed_team_run(
                "run-1",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-1 question " + ("a" * 160)),
                    Message(role="assistant", content="run-1 answer " + ("b" * 160)),
                ],
            ),
            _completed_team_run(
                "run-2",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-2 question " + ("c" * 160)),
                    Message(role="assistant", content="run-2 answer " + ("d" * 160)),
                ],
            ),
            _completed_team_run(
                "run-3",
                team_id="pair",
                messages=[
                    Message(role="user", content="run-3 question " + ("e" * 160)),
                    Message(role="assistant", content="run-3 answer " + ("f" * 160)),
                ],
            ),
        ],
    )
    scope_context.storage.upsert_session(session)

    prepared = await prepare_bound_agents_for_run(
        agents=[peer_agent, owner_agent],
        full_prompt="Now?",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
        active_model_name="default",
        active_context_window=400,
    )

    assert prepared.history_messages == []


def test_entity_compaction_override_enables_and_clears_inherited_thresholds(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(
                    display_name="Alpha",
                    compaction=CompactionOverrideConfig(
                        threshold_percent=0.6,
                        threshold_tokens=None,
                    ),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    threshold_tokens=1_000,
                    reserve_tokens=0,
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="default-model",
                    context_window=4_000,
                ),
            },
        ),
        runtime_paths,
    )

    resolved = config.get_entity_compaction_config("alpha")

    assert resolved.enabled is True
    assert resolved.threshold_tokens is None
    assert resolved.threshold_percent == pytest.approx(0.6)


def test_entity_compaction_override_enabled_null_still_enables_override(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(
                    display_name="Alpha",
                    compaction=CompactionOverrideConfig(
                        enabled=None,
                        threshold_percent=0.6,
                    ),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    threshold_tokens=1_000,
                    reserve_tokens=0,
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="default-model",
                    context_window=4_000,
                ),
            },
        ),
        runtime_paths,
    )

    resolved = config.get_entity_compaction_config("alpha")

    assert resolved.enabled is True
    assert resolved.threshold_tokens is None
    assert resolved.threshold_percent == pytest.approx(0.6)


@pytest.mark.parametrize(
    ("builder", "kwargs"),
    [
        (CompactionConfig, {"enabled": True, "threshold_tokens": 100, "threshold_percent": 0.6}),
        (CompactionOverrideConfig, {"threshold_tokens": 100, "threshold_percent": 0.6}),
    ],
)
def test_compaction_thresholds_are_mutually_exclusive(builder: object, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="threshold_tokens and threshold_percent are mutually exclusive"):
        cast("type[CompactionConfig | CompactionOverrideConfig]", builder)(**kwargs)


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
        patch("mindroom.ai.prepare_history_for_run", new=AsyncMock(return_value=PreparedHistory())) as mock_prepare,
    ):
        await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    assert mock_prepare.await_args is not None
    expected_fallback_prompt = build_prompt_with_thread_history("Current prompt", thread_history)
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
    write_scope_state(session, scope, CompactionState(force_compact_before_next_run=True))

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
async def test_prepare_agent_and_prompt_orders_unseen_summary_and_current_prompt(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1"), _completed_run("run-2")])
    write_scope_state(
        session,
        HistoryScope(kind="agent", scope_id="test_agent"),
        CompactionState(summary="stored summary", last_compacted_run_id="run-1"),
    )
    storage.upsert_session(session)

    recording_model = RecordingModel(id="recording-model", provider="fake")
    live_agent = _agent(model=recording_model, db=storage)

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.build_memory_enhanced_prompt",
            new=AsyncMock(return_value="Current prompt"),
        ),
    ):
        agent, full_prompt, unseen_event_ids, prepared = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=[
                {"sender": "alice", "body": "Unseen message", "event_id": "event-1"},
            ],
            session_id="session-1",
            reply_to_event_id="event-current",
        )

    response = await agent.arun(full_prompt, session_id="session-1")
    assert response.content == "ok"
    assert unseen_event_ids == ["event-1"]
    assert prepared.history_messages
    assert is_replay_message(prepared.history_messages[0]) is True
    assert [message.content for message in recording_model.seen_messages[:2]] == [
        "run-2 question",
        "run-2 answer",
    ]
    final_user_message = recording_model.seen_messages[-1]
    assert final_user_message.role == "user"
    assert isinstance(final_user_message.content, str)
    assert "alice: Unseen message" in final_user_message.content
    assert "<history_context>" in final_user_message.content
    assert "stored summary" in final_user_message.content
    assert final_user_message.content.index("alice: Unseen message") < final_user_message.content.index(
        "<history_context>",
    )
    assert final_user_message.content.index("<history_context>") < final_user_message.content.index("Current prompt")

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    latest_run = persisted.runs[-1]
    assert isinstance(latest_run, RunOutput)
    assert latest_run.additional_input in (None, [])
    assert all(not is_replay_message(message) for message in latest_run.messages or [])

    clear_prepared_history(agent)
