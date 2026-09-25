"""History compacted before the archive existed is adopted once as a content-free generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY
from mindroom.history import archive
from mindroom.history.legacy_compaction_state import _legacy_tombstones
from mindroom.history.storage import archive_compaction_chunk, read_scope_state, reconcile_compaction_state
from mindroom.history.types import HistoryScope, HistoryScopeState
from tests.conftest import seed_session
from tests.history_helpers import StoredGeneration, compaction_generations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SCOPE = HistoryScope(kind="agent", scope_id="code")


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[SqliteDb]:
    """Use the production conversation storage owner."""
    db = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    assert isinstance(db, SqliteDb)
    try:
        yield db
    finally:
        db.close()


def _legacy_metadata(raw_state: dict[str, object]) -> dict[str, object]:
    return {MINDROOM_COMPACTION_METADATA_KEY: {"version": 2, "states": {_SCOPE.key: raw_state}}}


def test_legacy_tombstones_recognize_only_the_destructive_compactor() -> None:
    """Only states carrying a retired compactor key are legacy."""
    assert _legacy_tombstones(None) is None
    assert _legacy_tombstones({"force_compact_before_next_run": True}) is None
    assert _legacy_tombstones({"last_compacted_at": "2026-09-01T00:00:00Z"}) == ()
    assert _legacy_tombstones({"compacted_run_ids": ["a", "", 3, "b", "a"]}) == ("a", "b")


def test_reconcile_adopts_legacy_summary_and_tombstones_once(storage: SqliteDb) -> None:
    """The summary keeps replaying, tombstones keep pruning, and the legacy keys are dropped."""
    session = seed_session(
        storage,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[RunOutput(run_id="gone", agent_id="code"), RunOutput(run_id="kept", agent_id="code")],
            summary=SessionSummary(summary="legacy summary"),
            metadata=_legacy_metadata(
                {
                    "compacted_run_ids": ["gone"],
                    "last_compacted_at": "2026-09-01T00:00:00Z",
                    "last_summary_model": "old-model",
                    "last_compacted_run_count": 1,
                    "force_compact_before_next_run": True,
                },
            ),
        ),
    )

    reconcile_compaction_state(storage, session, _SCOPE)
    reconcile_compaction_state(storage, session, _SCOPE)

    stored = get_agent_session(storage, "session")
    assert stored is not None
    assert [run.run_id for run in stored.runs or []] == ["kept"]
    assert stored.summary is not None
    assert stored.summary.summary == "legacy summary"
    assert stored.metadata == _legacy_metadata({"force_compact_before_next_run": True})
    assert read_scope_state(stored, _SCOPE) == HistoryScopeState(force_compact_before_next_run=True)
    assert compaction_generations(storage, _SCOPE.key, "session") == [
        StoredGeneration(summary="legacy summary", summary_model=None, legacy=True),
    ]
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["gone", "kept"]) == {"gone"}


def test_reconcile_adopts_a_summary_without_compaction_state(storage: SqliteDb) -> None:
    """A replayed summary from before versioned compaction state is kept as legacy history."""
    session = seed_session(
        storage,
        AgentSession(session_id="session", agent_id="code", summary=SessionSummary(summary="v1 summary")),
    )

    reconcile_compaction_state(storage, session, _SCOPE)

    assert compaction_generations(storage, _SCOPE.key, "session") == [
        StoredGeneration(summary="v1 summary", summary_model=None, legacy=True),
    ]
    stored = get_agent_session(storage, "session")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == "v1 summary"


def test_reconcile_leaves_uncompacted_scopes_untouched(storage: SqliteDb) -> None:
    """A scope that never compacted gets no generation."""
    session = seed_session(
        storage,
        AgentSession(session_id="session", agent_id="code", runs=[RunOutput(run_id="kept", agent_id="code")]),
    )

    reconcile_compaction_state(storage, session, _SCOPE)

    assert archive.latest_generation(storage, session_id="session", scope_key=_SCOPE.key) is None


def _compact(storage: SqliteDb, session: AgentSession, run_ids: list[str], summary: str) -> None:
    archive_compaction_chunk(
        storage=storage,
        session=session,
        scope=_SCOPE,
        summary=SessionSummary(summary=summary),
        summary_model="summary-model",
        archived_runs=[run for run in session.runs or [] if run.run_id in run_ids],
    )


def _legacy_session(summary: str, tombstones: list[str], runs: list[str]) -> AgentSession:
    return AgentSession(
        session_id="session",
        agent_id="code",
        runs=[RunOutput(run_id=run_id, agent_id="code") for run_id in runs],
        summary=SessionSummary(summary=summary),
        metadata=_legacy_metadata({"compacted_run_ids": tombstones}),
    )


def test_a_stale_pre_adoption_snapshot_does_not_replace_the_archive_summary(storage: SqliteDb) -> None:
    """Retired keys written back by a stale snapshot are dropped instead of adopted again."""
    session = seed_session(storage, _legacy_session("legacy summary", ["gone"], ["r1", "r2"]))
    reconcile_compaction_state(storage, session, _SCOPE)
    _compact(storage, session, ["r1"], "legacy summary and r1")

    stale = _legacy_session("legacy summary", ["gone"], [])
    storage.upsert_session(stale)
    reconciled = get_agent_session(storage, "session")
    assert reconciled is not None
    reconcile_compaction_state(storage, reconciled, _SCOPE)

    stored = get_agent_session(storage, "session")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == "legacy summary and r1"
    assert read_scope_state(stored, _SCOPE) == HistoryScopeState()
    assert [generation.summary for generation in compaction_generations(storage, _SCOPE.key, "session")] == [
        "legacy summary",
        "legacy summary and r1",
    ]


def test_adoption_keeps_a_concurrent_write_to_the_session_row(storage: SqliteDb) -> None:
    """Dropping the retired keys rewrites the freshest row instead of the caller's snapshot."""
    seed_session(storage, _legacy_session("legacy summary", ["gone"], ["r1"]))
    snapshot = get_agent_session(storage, "session")
    assert snapshot is not None
    concurrent = get_agent_session(storage, "session")
    assert concurrent is not None
    concurrent.session_data = {"session_state": {"written": "concurrently"}}
    storage.upsert_session(concurrent)

    reconcile_compaction_state(storage, snapshot, _SCOPE)

    stored = get_agent_session(storage, "session")
    assert stored is not None
    assert stored.session_data == {"session_state": {"written": "concurrently"}}
    assert read_scope_state(stored, _SCOPE) == HistoryScopeState()
