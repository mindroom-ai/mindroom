"""History compacted before the archive existed becomes summary rows once, when its conversation database opens."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY
from mindroom.history import summary_rows
from mindroom.history.legacy_compaction_state import _legacy_tombstones
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage, get_agent_session, get_team_session
from tests.conftest import seed_session
from tests.history_helpers import StoredSummaryRow, stored_run_ids, summary_rows_of

if TYPE_CHECKING:
    from pathlib import Path

_SCOPE = HistoryScope(kind="agent", scope_id="code")


def _open(tmp_path: Path) -> SqliteDb:
    db = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    assert isinstance(db, SqliteDb)
    return db


def _as_pre_archive(tmp_path: Path) -> None:
    """Drop the summary-row tables seeding created, since releases before the archive never had them."""
    with sqlite3.connect(tmp_path / "sessions" / "code.db") as connection:
        connection.execute("DROP TABLE IF EXISTS code_sessions_summaries")
        connection.execute("DROP TABLE IF EXISTS code_sessions_removed_runs")


def _migrated(tmp_path: Path, session: AgentSession | TeamSession) -> SqliteDb:
    """Seed ``session`` the way an older release left it, then reopen so the migration runs."""
    seeding = _open(tmp_path)
    seed_session(seeding, session)
    seeding.close()
    _as_pre_archive(tmp_path)
    return _open(tmp_path)


def _compaction_metadata(scope_key: str, raw_state: dict[str, object]) -> dict[str, object]:
    return {MINDROOM_COMPACTION_METADATA_KEY: {"version": 2, "states": {scope_key: raw_state}}}


def _seen_metadata(scope_key: str, event_ids: list[str]) -> dict[str, object]:
    return {MINDROOM_MATRIX_HISTORY_METADATA_KEY: {"version": 1, "states": {scope_key: {"seen_event_ids": event_ids}}}}


def test_legacy_tombstones_recognize_only_the_destructive_compactor() -> None:
    """Only states carrying a retired compactor key are legacy."""
    assert _legacy_tombstones({"force_compact_before_next_run": True}) is None
    assert _legacy_tombstones({"last_compacted_at": "2026-09-01T00:00:00Z"}) == ()
    assert _legacy_tombstones({"compacted_run_ids": ["a", "", 3, "b", "a"]}) == ("a", "b")


def test_opening_storage_adopts_legacy_summary_tombstones_and_seen_ids_once(tmp_path: Path) -> None:
    """The summary keeps replaying, its seen ids move into a legacy summary row, tombstoned copies go, and so do the retired keys."""
    storage = _migrated(
        tmp_path,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[RunOutput(run_id="gone", agent_id="code"), RunOutput(run_id="kept", agent_id="code")],
            summary=SessionSummary(summary="legacy summary"),
            metadata={
                **_compaction_metadata(
                    _SCOPE.key,
                    {
                        "compacted_run_ids": ["gone"],
                        "last_compacted_at": "2026-09-01T00:00:00Z",
                        "last_summary_model": "old-model",
                        "last_compacted_run_count": 1,
                        "force_compact_before_next_run": True,
                    },
                ),
                **_seen_metadata(_SCOPE.key, ["$legacy"]),
            },
        ),
    )
    storage.close()
    storage = _open(tmp_path)
    try:
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == _compaction_metadata(_SCOPE.key, {"force_compact_before_next_run": True})
        assert stored.summary is not None
        assert stored.summary.summary == "legacy summary"
        assert summary_rows_of(storage, _SCOPE.key, "session") == [
            StoredSummaryRow(summary="legacy summary", summary_model=None, legacy=True),
        ]
        assert summary_rows.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE.key) == {"$legacy"}
        assert stored_run_ids(storage, "session") == ["kept"]
        assert [run.run_id for run in stored.runs or []] == ["kept"]
    finally:
        storage.close()


def test_opening_storage_adopts_a_summary_without_compaction_state_under_the_row_scope(tmp_path: Path) -> None:
    """A replayed summary with no retired keys belongs to the scope that owns the session row."""
    team_scope = HistoryScope(kind="team", scope_id="squad")
    storage = _migrated(
        tmp_path,
        TeamSession(session_id="session", team_id="squad", summary=SessionSummary(summary="team summary")),
    )
    try:
        assert summary_rows_of(storage, team_scope.key, "session") == [
            StoredSummaryRow(summary="team summary", summary_model=None, legacy=True),
        ]
        stored = get_team_session(storage, "session")
        assert stored is not None
        assert stored.summary is not None
        assert stored.summary.summary == "team summary"
    finally:
        storage.close()


def test_opening_storage_leaves_uncompacted_scopes_and_their_seen_ids_alone(tmp_path: Path) -> None:
    """Seen ids a team recorded without compaction stay in session metadata."""
    metadata = _seen_metadata(_SCOPE.key, ["$consumed"])
    storage = _migrated(
        tmp_path,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[RunOutput(run_id="kept", agent_id="code")],
            metadata=metadata,
        ),
    )
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == []
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == metadata
    finally:
        storage.close()


def test_opening_storage_ignores_malformed_legacy_metadata(tmp_path: Path) -> None:
    """Unreadable metadata is left as it is instead of failing the conversation database open."""
    seeding = _open(tmp_path)
    seed_session(seeding, AgentSession(session_id="session", agent_id="code"))
    seeding.close()
    _as_pre_archive(tmp_path)
    connection = sqlite3.connect(tmp_path / "sessions" / "code.db")
    try:
        connection.execute("UPDATE code_sessions SET metadata = 'not json' WHERE session_id = 'session'")
        connection.commit()
    finally:
        connection.close()

    storage = _open(tmp_path)
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == []
    finally:
        storage.close()
    connection = sqlite3.connect(tmp_path / "sessions" / "code.db")
    try:
        assert connection.execute("SELECT metadata FROM code_sessions").fetchone() == ("not json",)
    finally:
        connection.close()


def test_opening_storage_keeps_seen_ids_of_a_scope_without_a_summary(tmp_path: Path) -> None:
    """Tombstones alone carry no replayed summary, so no row is written and the scope's seen ids stay in metadata."""
    seen = _seen_metadata(_SCOPE.key, ["$consumed"])
    storage = _migrated(
        tmp_path,
        AgentSession(
            session_id="session",
            agent_id="code",
            metadata={**_compaction_metadata(_SCOPE.key, {"compacted_run_ids": ["gone"]}), **seen},
        ),
    )
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == []
        assert summary_rows.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE.key) is None
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == seen
    finally:
        storage.close()


def test_opening_storage_reads_double_encoded_legacy_metadata(tmp_path: Path) -> None:
    """Metadata an older Agno stored as a JSON string of JSON is adopted like plain JSON."""
    seeding = _open(tmp_path)
    seed_session(
        seeding,
        AgentSession(session_id="session", agent_id="code", summary=SessionSummary(summary="legacy summary")),
    )
    seeding.close()
    _as_pre_archive(tmp_path)
    metadata = {
        **_compaction_metadata(_SCOPE.key, {"compacted_run_ids": ["gone"]}),
        **_seen_metadata(_SCOPE.key, ["$legacy"]),
    }
    connection = sqlite3.connect(tmp_path / "sessions" / "code.db")
    try:
        connection.execute(
            "UPDATE code_sessions SET metadata = ? WHERE session_id = 'session'",
            (json.dumps(json.dumps(metadata)),),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _open(tmp_path)
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == [
            StoredSummaryRow(summary="legacy summary", summary_model=None, legacy=True),
        ]
        assert summary_rows.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE.key) == {"$legacy"}
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == {}
    finally:
        storage.close()


def test_opening_storage_adopts_a_team_row_with_tombstones(tmp_path: Path) -> None:
    """A team scope's tombstones and summary are adopted under the team scope."""
    team_scope = HistoryScope(kind="team", scope_id="squad")
    storage = _migrated(
        tmp_path,
        TeamSession(
            session_id="session",
            team_id="squad",
            summary=SessionSummary(summary="team summary"),
            metadata=_compaction_metadata(team_scope.key, {"compacted_run_ids": ["gone"]}),
        ),
    )
    try:
        assert summary_rows_of(storage, team_scope.key, "session") == [
            StoredSummaryRow(summary="team summary", summary_model=None, legacy=True),
        ]
        stored = get_team_session(storage, "session")
        assert stored is not None
        assert stored.metadata == {}
    finally:
        storage.close()
