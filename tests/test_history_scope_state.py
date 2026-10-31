"""Tests for history scope-state and seen-event-id storage."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import MINDROOM_HISTORY_METADATA_KEY
from mindroom.history.interrupted_replay import (
    build_interrupted_replay_snapshot,
    is_interrupted_replay_run,
    persist_interrupted_replay_snapshot,
)
from mindroom.history.replay import scope_visible_runs
from mindroom.history.storage import (
    has_queued_force_compaction,
    read_scope_seen_event_ids,
    read_scope_state,
    set_force_compaction,
    with_force_compaction,
)
from mindroom.history.types import (
    HistoryScope,
)
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.session_storage.agent_storage import create_agent_state_storage, create_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.conftest import (
    seed_session,
)
from tests.history_helpers import (
    _completed_run,
    _session,
    _team_session,
    trace_store_statements,
)

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore


@pytest.fixture
def storage(tmp_path: Path) -> ConversationStore:
    """The test agent's conversation storage."""
    return create_agent_state_storage("test_agent", tmp_path / "seen")


def test_the_force_flag_is_set_and_cleared_beside_the_seen_ids(storage: ConversationStore) -> None:
    """The owner's force flag and earlier releases' preserved seen ids share one value, and each field changes alone."""
    session = seed_session(
        storage,
        _session(
            "session-1",
            runs=[_completed_run("run-1")],
            metadata={MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": ["$kept"]}},
        ),
    )

    set_force_compaction(storage, session=session, forced=True)
    assert session.metadata == {
        MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": ["$kept"], "force_compact_before_next_run": True},
    }
    set_force_compaction(storage, session=session, forced=False)
    assert session.metadata == {MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": ["$kept"]}}
    assert with_force_compaction(None) == {MINDROOM_HISTORY_METADATA_KEY: {"force_compact_before_next_run": True}}


def test_clearing_the_force_flag_through_a_stale_session_lands_on_the_freshest_row(storage: ConversationStore) -> None:
    """The clear applies to the stored row's latest metadata, keeps what another writer stored, and syncs the session."""
    stale_session = seed_session(storage, _session("session-1", runs=[_completed_run("run-1")]))

    concurrent_session = storage.load_session("session-1")
    assert concurrent_session is not None
    storage.update_metadata(concurrent_session, lambda metadata: {**with_force_compaction(metadata), "other": "kept"})
    storage.save_run(concurrent_session, _completed_run("run-2"), record_usage=False)

    set_force_compaction(storage, session=stale_session, forced=False)

    persisted_session = storage.load_session("session-1")
    assert persisted_session is not None
    assert persisted_session.metadata == {"other": "kept"}
    assert stale_session.metadata == persisted_session.metadata
    assert [run.run_id for run in persisted_session.runs] == ["run-1", "run-2"]


@pytest.mark.parametrize("forced", [True, False], ids=["already-forced", "never-forced"])
def test_a_force_flag_update_that_changes_nothing_writes_nothing(
    storage: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    forced: bool,
) -> None:
    """Setting the flag to what the freshest row holds writes nothing, and the session then holds that row's metadata."""
    stale_session = seed_session(storage, _session("session-1", runs=[_completed_run("run-1")]))
    if forced:
        concurrent_session = storage.load_session("session-1")
        assert concurrent_session is not None
        storage.update_metadata(concurrent_session, with_force_compaction)
    statements = trace_store_statements(monkeypatch)

    set_force_compaction(storage, session=stale_session, forced=forced)

    assert read_scope_state(stale_session).force_compact_before_next_run is forced
    assert statements
    assert not [statement for statement in statements if statement.lstrip().upper().startswith("UPDATE")]
    persisted_session = storage.load_session("session-1")
    assert persisted_session is not None
    assert stale_session.metadata == persisted_session.metadata


def test_scope_seen_event_ids_include_persisted_response_event_ids(storage: ConversationStore) -> None:
    run = _completed_run("run-1")
    run.metadata = {
        "matrix_seen_event_ids": ["question-1"],
        "matrix_response_event_id": "answer-1",
    }
    session = _session("session-1", runs=[run])

    assert read_scope_seen_event_ids(storage, session) == {"question-1", "answer-1"}


@pytest.mark.parametrize("is_team", [False, True], ids=["agent", "team"])
def test_seen_event_ids_match_model_history_visibility(tmp_path: Path, *, is_team: bool) -> None:
    entity_id = "team-123" if is_team else "test_agent"
    scope = HistoryScope(kind="team" if is_team else "agent", scope_id=entity_id)
    storage = create_state_storage(entity_id, tmp_path / "seen", scope)

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
    session = ConversationSession(
        kind=scope.kind,
        session_id="session-1",
        owner_id=entity_id,
        runs=runs,
        # Seen ids earlier releases copied onto the session row; no run or summary row holds them.
        metadata={MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": ["preserved-event"]}},
        created_at=1,
    )

    assert read_scope_seen_event_ids(storage, session) == {"completed-event", "running-event"}
    assert [run.run_id for run in scope_visible_runs(session)] == ["completed", "running"]


def test_queued_force_compaction_reads_the_session_row_without_its_runs(
    storage: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert has_queued_force_compaction(storage, "flagged") is False

    flagged = _session("flagged", runs=[_completed_run("run-1")])
    flagged.metadata = with_force_compaction(flagged.metadata)
    for session in (flagged, _session("idle", runs=[_completed_run("run-2")])):
        seed_session(storage, session)
    statements = trace_store_statements(monkeypatch)

    assert has_queued_force_compaction(storage, "flagged") is True
    assert has_queued_force_compaction(storage, "idle") is False
    assert has_queued_force_compaction(storage, "missing") is False
    assert statements
    run_tables = (storage.names.log_runs, storage.names.log_messages)
    assert not any(table in statement for table in run_tables for statement in statements)


def test_scope_seen_event_ids_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    """A scope's seen ids leave out the runs of another scope with the same id."""
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    storage = create_state_storage("team_123", tmp_path / "seen", team_scope)
    session = _team_session(
        "session-1",
        team_id="team-123",
        runs=[
            RunRecord(
                run_id="agent-run",
                agent_id="team-123",
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

    assert read_scope_seen_event_ids(storage, replace(session, kind="agent")) == {"agent-event"}
    assert read_scope_seen_event_ids(storage, session) == {"team-event"}


def test_an_interrupted_record_over_a_completed_team_run_keeps_the_events_it_read(tmp_path: Path) -> None:
    """A team turn that failed after its terminal save keeps the events it read in the record that replaces its run."""
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    storage = create_state_storage("team_123", tmp_path / "seen", team_scope)
    metadata = {"matrix_event_id": "$question", "matrix_seen_event_ids": ["$question", "$context"]}
    completed = TeamRunRecord(run_id="team-run", team_id="team-123", status=RunStatus.completed, metadata=metadata)
    session = seed_session(storage, _team_session("session-1", team_id="team-123", runs=[completed]))

    persist_interrupted_replay_snapshot(
        storage=storage,
        session=session,
        run_id="team-run",
        snapshot=build_interrupted_replay_snapshot(
            user_message="Question",
            user_message_is_structured=False,
            partial_text="Partial answer",
            completed_tools=(),
            interrupted_tools=(),
            run_metadata=metadata,
        ),
    )

    stored = storage.load_session("session-1")
    assert stored is not None
    assert [(run.run_id, run.status, is_interrupted_replay_run(run)) for run in stored.runs] == [
        ("team-run", RunStatus.completed, True),
    ]
    assert read_scope_seen_event_ids(storage, stored) == {"$question", "$context"}
