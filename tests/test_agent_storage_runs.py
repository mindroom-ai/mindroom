"""Runs-table behavior of MindRoom's SQLite session adapter under Agno 3."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_storage import create_state_storage, get_agent_session, get_team_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

_LEGACY_SESSION_COLUMNS = (
    "session_id TEXT PRIMARY KEY, session_type TEXT NOT NULL, agent_id TEXT, team_id TEXT, workflow_id TEXT, "
    "user_id TEXT, session_data TEXT, agent_data TEXT, team_data TEXT, workflow_data TEXT, metadata TEXT, "
    "runs TEXT, summary TEXT, created_at INTEGER NOT NULL, updated_at INTEGER"
)


def _storage(tmp_path: Path) -> BaseDb:
    return create_state_storage(
        "code",
        tmp_path,
        subdir="sessions",
        session_table="code_sessions",
        prompt_roles=frozenset({"system"}),
    )


def _session(session_id: str, run_ids: list[str]) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        agent_id="code",
        user_id="@alice:example.test",
        runs=[
            RunOutput(
                run_id=run_id,
                agent_id="code",
                session_id=session_id,
                messages=[Message(role="system", content="prompt"), Message(role="user", content=run_id)],
            )
            for run_id in run_ids
        ],
    )


def _run_rows(db_path: Path) -> list[tuple[str, int]]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("SELECT run_id, run_index FROM code_sessions_runs ORDER BY run_index").fetchall()
    finally:
        connection.close()


def test_upsert_session_reconciles_the_runs_table(tmp_path: Path) -> None:
    """Saving a whole session writes its runs in order and drops runs it no longer holds."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        storage.upsert_session(_session("s1", ["r1", "r2", "r3"]))
        assert _run_rows(db_path) == [("r1", 0), ("r2", 1), ("r3", 2)]

        storage.upsert_session(_session("s1", ["r1", "r3"]))
        assert [run_id for run_id, _index in _run_rows(db_path)] == ["r1", "r3"]

        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r1", "r3"]
    assert [message.role for run in loaded.runs or [] for message in run.messages or []] == ["user", "user"]


def test_upsert_run_strips_prompt_roles(tmp_path: Path) -> None:
    """Per-run writes get the same prompt-role stripping as whole-session writes."""
    storage = _storage(tmp_path)
    try:
        storage.upsert_session(_session("s1", []))
        run = RunOutput(
            run_id="r1",
            agent_id="code",
            session_id="s1",
            messages=[Message(role="system", content="prompt"), Message(role="user", content="hi")],
        )
        storage.upsert_run(run=run, session_id="s1", user_id="@alice:example.test", run_index=0)
        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert loaded is not None
    assert [message.role for message in (loaded.runs or [])[0].messages or []] == ["user"]
    assert [message.role for message in run.messages or []] == ["system", "user"]


def test_get_session_ignores_runs_limit_and_hands_out_fresh_run_objects(tmp_path: Path) -> None:
    """MindRoom's history layer needs the whole run list and may edit it in place."""
    storage = _storage(tmp_path)
    try:
        storage.upsert_session(_session("s1", ["r1", "r2", "r3"]))
        limited = storage.get_session("s1", SessionType.AGENT, runs_limit=1)
        first = get_agent_session(storage, "s1")
        second = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert isinstance(limited, AgentSession)
    assert [run.run_id for run in limited.runs or []] == ["r1", "r2", "r3"]
    assert first is not None
    assert second is not None
    assert (first.runs or [])[0] is not (second.runs or [])[0]


def test_legacy_runs_blob_moves_into_the_runs_table(tmp_path: Path) -> None:
    """Opening a 2.x database copies its embedded runs out and drops the blob column."""
    db_path = tmp_path / "sessions" / "code.db"
    db_path.parent.mkdir(parents=True)
    legacy_runs = [
        RunOutput(
            run_id=f"r{index}",
            agent_id="code",
            session_id="s1",
            messages=[Message(role="user", content=str(index))],
        ).to_dict()
        for index in range(3)
    ]
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE code_sessions ({_LEGACY_SESSION_COLUMNS})")
        connection.execute(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, user_id, runs, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            # Agno 2.x encoded the run list to a string and then stored that string as JSON.
            ("s1", "agent", "code", "@alice:example.test", json.dumps(json.dumps(legacy_runs)), 1),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    try:
        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r0", "r1", "r2"]
    assert _run_rows(db_path) == [("r0", 0), ("r1", 1), ("r2", 2)]
    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(code_sessions)")}
    finally:
        connection.close()
    assert "runs" not in columns


def test_runs_appended_after_removal_load_in_session_order(tmp_path: Path) -> None:
    """Compaction removes leading runs; later per-run saves must still sort after the survivors."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        storage.upsert_session(_session("s1", ["r1", "r2", "r3"]))
        compacted = _session("s1", ["r3"])
        storage.upsert_session(compacted)
        appended = _session("s1", ["r3", "r4"])
        # Agno's per-run save passes the run's position in the shortened session.
        storage.upsert_run(run=(appended.runs or [])[1], session_id="s1", user_id="@alice:example.test", run_index=1)
        loaded = get_agent_session(storage, "s1")
        storage.upsert_session(_session("s1", ["r4", "r3"]))
        reordered = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r3", "r4"]
    assert _run_rows(db_path) == [("r4", 0), ("r3", 1)]
    assert reordered is not None
    assert [run.run_id for run in reordered.runs or []] == ["r4", "r3"]


def test_unchanged_session_save_writes_no_run_rows(tmp_path: Path) -> None:
    """A steady-state save must not rewrite run rows it does not change."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        storage.upsert_session(_session("s1", ["r1", "r2"]))
        connection = sqlite3.connect(db_path)
        try:
            before = connection.execute(
                "SELECT run_id, updated_at FROM code_sessions_runs ORDER BY run_index",
            ).fetchall()
        finally:
            connection.close()
        storage.upsert_session(_session("s1", ["r1", "r2"]))
        connection = sqlite3.connect(db_path)
        try:
            after = connection.execute(
                "SELECT run_id, updated_at FROM code_sessions_runs ORDER BY run_index",
            ).fetchall()
        finally:
            connection.close()
    finally:
        storage.close()

    assert after == before


def test_team_session_reconciles_member_runs(tmp_path: Path) -> None:
    """Team sessions keep member rows (parent_run_id) alongside the team run across saves."""
    storage = create_state_storage("eng", tmp_path, subdir="sessions", session_table="eng_sessions")
    member_run = RunOutput(run_id="m1", agent_id="code", session_id="t1", parent_run_id="team1")
    team_run = TeamRunOutput(run_id="team1", team_id="eng", session_id="t1", member_responses=[member_run])
    session = TeamSession(session_id="t1", team_id="eng", runs=[team_run, member_run])
    try:
        storage.upsert_session(session)
        storage.upsert_session(session)
        loaded = get_team_session(storage, "t1")
    finally:
        storage.close()

    assert loaded is not None
    assert [(run.run_id, run.parent_run_id) for run in loaded.runs or []] == [("team1", None), ("m1", "team1")]


def test_wrong_shape_legacy_runs_blob_keeps_the_column(tmp_path: Path) -> None:
    """A blob that is not a run list must never be dropped."""
    db_path = tmp_path / "sessions" / "code.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE code_sessions ({_LEGACY_SESSION_COLUMNS})")
        connection.execute(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, runs, created_at) VALUES (?, ?, ?, ?, ?)",
            ("s1", "agent", "code", json.dumps({"unexpected": "shape"}), 1),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    storage.close()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(code_sessions)")}
    finally:
        connection.close()
    assert "runs" in columns


def test_rejected_owner_mismatch_write_leaves_runs_untouched(tmp_path: Path) -> None:
    """Agno refuses a session write from another user; the runs table must not change either."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        alice_session = _session("s1", ["r1"])
        alice_session.created_at = 1_700_000_000
        storage.upsert_session(alice_session)
        other_users_session = _session("s1", ["r2"])
        other_users_session.user_id = "@bob:example.test"
        other_users_session.created_at = 1_700_000_100

        assert storage.upsert_session(other_users_session) is None
        assert storage.upsert_sessions([other_users_session]) == []
        stored = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert stored is not None
    assert stored.user_id == "@alice:example.test"
    assert _run_rows(db_path) == [("r1", 0)]


def test_malformed_legacy_runs_blob_keeps_the_column(tmp_path: Path) -> None:
    """A truncated 2.x blob must neither abort storage open nor lose the backup column."""
    db_path = tmp_path / "sessions" / "code.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE code_sessions ({_LEGACY_SESSION_COLUMNS})")
        connection.execute(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, runs, created_at) VALUES (?, ?, ?, ?, ?)",
            ("s1", "agent", "code", '"[{"run_id": "r0"', 1),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    storage.close()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(code_sessions)")}
    finally:
        connection.close()
    assert "runs" in columns


def test_refused_legacy_runs_migration_leaves_the_file_untouched(tmp_path: Path) -> None:
    """A session whose runs cannot all land rolls the whole migration back, copied rows included."""
    db_path = tmp_path / "sessions" / "code.db"
    db_path.parent.mkdir(parents=True)
    shared_run = json.dumps(json.dumps([{"run_id": "shared", "agent_id": "code", "messages": []}]))
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE code_sessions ({_LEGACY_SESSION_COLUMNS})")
        for session_id in ("s1", "s2"):  # the same run_id in two sessions cannot both be inserted
            connection.execute(
                "INSERT INTO code_sessions (session_id, session_type, agent_id, runs, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, "agent", "code", shared_run, 1),
            )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    storage.close()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(code_sessions)")}
        copied = connection.execute("SELECT count(*) FROM code_sessions_runs").fetchone()[0]
    finally:
        connection.close()
    assert "runs" in columns
    assert copied == 0
