"""Tests for history scope-state and seen-event-id storage."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import (
    MINDROOM_COMPACTION_METADATA_KEY,
)
from mindroom.history.replay import scope_visible_runs
from mindroom.history.storage import (
    _with_scope_state,
    add_pending_force_compaction_scope,
    has_queued_force_compaction,
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
)
from mindroom.history.types import (
    HistoryScope,
    HistoryScopeState,
)
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.session_storage import store as store_module
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.conftest import (
    seed_session,
)
from tests.history_helpers import (
    _completed_run,
    _make_config,
    _session,
)

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore


@pytest.fixture
def storage(tmp_path: Path) -> ConversationStore:
    """Conversation storage for reading seen ids, which include the summary rows'."""
    return create_state_storage("test_agent", tmp_path / "seen", session_table="test_agent_sessions")


def _shared_session(*, is_team: bool) -> ConversationSession:
    if is_team:
        return ConversationSession(
            kind="team",
            session_id="session-1",
            owner_id="test_agent",
            user_id="@history-owner:localhost",
            runs=[],
            created_at=1,
            updated_at=1,
        )
    return ConversationSession(
        kind="agent",
        session_id="session-1",
        owner_id="test_agent",
        user_id="@history-owner:localhost",
        runs=[],
        created_at=1,
        updated_at=1,
    )


def test_scope_seen_event_ids_survive_scope_state_writes(tmp_path: Path, storage: ConversationStore) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    assert update_scope_seen_event_ids(session, scope, ["event-1"]) is True
    session.metadata = _with_scope_state(session.metadata, scope, HistoryScopeState(force_compact_before_next_run=True))

    assert read_scope_seen_event_ids(storage, session, scope) == {"event-1"}


def test_with_scope_state_updates_only_force_flag(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    forced = HistoryScopeState(force_compact_before_next_run=True)

    forced_metadata = _with_scope_state({}, scope, forced)

    assert read_scope_state(_session("session-1", metadata=forced_metadata), scope) == forced
    cleared_metadata = _with_scope_state(forced_metadata, scope, HistoryScopeState())
    assert read_scope_state(_session("session-1", metadata=cleared_metadata), scope) == HistoryScopeState()
    assert cleared_metadata == {}
    assert read_scope_state(_session("session-1", metadata=forced_metadata), scope) == forced


def test_scope_seen_event_ids_include_persisted_response_event_ids(tmp_path: Path, storage: ConversationStore) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    run = _completed_run("run-1")
    run.metadata = {
        "matrix_seen_event_ids": ["question-1"],
        "matrix_response_event_id": "answer-1",
    }
    session = _session("session-1", runs=[run])

    assert read_scope_seen_event_ids(storage, session, scope) == {"question-1", "answer-1"}


@pytest.mark.parametrize("is_team", [False, True], ids=["agent", "team"])
def test_seen_event_ids_match_model_history_visibility(storage: ConversationStore, *, is_team: bool) -> None:
    entity_id = "team-123" if is_team else "test_agent"
    scope = HistoryScope(kind="team" if is_team else "agent", scope_id=entity_id)

    def make_run(run_id: str, status: RunStatus, *, parent_run_id: str | None = None) -> RunRecord | TeamRunRecord:
        metadata = {"matrix_seen_event_ids": [f"{run_id}-event"]}
        if is_team:
            return TeamRunRecord(
                run_id=run_id,
                team_id=entity_id,
                status=status,
                parent_run_id=parent_run_id,
                metadata=metadata,
            )
        return RunRecord(
            run_id=run_id,
            agent_id=entity_id,
            status=status,
            parent_run_id=parent_run_id,
            metadata=metadata,
        )

    runs = [
        make_run("completed", RunStatus.completed),
        make_run("running", RunStatus.running),
        make_run("paused", RunStatus.paused),
        make_run("cancelled", RunStatus.cancelled),
        make_run("error", RunStatus.error),
        make_run("child", RunStatus.completed, parent_run_id="completed"),
    ]
    session: ConversationSession
    if is_team:
        session = ConversationSession(
            kind="team",
            session_id="session-1",
            owner_id=entity_id,
            runs=runs,
            created_at=1,
            updated_at=1,
        )
    else:
        session = ConversationSession(
            kind="agent",
            session_id="session-1",
            owner_id=entity_id,
            runs=runs,
            created_at=1,
            updated_at=1,
        )
    update_scope_seen_event_ids(session, scope, ["preserved-event"])

    assert read_scope_seen_event_ids(storage, session, scope) == {"completed-event", "preserved-event", "running-event"}
    assert [run.run_id for run in scope_visible_runs(session, scope)] == ["completed", "running"]


def test_scope_states_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    session.metadata = _with_scope_state(
        session.metadata,
        agent_scope,
        HistoryScopeState(force_compact_before_next_run=True),
    )

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is True
    assert read_scope_state(session, team_scope).force_compact_before_next_run is False


def test_queued_force_compaction_reads_the_session_row_without_its_runs(
    storage: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    assert has_queued_force_compaction(storage, "flagged", agent_scope) is False

    flagged = _session("flagged", runs=[_completed_run("run-1")])
    flagged.metadata = _with_scope_state(
        flagged.metadata,
        agent_scope,
        HistoryScopeState(force_compact_before_next_run=True),
    )
    pending = _session("pending", runs=[_completed_run("run-2")])
    pending.session_state = add_pending_force_compaction_scope(None, agent_scope)
    for session in (flagged, pending, _session("idle", runs=[_completed_run("run-3")])):
        seed_session(storage, session)
    statements: list[str] = []
    connect = store_module.connect_conversation_database

    def traced_connect(path: Path) -> sqlite3.Connection:
        connection = connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module, "connect_conversation_database", traced_connect)

    assert has_queued_force_compaction(storage, "flagged", agent_scope) is True
    assert has_queued_force_compaction(storage, "pending", agent_scope) is True
    assert has_queued_force_compaction(storage, "flagged", team_scope) is False
    assert has_queued_force_compaction(storage, "pending", team_scope) is False
    assert has_queued_force_compaction(storage, "idle", agent_scope) is False
    assert has_queued_force_compaction(storage, "missing", agent_scope) is False
    assert statements
    assert not any(storage.names.runs in statement for statement in statements)


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

    session.metadata = _with_scope_state(
        session.metadata,
        agent_scope,
        HistoryScopeState(force_compact_before_next_run=True),
    )

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


def test_scope_seen_event_ids_do_not_bleed_between_scopes(tmp_path: Path, storage: ConversationStore) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session(
        "session-1",
        runs=[
            RunRecord(
                run_id="agent-run",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["agent-event"]},
            ),
            TeamRunRecord(
                run_id="team-run",
                team_id="team-123",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["team-event"]},
            ),
        ],
    )
    update_scope_seen_event_ids(session, team_scope, ["preserved-team-event"])

    assert read_scope_seen_event_ids(storage, session, agent_scope) == {"agent-event"}
    assert read_scope_seen_event_ids(storage, session, team_scope) == {"team-event", "preserved-team-event"}
