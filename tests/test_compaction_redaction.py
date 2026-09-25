"""Redaction undoes compaction precisely instead of discarding the whole scope."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom import constants
from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.history import archive
from mindroom.history.storage import (
    archive_compaction_chunk,
    read_scope_seen_event_ids,
    reconcile_compaction_state,
    remove_redacted_event_from_compaction,
    update_scope_seen_event_ids,
)
from mindroom.history.types import HistoryScope
from tests.conftest import seed_session

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


def _run(run_id: str) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="code",
        session_id="session",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"],
        },
    )


def _seed(storage: SqliteDb, run_ids: list[str]) -> AgentSession:
    return seed_session(
        storage,
        AgentSession(session_id="session", agent_id="code", runs=[_run(run_id) for run_id in run_ids]),
    )


def _compact(storage: SqliteDb, session: AgentSession, run_ids: list[str], summary: str) -> None:
    archive_compaction_chunk(
        storage=storage,
        session=session,
        scope=_SCOPE,
        summary=SessionSummary(summary=summary, updated_at=datetime.now(UTC)),
        summary_model="summary-model",
        archived_runs=[run for run in session.runs or [] if run.run_id in run_ids],
    )


def _stored(storage: SqliteDb) -> AgentSession:
    session = get_agent_session(storage, "session")
    assert session is not None
    return session


def _summary(session: AgentSession) -> str | None:
    return session.summary.summary if session.summary is not None else None


def test_redaction_restores_the_runs_compacted_before_the_redacted_one(storage: SqliteDb) -> None:
    """Only the redacted run and what follows it leave history; earlier compacted runs return to replay."""
    session = _seed(storage, ["r1", "r2", "r3", "r4", "r5"])
    _compact(storage, session, ["r1"], "summary of r1")
    _compact(storage, session, ["r2", "r3"], "summary of r1 to r3")

    changed = remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$r3", removed_live_run=False)

    assert changed is True
    stored = _stored(storage)
    assert [run.run_id for run in stored.runs or []] == ["r2"]
    assert _summary(stored) == "summary of r1"
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["r1", "r2", "r3"]) == {"r1"}
    assert read_scope_seen_event_ids(stored, _SCOPE) == {"$r1", "$r2"}
    assert _summary(session) == "summary of r1"


def test_redacting_the_first_compacted_run_clears_the_summary_for_good(storage: SqliteDb) -> None:
    """A rollback past every generation keeps a stale summary write from being adopted as legacy history."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary with the redacted content")

    assert (
        remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$r1", removed_live_run=False) is True
    )
    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None

    stored.summary = SessionSummary(summary="summary with the redacted content")
    storage.upsert_session(stored)
    reconcile_compaction_state(storage, stored, _SCOPE)

    assert _stored(storage).summary is None


def test_redacting_a_live_only_event_keeps_the_archive_summary(storage: SqliteDb) -> None:
    """A summary provably built without the event survives its redaction."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary of r1")
    update_scope_seen_event_ids(session, _SCOPE, ["$team-consumed"])
    storage.upsert_session(session)

    assert remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$r2", removed_live_run=True)

    stored = _stored(storage)
    assert _summary(stored) == "summary of r1"
    assert [run.run_id for run in stored.runs or []] == ["r2"]
    # Preserved ids now name exactly the compacted history, so removed messages count as unseen again.
    assert read_scope_seen_event_ids(stored, _SCOPE) == {"$r1", "$r2"}


def test_redaction_without_any_matching_history_changes_nothing(storage: SqliteDb) -> None:
    """An event no stored history represents leaves compaction untouched."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary of r1")

    changed = remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$other", removed_live_run=False)

    assert changed is False
    assert _summary(_stored(storage)) == "summary of r1"


def test_redacting_legacy_provenance_clears_summary_and_archived_generations(storage: SqliteDb) -> None:
    """Content-free legacy history cannot be split, so an event it consumed clears the scope."""
    session = AgentSession(
        session_id="session",
        agent_id="code",
        runs=[_run("r2"), _run("r3")],
        summary=SessionSummary(summary="legacy summary"),
    )
    update_scope_seen_event_ids(session, _SCOPE, ["$legacy"])
    seed_session(storage, session)
    reconcile_compaction_state(storage, session, _SCOPE)
    _compact(storage, session, ["r2"], "legacy summary and r2")

    assert (
        remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$legacy", removed_live_run=False)
        is True
    )

    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None
    assert read_scope_seen_event_ids(stored, _SCOPE) == set()
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["r2"]) == set()


def test_removing_a_live_run_retires_a_legacy_summary(storage: SqliteDb) -> None:
    """Legacy summaries lack complete provenance, so any live removal for the event retires them."""
    session = seed_session(
        storage,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[_run("r2")],
            summary=SessionSummary(summary="legacy summary"),
        ),
    )

    assert remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$gone", removed_live_run=True)

    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None


def test_team_redaction_without_compaction_keeps_unrelated_runs(storage: SqliteDb) -> None:
    """Seen ids a team records on the session row are not legacy provenance."""
    session = _seed(storage, ["t1", "t2"])
    update_scope_seen_event_ids(session, _SCOPE, ["$t1", "$t2", "$t3"])
    storage.upsert_session(session)
    reconcile_compaction_state(storage, session, _SCOPE)

    assert remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$t3", removed_live_run=True)

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs or []] == ["t1", "t2"]
    assert read_scope_seen_event_ids(stored, _SCOPE) == {"$t1", "$t2"}


def test_rollback_forgets_seen_ids_of_the_removed_runs(storage: SqliteDb) -> None:
    """Messages whose runs a rollback removed become unseen, so thread history supplies them again."""
    session = _seed(storage, ["r1", "r2", "r3"])
    _compact(storage, session, ["r1"], "summary of r1")
    _compact(storage, session, ["r2"], "summary of r1 and r2")
    update_scope_seen_event_ids(session, _SCOPE, ["$r3"])
    storage.upsert_session(session)

    assert remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$r2", removed_live_run=False)

    stored = _stored(storage)
    assert stored.runs == []
    assert _summary(stored) == "summary of r1"
    assert read_scope_seen_event_ids(stored, _SCOPE) == {"$r1"}


def test_legacy_provenance_wins_over_a_later_archive_hit(storage: SqliteDb) -> None:
    """An event a legacy summary may contain clears it even when an archived run also represents it."""
    session = AgentSession(
        session_id="session",
        agent_id="code",
        runs=[_run("r1"), _run("r2")],
        summary=SessionSummary(summary="legacy summary quoting $r1"),
    )
    update_scope_seen_event_ids(session, _SCOPE, ["$r1"])
    seed_session(storage, session)
    reconcile_compaction_state(storage, session, _SCOPE)
    _compact(storage, session, ["r1"], "legacy summary quoting $r1, then r1")

    assert remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$r1", removed_live_run=False)

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []


def test_a_rearchived_run_keeps_its_first_generation(storage: SqliteDb) -> None:
    """Redaction rolls back to the generation that first folded a run, even if it was archived again."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary mentioning r1")
    resurrected = seed_session(
        storage,
        AgentSession(session_id="session", agent_id="code", runs=[_run("r1"), *(_stored(storage).runs or [])]),
    )
    _compact(storage, resurrected, ["r1", "r2"], "summary mentioning r1 and r2")

    assert remove_redacted_event_from_compaction(storage, resurrected, _SCOPE, event_id="$r1", removed_live_run=False)

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []


def test_repairing_a_stale_summary_also_resets_seen_ids(storage: SqliteDb) -> None:
    """A stale pre-rollback row cannot mark the rolled-back messages as seen again."""
    session = _seed(storage, ["r1", "r2", "r3"])
    _compact(storage, session, ["r1"], "summary of r1")
    _compact(storage, session, ["r2"], "summary of r1 and r2")
    stale = _stored(storage)
    update_scope_seen_event_ids(stale, _SCOPE, ["$r3"])
    assert remove_redacted_event_from_compaction(storage, session, _SCOPE, event_id="$r2", removed_live_run=False)

    storage.upsert_session(stale)
    reconciled = _stored(storage)
    reconcile_compaction_state(storage, reconciled, _SCOPE)

    stored = _stored(storage)
    assert _summary(stored) == "summary of r1"
    assert read_scope_seen_event_ids(stored, _SCOPE) == {"$r1"}
