"""Tests for the `compact_context` manual compaction trigger."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.custom_tools.compact_context import CompactContextTools
from mindroom.history.manual import request_compaction_before_next_reply
from mindroom.history.session_context import load_scope_session
from mindroom.history.storage import (
    has_queued_force_compaction,
    read_scope_state,
    with_force_compaction,
)
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.message_target import MessageTarget
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import ConversationStore, persist_to
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_conversation_reader_mock,
    make_relation_lookup,
    prepare_history_for_run_for_test,
    seed_session,
)
from tests.fakes.summary_model import summary_model
from tests.history_helpers import (
    RecordingCompactionLifecycle,
    _runtime_paths,
    _session,
    completed_outcome,
    folded_run_ids,
)
from tests.history_helpers import _make_config as _history_config

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


COMPACT_CONTEXT_SUCCESS = "Compaction will run before the next reply in this conversation scope."


def _make_config(
    tmp_path: Path,
    *,
    context_window: int | None = 48_000,
    compaction: CompactionConfig | None = None,
) -> tuple[Config, RuntimePaths]:
    return _history_config(tmp_path, context_window=context_window, defaults_compaction=compaction)


def _completed_run(run_id: str, *, agent_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        status=RunStatus.completed,
    )


def _execution_identity(*, agent_name: str = "test_agent") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )


_AGENT_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")


def _member_runtime_context(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str = "test_agent",
) -> ToolRuntimeContext:
    """Return the tool context of a team member's turn; the team scope travels on each call's context instead."""
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget(
            room_id="!room:localhost",
            source_thread_id="thread-1",
            resolved_thread_id="thread-1",
            reply_to_event_id=None,
            session_id="session-1",
        ),
        requester_id="@alice:localhost",
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


async def _compact_as_team_member(tool: CompactContextTools, team_scope: HistoryScope) -> object:
    """Call ``compact_context`` as the team runtime runs a member's call: with the team scope on its context."""
    [function] = adapt_toolkit(tool).functions
    context = ToolCallContext(run_id="member-run", session_id="session-1", history_scope=team_scope, user_id=None)
    return (await run_tool_call(function, {}, context, middleware=())).value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "compaction",
    [None, CompactionConfig(enabled=False), CompactionConfig()],
    ids=["built-in", "disabled", "enabled"],
)
async def test_request_compaction_before_next_reply_is_public_manual_seam(
    tmp_path: Path,
    compaction: CompactionConfig | None,
) -> None:
    """Manual compaction is scheduled without the tool adapter, whether or not compaction runs automatically."""
    config, runtime_paths = _make_config(tmp_path, compaction=compaction)
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    seed_session(storage, _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))

    result = await request_compaction_before_next_reply(
        scope=_AGENT_SCOPE,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
        active_model_name=None,
        room_id=None,
    )

    assert has_queued_force_compaction(storage, "session-1")
    assert result == COMPACT_CONTEXT_SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_window", "expected_error"),
    [
        (
            None,
            "Error: Compaction is unavailable for this scope because no context_window is configured on the "
            "active model.",
        ),
        (
            4096,
            "Error: Compaction is unavailable for this scope because the active compaction model leaves no "
            "usable summary input budget after reserve and prompt overhead.",
        ),
        (
            10_000,
            "Error: Compaction is unavailable for this scope because the summary input budget must exceed "
            "2,000 tokens to provide meaningful headroom for a smaller retry.",
        ),
    ],
    ids=["no-context-window", "no-summary-input-budget", "no-retry-headroom"],
)
async def test_compact_context_refuses_a_scope_without_a_usable_summary_budget(
    tmp_path: Path,
    context_window: int | None,
    expected_error: str,
) -> None:
    """A request that manual validation refuses never opens the scope's store, so it leaves the session untouched."""
    config, runtime_paths = _make_config(tmp_path, context_window=context_window)
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    seed_session(storage, _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    with patch("mindroom.history.manual.create_scope_session_storage") as open_store:
        result = await tool.compact_context()

    assert result == expected_error
    open_store.assert_not_called()
    assert not has_queued_force_compaction(storage, "session-1")


@pytest.mark.asyncio
async def test_compact_context_saves_only_the_scope_metadata(tmp_path: Path) -> None:
    """Scheduling writes only the session metadata, so it cannot overwrite runs or tool state a reply saves."""
    config, runtime_paths = _make_config(tmp_path)
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    seeded = _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")])
    seeded.metadata = {"other": "kept"}
    seeded.session_state = {"tool": "state"}
    seed_session(storage, seeded)
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    result = await tool.compact_context()

    assert result == COMPACT_CONTEXT_SUCCESS
    stored = storage.load_session("session-1")
    assert stored is not None
    assert [run.run_id for run in stored.runs] == ["run-1"]
    assert stored.session_state == {"tool": "state"}
    assert stored.metadata == with_force_compaction({"other": "kept"})


@pytest.mark.asyncio
async def test_compact_context_writes_the_request_in_the_database_s_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request's metadata write runs in the database's lane, behind the session's other writes."""
    config, runtime_paths = _make_config(tmp_path)
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    seed_session(storage, _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))
    writing_threads: list[str] = []
    update_metadata = ConversationStore.update_metadata

    def recording(self: ConversationStore, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        writing_threads.append(threading.current_thread().name)
        update_metadata(self, *args, **kwargs)

    monkeypatch.setattr(ConversationStore, "update_metadata", recording)
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    assert await tool.compact_context() == COMPACT_CONTEXT_SUCCESS

    assert len(writing_threads) == 1
    assert writing_threads[0].startswith("conversation-store"), writing_threads


@pytest.mark.asyncio
async def test_compact_context_can_use_compaction_model_window_when_active_model_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual compaction should work when only the selected compaction model declares a context window."""
    config, runtime_paths = _history_config(
        tmp_path,
        defaults_compaction=CompactionConfig(model="summary-model"),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=None),
            "summary-model": ModelConfig(provider="openai", id="summary-model", context_window=32_000),
        },
    )
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", agent_id="test_agent"),
            _completed_run("run-2", agent_id="test_agent"),
            _completed_run("run-3", agent_id="test_agent"),
            _completed_run("run-4", agent_id="test_agent"),
        ],
    )
    seed_session(storage, session)

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    result = await tool.compact_context()
    assert result == COMPACT_CONTEXT_SUCCESS

    install_model_providers(monkeypatch, {"summary-model": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(
            return_value="merged summary",
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "merged summary"
    assert not has_queued_force_compaction(storage, "session-1")
    assert prepared.compaction_reply_outcome == "success"
    outcome = completed_outcome(lifecycle)
    assert outcome.window_tokens == 0
    assert outcome.history_budget_tokens is None
    assert outcome.to_notice_metadata()["version"] == 3
    assert outcome.to_notice_metadata()["window_tokens"] == 0
    assert "history budget" not in outcome.format_notice()
    assert "/ 0 " not in outcome.format_notice()


@pytest.mark.asyncio
async def test_compact_context_follows_the_agents_own_compaction_config(tmp_path: Path) -> None:
    """An agent's own compaction model makes manual compaction available where the defaults leave no window."""
    config, runtime_paths = _history_config(
        tmp_path,
        compaction=CompactionOverrideConfig(model="summary-model"),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=None),
            "summary-model": ModelConfig(provider="openai", id="summary-model", context_window=32_000),
        },
    )
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_execution_identity(),
    )

    assert await tool.compact_context() == COMPACT_CONTEXT_SUCCESS


@pytest.mark.asyncio
async def test_compact_context_sets_force_flag_for_team_scope_only(tmp_path: Path) -> None:
    """Only the team scope should receive the forced-compaction flag."""
    config, runtime_paths = _make_config(tmp_path)
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    seed_session(storage, _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    team_scope = HistoryScope(kind="team", scope_id="team-123")
    team_context = load_scope_session(
        scope=team_scope,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
    ).with_session()
    assert team_context.session is not None
    seed_session(team_context.storage, team_context.session)
    with tool_runtime_context(_member_runtime_context(config, runtime_paths)):
        await _compact_as_team_member(tool, team_scope)

    assert not has_queued_force_compaction(storage, "session-1")
    assert has_queued_force_compaction(team_context.storage, "session-1")


@pytest.mark.asyncio
async def test_prepare_history_for_run_clears_forced_flag_when_no_visible_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced compaction clears itself when the scope has no visible runs to compact."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=_execution_identity())
    session = _session("session-1")
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)

    summary_mock = AsyncMock()
    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=summary_mock,
    ):
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert not has_queued_force_compaction(storage, "session-1")
    assert prepared.compaction_reply_outcome == "failed"
    summary_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_compacts_single_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced compaction of a single run produces a summary and clears the flag."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=_execution_identity())
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", agent_id="test_agent"),
        ],
    )
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(
            return_value="single run summary",
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "single run summary"
    assert persisted.runs == []
    assert not has_queued_force_compaction(storage, "session-1")
    assert len(folded_run_ids(storage)) == 1
    assert prepared.compaction_reply_outcome == "success"


@pytest.mark.asyncio
async def test_a_request_survives_the_requesting_runs_terminal_save_and_a_concurrent_metadata_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata flag alone carries a request to the next reply: no run save writes metadata, and changes merge."""
    config, runtime_paths = _make_config(tmp_path)
    identity = _execution_identity()
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=identity)
    seed_session(storage, _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))
    # The requesting run and another writer both loaded the session before the run's tool call asked for compaction.
    running = storage.load_session("session-1")
    other_copy = storage.load_session("session-1")
    assert running is not None
    assert other_copy is not None
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    assert await tool.compact_context() == COMPACT_CONTEXT_SUCCESS
    storage.update_metadata(other_copy, lambda metadata: {**(metadata or {}), "other": "kept"})
    # The run's tools changed the state they share, which its terminal record saves with it.
    running.session_state["tool"] = "state"
    await persist_to(storage, running)(_completed_run("run-2", agent_id="test_agent"))

    stored = storage.load_session("session-1")
    assert stored is not None
    assert stored.session_state == {"tool": "state"}
    assert stored.metadata == with_force_compaction({"other": "kept"})
    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock(return_value="summary")):
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
        )

    assert prepared.compaction_reply_outcome == "success"
    compacted = storage.load_session("session-1")
    assert compacted is not None
    assert compacted.summary is not None
    assert compacted.summary.text == "summary"
    # The request was the flag, which the compaction cleared, so the reply after this one does not compact again.
    assert not has_queued_force_compaction(storage, "session-1")


@pytest.mark.asyncio
async def test_a_requested_compaction_keeps_metadata_another_writer_stored_meanwhile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that compacts on a queued request clears the flag in the row's metadata, not in its loaded copy."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=_execution_identity())
    seeded = _session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")])
    seeded.metadata = with_force_compaction(seeded.metadata)
    seed_session(storage, seeded)
    session = storage.load_session("session-1")
    assert session is not None
    # Another writer changes the row's metadata after this reply loaded it.
    other = storage.load_session("session-1")
    assert other is not None
    storage.update_metadata(other, lambda metadata: {**(metadata or {}), "other": "kept"})

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock(return_value="summary")):
        await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    stored = storage.load_session("session-1")
    assert stored is not None
    assert stored.metadata == {"other": "kept"}
    assert stored.summary is not None
    assert not has_queued_force_compaction(storage, "session-1")


@pytest.mark.asyncio
async def test_compact_context_uses_stable_team_scope_storage(tmp_path: Path) -> None:
    """Team-scoped compaction should persist through the stable team session store."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha"),
                "beta": AgentConfig(display_name="Beta"),
            },
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=48_000)},
        ),
        runtime_paths,
    )
    legacy_storage = create_session_storage("alpha", config, runtime_paths, execution_identity=None)
    seed_session(legacy_storage, _session("session-1", runs=[_completed_run("run-1", agent_id="alpha")]))

    identity = _execution_identity(agent_name="beta")
    tool = CompactContextTools(
        agent_name="beta",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    team_scope = HistoryScope(kind="team", scope_id="team-123")

    with tool_runtime_context(_member_runtime_context(config, runtime_paths, agent_name="beta")):
        result = await _compact_as_team_member(tool, team_scope)

    team_context = load_scope_session(
        scope=team_scope,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
    )
    assert team_context.session is not None
    team_state = read_scope_state(team_context.session)
    assert team_state.force_compact_before_next_run is True
    assert result == COMPACT_CONTEXT_SUCCESS


@pytest.mark.asyncio
async def test_compact_context_uses_active_team_model_from_runtime_context(tmp_path: Path) -> None:
    """Team-scoped compaction should honor the actual per-run model override."""
    config, runtime_paths = _history_config(
        tmp_path,
        teams={
            "team_123": TeamConfig(
                display_name="Test Team",
                role="Coordinate work",
                agents=["test_agent"],
            ),
        },
        models={
            "default": ModelConfig(provider="openai", id="default-model", context_window=None),
            "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
        },
    )
    identity = _execution_identity()
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    team_scope = HistoryScope(kind="team", scope_id="team_123")
    team_context = load_scope_session(
        scope=team_scope,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
    ).with_session()
    assert team_context.session is not None
    team_context.session.runs = [_completed_run("run-1", agent_id="test_agent")]
    seed_session(team_context.storage, team_context.session)

    runtime_context = replace(_member_runtime_context(config, runtime_paths), active_model_name="large")

    with tool_runtime_context(runtime_context):
        result = await _compact_as_team_member(tool, team_scope)

    assert has_queued_force_compaction(team_context.storage, "session-1")
    assert result == COMPACT_CONTEXT_SUCCESS


@pytest.mark.asyncio
async def test_compact_context_uses_room_resolved_team_model_when_runtime_model_missing(
    tmp_path: Path,
) -> None:
    """Team-scoped compaction should reuse the room-aware team model resolver."""
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
            room_models={"!room:localhost": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )

    identity = _execution_identity()
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    team_scope = HistoryScope(kind="team", scope_id="team_123")
    team_context = load_scope_session(
        scope=team_scope,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
    ).with_session()
    assert team_context.session is not None
    team_context.session.runs = [_completed_run("run-1", agent_id="test_agent")]
    seed_session(team_context.storage, team_context.session)

    runtime_context = _member_runtime_context(config, runtime_paths)

    with tool_runtime_context(runtime_context):
        result = await _compact_as_team_member(tool, team_scope)

    assert has_queued_force_compaction(team_context.storage, "session-1")
    assert result == COMPACT_CONTEXT_SUCCESS


@pytest.mark.asyncio
async def test_compact_context_uses_room_resolved_agent_model_when_runtime_model_missing(
    tmp_path: Path,
) -> None:
    """Agent-scoped compaction should reuse the room-aware runtime model resolver."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    model="default",
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            room_models={"!room:localhost": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )

    identity = _execution_identity()
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    scope_context = load_scope_session(
        scope=_AGENT_SCOPE,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
    ).with_session()
    assert scope_context.session is not None
    scope_context.session.runs = [_completed_run("run-1", agent_id="test_agent")]
    seed_session(scope_context.storage, scope_context.session)

    runtime_context = _member_runtime_context(config, runtime_paths)

    with tool_runtime_context(runtime_context):
        result = await tool.compact_context()

    assert has_queued_force_compaction(scope_context.storage, "session-1")
    assert result == COMPACT_CONTEXT_SUCCESS
