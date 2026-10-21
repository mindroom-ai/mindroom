"""Redaction undoes compaction precisely instead of discarding the whole scope."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom import constants
from mindroom.history.storage import (
    _read_scope_history_event_ids,
    _remove_redacted_event_from_history,
    read_scope_seen_event_ids,
    update_scope_seen_event_ids,
)
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage
from tests.conftest import seed_session
from tests.history_helpers import compact_runs, folded_run_ids, stored_run_ids, summary_rows_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_SCOPE = HistoryScope(kind="agent", scope_id="code")


def _open(tmp_path: Path) -> SqliteDb:
    db = create_state_storage("code", tmp_path, session_table="code_sessions")
    assert isinstance(db, SqliteDb)
    return db


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[SqliteDb]:
    """Use the production conversation storage owner."""
    db = _open(tmp_path)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def open_legacy(tmp_path: Path) -> Iterator[Callable[[AgentSession], SqliteDb]]:
    """Seed a session as a pre-archive release left it, then reopen so the migration adopts it."""
    opened: list[SqliteDb] = []

    def open_migrated(session: AgentSession) -> SqliteDb:
        seeding = _open(tmp_path)
        seed_session(seeding, session)
        seeding.close()
        # A pre-archive release never had summary-row tables, so the next open adopts its state.
        with sqlite3.connect(tmp_path / "sessions" / "code.db") as connection:
            connection.execute("DROP TABLE code_sessions_summaries")
            connection.execute("DROP TABLE code_sessions_removed_runs")
        opened.append(_open(tmp_path))
        return opened[-1]

    yield open_migrated
    for db in opened:
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
    compact_runs(storage, session, _SCOPE, run_ids, summary)


def _stored(storage: SqliteDb) -> AgentSession:
    session = storage.load_session("session", "agent")
    assert session is not None
    return session


def _summary(session: AgentSession) -> str | None:
    return session.summary.summary if session.summary is not None else None


def _seen(storage: SqliteDb, session: AgentSession) -> set[str]:
    return read_scope_seen_event_ids(storage, session, _SCOPE)


def _redact(storage: SqliteDb, session: AgentSession, event_id: str) -> bool:
    return _remove_redacted_event_from_history(storage, session, _SCOPE, event_id=event_id)


def _legacy_session(run_ids: list[str], summary: str, seen_event_ids: list[str]) -> AgentSession:
    session = AgentSession(
        session_id="session",
        agent_id="code",
        runs=[_run(run_id) for run_id in run_ids],
        summary=SessionSummary(summary=summary),
        metadata={
            constants.MINDROOM_COMPACTION_METADATA_KEY: {
                "version": 2,
                "states": {_SCOPE.key: {"compacted_run_ids": ["legacy-gone"]}},
            },
        },
    )
    update_scope_seen_event_ids(session, _SCOPE, seen_event_ids)
    return session


def test_a_compacted_run_counts_as_seen_what_it_counted_while_loaded(storage: SqliteDb) -> None:
    """Compaction changes where seen ids come from, not which ids a run marks seen; redaction still matches all."""
    run = _run("r1")
    assert run.metadata is not None
    run.metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] = ["$coalesced"]
    session = seed_session(storage, AgentSession(session_id="session", agent_id="code", runs=[run, _run("r2")]))
    seen_while_loaded = _seen(storage, _stored(storage))
    _compact(storage, session, ["r1"], "summary of r1")

    assert _seen(storage, _stored(storage)) == seen_while_loaded == {"$r1", "$r2"}
    assert _redact(storage, _stored(storage), "$coalesced")
    assert _summary(_stored(storage)) is None


def test_compaction_derives_seen_ids_without_storing_them(storage: SqliteDb) -> None:
    """Compacted runs count as seen through their summary row, so session metadata keeps no copy to repair."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary of r1")

    stored = _stored(storage)
    assert constants.MINDROOM_MATRIX_HISTORY_METADATA_KEY not in (stored.metadata or {})
    assert _seen(storage, stored) == {"$r1", "$r2"}


def test_redaction_restores_the_runs_compacted_before_the_redacted_one(storage: SqliteDb) -> None:
    """Only the redacted run and what follows it leave history; earlier folded runs return to replay."""
    session = _seed(storage, ["r1", "r2", "r3", "r4", "r5"])
    _compact(storage, session, ["r1"], "summary of r1")
    _compact(storage, session, ["r2", "r3"], "summary of r1 to r3")

    assert _redact(storage, session, "$r3") is True

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs or []] == ["r2"]
    assert _summary(stored) == "summary of r1"
    assert stored_run_ids(storage, "session") == ["r1", "r2"]
    assert _seen(storage, stored) == {"$r1", "$r2"}
    assert _summary(session) == "summary of r1"


def test_redacting_the_first_compacted_run_clears_the_summary_for_good(storage: SqliteDb) -> None:
    """A cut past every summary row leaves no summary, and a stale session-row summary never replays."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary with the redacted content")

    assert _redact(storage, session, "$r1") is True
    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None

    stored.summary = SessionSummary(summary="summary with the redacted content")
    storage.upsert_session(stored)

    assert _stored(storage).summary is None


def test_redacting_a_loaded_only_event_keeps_the_summary(storage: SqliteDb) -> None:
    """A summary provably built without the event survives its redaction."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary of r1")
    update_scope_seen_event_ids(session, _SCOPE, ["$team-consumed"])
    storage.upsert_session(session)

    assert _redact(storage, session, "$r2")

    stored = _stored(storage)
    assert _summary(stored) == "summary of r1"
    assert stored.runs == []
    # Preserved ids are dropped; the summary rows and loaded runs still supply everything replay represents.
    assert _seen(storage, stored) == {"$r1"}


def test_compacted_history_that_read_a_redacted_event_is_found_and_removed(storage: SqliteDb) -> None:
    """A message a compacted run read only as context still marks that history, and its removal syncs the session."""
    run = _run("r1")
    assert run.metadata is not None
    run.metadata[constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY] = ["$context", "$r1"]
    session = seed_session(storage, AgentSession(session_id="session", agent_id="code", runs=[run, _run("r2")]))
    _compact(storage, session, ["r1"], "summary that quotes the context")
    loaded = _stored(storage)

    assert "$context" in _read_scope_history_event_ids(storage, loaded, _SCOPE)
    assert _redact(storage, loaded, "$context") is True

    assert loaded.runs == []
    assert loaded.summary is None
    assert "$context" not in _read_scope_history_event_ids(storage, _stored(storage), _SCOPE)


def test_history_event_ids_include_compacted_sources_and_paused_runs(storage: SqliteDb) -> None:
    """Redaction finds a coalesced source compaction folded and a run paused for approval."""
    coalesced = _run("r1")
    assert coalesced.metadata is not None
    coalesced.metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] = ["$coalesced"]
    paused = _run("r3")
    paused.status = RunStatus.paused
    session = seed_session(
        storage,
        AgentSession(session_id="session", agent_id="code", runs=[coalesced, _run("r2"), paused]),
    )
    _compact(storage, session, ["r1"], "summary of r1")

    event_ids = _read_scope_history_event_ids(storage, _stored(storage), _SCOPE)

    assert {"$coalesced", "$r1", "$r2", "$r3"} <= event_ids
    assert "$coalesced" not in read_scope_seen_event_ids(storage, _stored(storage), _SCOPE)


def test_redaction_without_any_matching_history_changes_nothing(storage: SqliteDb) -> None:
    """An event no stored history represents leaves compaction untouched."""
    session = _seed(storage, ["r1", "r2"])
    _compact(storage, session, ["r1"], "summary of r1")

    assert _redact(storage, session, "$other") is False
    assert _summary(_stored(storage)) == "summary of r1"


def test_redacting_legacy_provenance_clears_summary_and_folded_runs(
    open_legacy: Callable[[AgentSession], SqliteDb],
) -> None:
    """Content-free legacy history cannot be split, so an event it consumed clears the scope."""
    storage = open_legacy(_legacy_session(["r2", "r3"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    _compact(storage, session, ["r2"], "legacy summary and r2")

    assert _redact(storage, session, "$legacy") is True

    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None
    assert _seen(storage, stored) == set()
    assert stored_run_ids(storage, "session") == []
    assert summary_rows_of(storage, _SCOPE.key, "session") == []


def test_removing_a_loaded_run_retires_a_legacy_summary(open_legacy: Callable[[AgentSession], SqliteDb]) -> None:
    """Legacy summaries lack complete provenance, so removing a loaded run for any event retires them."""
    storage = open_legacy(_legacy_session(["r2", "r3"], "legacy summary", []))
    session = _stored(storage)

    assert _redact(storage, session, "$r3")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs or []] == ["r2"]
    assert stored.summary is None


def test_removing_a_loaded_run_keeps_runs_folded_after_a_legacy_summary(
    open_legacy: Callable[[AgentSession], SqliteDb],
) -> None:
    """Retiring a legacy summary keeps later folded runs stored, but no longer counts or replays them."""
    storage = open_legacy(_legacy_session(["r1", "r2", "r3"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    _compact(storage, session, ["r1"], "legacy summary and r1")

    assert _redact(storage, session, "$r3")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs or []] == ["r2"]
    assert stored.summary is None
    assert "r1" in stored_run_ids(storage, "session")
    assert _seen(storage, stored) == {"$r2"}
    stored.summary = SessionSummary(summary="legacy summary and r1")
    storage.upsert_session(stored)
    assert _stored(storage).summary is None


def test_retirement_survives_a_later_cut(open_legacy: Callable[[AgentSession], SqliteDb]) -> None:
    """A later cut before the retirement barrier brings back neither the retired summaries nor the runs behind it."""
    cancelled = _run("c1")
    cancelled.status = RunStatus.cancelled
    assert cancelled.metadata is not None
    cancelled.metadata[constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY] = ["$c1", "$late-redaction"]
    legacy = _legacy_session(["r1", "r2", "r3"], "legacy summary", ["$legacy"])
    first, *rest = legacy.runs or []
    legacy.runs = [first, cancelled, *rest]
    storage = open_legacy(legacy)
    session = _stored(storage)
    _compact(storage, session, ["r1"], "legacy summary and r1")
    assert _redact(storage, session, "$r3")

    # The cancelled run sorts after r1 and before the barrier the retirement appended.
    assert _redact(storage, _stored(storage), "$late-redaction")

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []
    assert "r1" in stored_run_ids(storage, "session")


def test_removing_only_folded_runs_retires_a_legacy_summary(open_legacy: Callable[[AgentSession], SqliteDb]) -> None:
    """A legacy summary may hold the event without listing it, so a cut of folded runs alone retires it too."""
    storage = open_legacy(_legacy_session(["r1", "r2", "r3"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    _compact(storage, session, ["r1", "r2"], "legacy summary and r1 to r2")

    assert _redact(storage, session, "$r2")

    stored = _stored(storage)
    assert stored.summary is None
    assert [run.run_id for run in stored.runs or []] == ["r1"]


def test_a_late_save_of_a_redacted_run_cannot_replay_it(storage: SqliteDb) -> None:
    """A stale copy of a run redaction removed is never written back."""
    session = _seed(storage, ["r1", "r2"])
    late_copy = next(run for run in session.runs or [] if run.run_id == "r1")
    _compact(storage, session, ["r1"], "summary of r1")
    assert _redact(storage, session, "$r1")

    storage.upsert_run(run=late_copy, session_id="session", user_id=None)

    assert _stored(storage).runs == []
    assert stored_run_ids(storage, "session") == []


def test_a_late_save_after_a_legacy_clear_cannot_replay_the_cleared_run(
    open_legacy: Callable[[AgentSession], SqliteDb],
) -> None:
    """Runs folded after a legacy summary stay gone when that summary is cleared."""
    storage = open_legacy(_legacy_session(["r1", "r2"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    late_copy = next(run for run in session.runs or [] if run.run_id == "r1")
    _compact(storage, session, ["r1"], "legacy summary and r1")
    assert _redact(storage, session, "$legacy")

    storage.upsert_run(run=late_copy, session_id="session", user_id=None)

    assert _stored(storage).runs == []


def test_team_redaction_without_compaction_keeps_unrelated_runs(storage: SqliteDb) -> None:
    """Seen ids a team records on the session row are not legacy provenance."""
    session = _seed(storage, ["t1", "t2", "t3"])
    update_scope_seen_event_ids(session, _SCOPE, ["$t1", "$t2", "$t3"])
    storage.upsert_session(session)

    assert _redact(storage, session, "$t3")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs or []] == ["t1", "t2"]
    assert _seen(storage, stored) == {"$t1", "$t2"}


def test_rollback_forgets_seen_ids_of_the_removed_runs(storage: SqliteDb) -> None:
    """Messages whose runs a cut removed become unseen, so thread history supplies them again."""
    session = _seed(storage, ["r1", "r2", "r3"])
    _compact(storage, session, ["r1"], "summary of r1")
    _compact(storage, session, ["r2"], "summary of r1 and r2")
    update_scope_seen_event_ids(session, _SCOPE, ["$r3"])
    storage.upsert_session(session)

    assert _redact(storage, session, "$r2")

    stored = _stored(storage)
    assert stored.runs == []
    assert _summary(stored) == "summary of r1"
    assert _seen(storage, stored) == {"$r1"}


def test_legacy_provenance_wins_over_a_later_folded_hit(open_legacy: Callable[[AgentSession], SqliteDb]) -> None:
    """An event a legacy summary may contain clears it even when a folded run also holds it."""
    storage = open_legacy(_legacy_session(["r1", "r2"], "legacy summary quoting $r1", ["$r1"]))
    session = _stored(storage)
    _compact(storage, session, ["r1"], "legacy summary quoting $r1, then r1")

    assert _redact(storage, session, "$r1")

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []


def test_a_stale_copy_of_a_folded_run_stays_folded(storage: SqliteDb) -> None:
    """A late save of a folded run is refused, so it neither replays again nor hides its first summary."""
    session = _seed(storage, ["r1", "r2"])
    stale = next(run for run in session.runs or [] if run.run_id == "r1")
    _compact(storage, session, ["r1"], "summary mentioning r1")
    storage.upsert_run(run=stale, session_id="session", user_id=None)
    assert folded_run_ids(storage, "session") == ["r1"]
    _compact(storage, _stored(storage), ["r2"], "summary mentioning r1 and r2")

    assert _redact(storage, _stored(storage), "$r1")

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []


def test_a_stale_session_row_keeps_the_rolled_back_seen_ids_out(storage: SqliteDb) -> None:
    """A stale pre-cut session row cannot mark rolled-back messages as seen through the summary rows."""
    session = _seed(storage, ["r1", "r2", "r3"])
    _compact(storage, session, ["r1"], "summary of r1")
    _compact(storage, session, ["r2"], "summary of r1 and r2")
    stale = _stored(storage)
    assert _redact(storage, session, "$r2")

    storage.upsert_session(stale)

    stored = _stored(storage)
    assert _summary(stored) == "summary of r1"
    assert _seen(storage, stored) == {"$r1"}
