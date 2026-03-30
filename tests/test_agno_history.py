"""Tests for MindRoom-owned history replay and compaction."""

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
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.messages import RunMessages
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.agents import create_agent, create_session_storage, get_agent_session
from mindroom.ai import _prepare_agent_and_prompt
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import (
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history import PreparedHistory, clear_prepared_history, prepare_bound_agents_for_run, prepare_history_for_run
from mindroom.history.replay import build_replay_plan, is_replay_message
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    read_scope_states,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import CompactionState, HistoryScope
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
    config, _runtime_paths_value = _make_config(tmp_path, num_history_messages=2)
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
        agent=agent,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        state=CompactionState(),
    )

    assert [message.content for message in plan.history_messages] == ["new question", "new answer"]
    assert all(is_replay_message(message) for message in plan.history_messages)


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_team_scope_state_for_team_member(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
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
    storage.upsert_session(session)

    agent = _agent(db=storage)
    agent.team_id = "team-123"
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
    agent._start_learning_future = lambda run_messages, session, user_id, existing_future=None: captured.update(  # type: ignore[method-assign]
        learning_messages=list(run_messages.messages),
    )
    agent._cleanup_and_store = lambda run_response, session, run_context=None, user_id=None: captured.update(  # type: ignore[method-assign]
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

    with patch("mindroom.history.runtime.prepare_history_for_run", new=AsyncMock(side_effect=_fake_prepare)) as mock_prepare:
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


def test_scope_seen_event_ids_reads_legacy_team_metadata_from_previous_commit(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    session = _session(
        "session-1",
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "seen_event_ids": ["event-1"],
            },
        },
    )

    assert read_scope_seen_event_ids(session, HistoryScope(kind="team", scope_id="team-123")) == {"event-1"}
    assert read_scope_seen_event_ids(session, HistoryScope(kind="agent", scope_id="test_agent")) == set()


def test_scope_seen_event_ids_write_preserves_legacy_team_metadata_from_previous_commit(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    session = _session(
        "session-1",
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "seen_event_ids": ["event-1"],
            },
        },
    )
    scope = HistoryScope(kind="team", scope_id="team-123")

    assert update_scope_seen_event_ids(session, scope, ["event-2"]) is True
    assert read_scope_seen_event_ids(session, scope) == {"event-1", "event-2"}
    assert session.metadata == {
        MINDROOM_COMPACTION_METADATA_KEY: {
            "seen_event_ids": ["event-1"],
        },
        MINDROOM_MATRIX_HISTORY_METADATA_KEY: {
            "version": 1,
            "states": {
                "team:team-123": {
                    "seen_event_ids": ["event-1", "event-2"],
                },
            },
        },
    }


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


def test_legacy_single_scope_state_migrates(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "last_compacted_run_id": "run-1",
            },
        },
        summary=SessionSummary(summary="legacy summary", updated_at=datetime.now(UTC)),
    )

    states = read_scope_states(session)

    assert states == {
        "agent:test_agent": CompactionState(
            summary="legacy summary",
            last_compacted_run_id="run-1",
        ),
    }


def test_legacy_mixed_scope_state_is_ignored(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    session = _session(
        "session-1",
        runs=[
            _completed_run("direct-run"),
            _completed_team_run("team-run", team_id="team-123"),
        ],
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "last_compacted_run_id": "direct-run",
            },
        },
        summary=SessionSummary(summary="legacy summary", updated_at=datetime.now(UTC)),
    )

    assert read_scope_states(session) == {}
