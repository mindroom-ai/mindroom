"""Runs-table behavior of MindRoom's SQLite session adapter under Agno 3."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_storage import (
    create_state_storage,
    get_agent_session,
    get_team_session,
    replace_runs,
    runs_without,
    save_runs,
)
from tests.conftest import create_agno_2_sessions_db, seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb


def _storage(tmp_path: Path) -> BaseDb:
    return create_state_storage(
        "code",
        tmp_path,
        subdir="sessions",
        session_table="code_sessions",
        prompt_roles=frozenset({"system"}),
    )


def _run(session_id: str, run_id: str) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="code",
        session_id=session_id,
        messages=[Message(role="system", content="prompt"), Message(role="user", content=run_id)],
    )


def _session(session_id: str, run_ids: list[str]) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        agent_id="code",
        user_id="@alice:example.test",
        runs=[_run(session_id, run_id) for run_id in run_ids],
    )


def _run_rows(db_path: Path) -> list[tuple[str, int]]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("SELECT run_id, run_index FROM code_sessions_runs ORDER BY run_index").fetchall()
    finally:
        connection.close()


def _loaded_run_ids(storage: BaseDb, session_id: str) -> list[str]:
    loaded = get_agent_session(storage, session_id)
    assert loaded is not None
    return [run.run_id or "" for run in loaded.runs or []]


def test_saved_runs_load_in_write_order_without_prompt_messages(tmp_path: Path) -> None:
    """Runs are rows indexed in write order; prompt-role messages never reach the store."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        seed_session(storage, _session("s1", ["r1", "r2", "r3"]))
        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert _run_rows(db_path) == [("r1", 0), ("r2", 1), ("r3", 2)]
    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r1", "r2", "r3"]
    assert [message.role for run in loaded.runs or [] for message in run.messages or []] == ["user"] * 3


def test_upsert_run_strips_prompt_roles_from_the_row_only(tmp_path: Path) -> None:
    """The stored row loses prompt-role messages; the caller's run object is untouched."""
    storage = _storage(tmp_path)
    try:
        storage.upsert_session(_session("s1", []))
        run = _run("s1", "r1")
        storage.upsert_run(run=run, session_id="s1", user_id="@alice:example.test", run_index=0)
        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert loaded is not None
    assert [message.role for message in (loaded.runs or [])[0].messages or []] == ["user"]
    assert [message.role for message in run.messages or []] == ["system", "user"]


def test_get_session_ignores_runs_limit(tmp_path: Path) -> None:
    """MindRoom's history layer reasons over the whole run list."""
    storage = _storage(tmp_path)
    try:
        seed_session(storage, _session("s1", ["r1", "r2", "r3"]))
        limited = storage.get_session("s1", SessionType.AGENT, runs_limit=1)
    finally:
        storage.close()

    assert isinstance(limited, AgentSession)
    assert [run.run_id for run in limited.runs or []] == ["r1", "r2", "r3"]


def test_replace_runs_deletes_only_the_dropped_rows(tmp_path: Path) -> None:
    """Removing runs deletes their rows and leaves the survivors' rows unwritten."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        seed_session(storage, _session("s1", ["r1", "r2", "r3"]))
        connection = sqlite3.connect(db_path)
        try:
            before = dict(connection.execute("SELECT run_id, updated_at FROM code_sessions_runs").fetchall())
        finally:
            connection.close()
        session = get_agent_session(storage, "s1")
        assert session is not None
        removed = replace_runs(storage, session, [run for run in session.runs or [] if run.run_id != "r2"])
        connection = sqlite3.connect(db_path)
        try:
            after = dict(connection.execute("SELECT run_id, updated_at FROM code_sessions_runs").fetchall())
        finally:
            connection.close()
        reloaded_ids = _loaded_run_ids(storage, "s1")
    finally:
        storage.close()

    assert removed == ["r2"]
    assert [run.run_id for run in session.runs or []] == ["r1", "r3"]
    assert reloaded_ids == ["r1", "r3"]
    assert after == {"r1": before["r1"], "r3": before["r3"]}


def test_runs_appended_after_deleting_leading_runs_sort_after_the_survivors(tmp_path: Path) -> None:
    """Agno passes the run's position in the shortened session; the row must still come last."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        seed_session(storage, _session("s1", ["r1", "r2", "r3"]))
        session = get_agent_session(storage, "s1")
        assert session is not None
        replace_runs(storage, session, [run for run in session.runs or [] if run.run_id == "r3"])
        # Position 1 in ["r3", "r4"] would sort before the surviving r3 (index 2).
        storage.upsert_run(run=_run("s1", "r4"), session_id="s1", user_id="@alice:example.test", run_index=1)
        loaded_ids = _loaded_run_ids(storage, "s1")
    finally:
        storage.close()

    assert loaded_ids == ["r3", "r4"]
    assert _run_rows(db_path) == [("r3", 2), ("r4", 3)]


def test_save_runs_writes_the_copy_and_swaps_it_into_the_session(tmp_path: Path) -> None:
    """Callers edit a copy of a loaded run; save_runs persists it and the session holds the copy."""
    storage = _storage(tmp_path)
    try:
        seed_session(storage, _session("s1", ["r1", "r2"]))
        session = get_agent_session(storage, "s1")
        assert session is not None
        original = (session.runs or [])[0]
        edited = _run("s1", "r1")
        edited.metadata = {"linked": True}
        save_runs(storage, session, [edited, _run("s1", "r3")])
        reloaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert [run.run_id for run in session.runs or []] == ["r1", "r2", "r3"]
    assert (session.runs or [])[0] is edited
    assert original.metadata is None
    assert reloaded is not None
    assert [(run.run_id, run.metadata) for run in reloaded.runs or []] == [
        ("r1", {"linked": True}),
        ("r2", None),
        ("r3", None),
    ]


def test_team_session_keeps_member_runs(tmp_path: Path) -> None:
    """Team sessions keep member rows (parent_run_id) alongside the team run."""
    storage = create_state_storage("eng", tmp_path, subdir="sessions", session_table="eng_sessions")
    member_run = RunOutput(run_id="m1", agent_id="code", session_id="t1", parent_run_id="team1")
    team_run = TeamRunOutput(run_id="team1", team_id="eng", session_id="t1", member_responses=[member_run])
    session = TeamSession(session_id="t1", team_id="eng", runs=[team_run, member_run])
    try:
        seed_session(storage, session)
        seed_session(storage, session)
        loaded = get_team_session(storage, "t1")
    finally:
        storage.close()

    assert loaded is not None
    assert [(run.run_id, run.parent_run_id) for run in loaded.runs or []] == [("team1", None), ("m1", "team1")]


def test_rejected_owner_mismatch_write_is_reported(tmp_path: Path) -> None:
    """Agno refuses a session write from another user, on the single and the bulk path."""
    storage = _storage(tmp_path)
    try:
        alice_session = _session("s1", [])
        alice_session.created_at = 1_700_000_000
        storage.upsert_session(alice_session)
        other_users_session = _session("s1", [])
        other_users_session.user_id = "@bob:example.test"
        other_users_session.created_at = 1_700_000_100

        assert storage.upsert_session(other_users_session) is None
        assert storage.upsert_sessions([other_users_session]) == []
        stored = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert stored is not None
    assert stored.user_id == "@alice:example.test"


def test_bulk_upsert_preserves_updated_at_when_asked(tmp_path: Path) -> None:
    """The per-row path stamps updated_at with now; preserve_updated_at must undo that."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    try:
        session = _session("s1", [])
        session.created_at = 100
        session.updated_at = 123
        stamped = storage.upsert_sessions([session])
        preserved = storage.upsert_sessions([session], preserve_updated_at=True)
    finally:
        storage.close()

    connection = sqlite3.connect(db_path)
    try:
        created_at, updated_at = connection.execute("SELECT created_at, updated_at FROM code_sessions").fetchone()
    finally:
        connection.close()
    assert (created_at, updated_at) == (100, 123)
    assert isinstance(stamped[0], AgentSession)
    assert stamped[0].updated_at != 123
    assert isinstance(preserved[0], AgentSession)
    assert preserved[0].updated_at == 123


def test_fresh_state_database_gets_no_session_tables(tmp_path: Path) -> None:
    """Opening a state database must not create sessions or runs tables."""
    storage = create_state_storage("code", tmp_path, subdir="learning", session_table="code_learning_sessions")
    storage.close()

    connection = sqlite3.connect(tmp_path / "learning" / "code.db")
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()
    assert tables == set()


def test_legacy_runs_blob_is_merged_into_reads_and_deletions_stick(tmp_path: Path) -> None:
    """A real 2.6.12 database is read as-is; deleting a legacy run scrubs it from the blob, saves append rows."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")

    storage = _storage(tmp_path)
    try:
        loaded = get_agent_session(storage, "session-1")
        assert loaded is not None
        assert [run.run_id for run in loaded.runs or []] == ["run-1", "run-2", "run-3"]
        replace_runs(storage, loaded, [run for run in loaded.runs or [] if run.run_id != "run-2"])
        after_delete = _loaded_run_ids(storage, "session-1")
        save_runs(storage, loaded, [_run("session-1", "run-4")])
        after_save = _loaded_run_ids(storage, "session-1")
    finally:
        storage.close()

    # A restart must not resurrect the deleted legacy run through the blob merge.
    storage = _storage(tmp_path)
    try:
        after_restart = _loaded_run_ids(storage, "session-1")
    finally:
        storage.close()

    assert after_delete == ["run-1", "run-3"]
    assert after_save == ["run-1", "run-3", "run-4"]
    assert after_restart == ["run-1", "run-3", "run-4"]
    assert _run_rows(db_path) == [("run-4", 0)]
    connection = sqlite3.connect(db_path)
    try:
        (blob,) = connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()
    assert [run["run_id"] for run in json.loads(json.loads(blob))] == ["run-1", "run-3"]
    # Opening a pre-existing file must not let agno's connect listener switch it to WAL.
    assert journal_mode == ("delete",)


def test_failed_run_writes_leave_the_session_as_loaded() -> None:
    """The store is written first; when it refuses, ``session.runs`` must not claim the change landed."""
    session = _session("s1", ["r1", "r2"])
    original_runs = list(session.runs or [])
    storage = MagicMock()
    storage.upsert_run.side_effect = RuntimeError("disk full")
    storage.delete_runs.side_effect = RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        save_runs(storage, session, [_run("s1", "r3")])
    with pytest.raises(RuntimeError):
        replace_runs(storage, session, original_runs[:1])

    assert session.runs == original_runs


def test_delete_runs_scrubs_the_legacy_blob_in_the_same_transaction(tmp_path: Path) -> None:
    """A refused blob rewrite must roll back the row deletion; both land or neither does."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")

    storage = _storage(tmp_path)
    try:
        loaded = get_agent_session(storage, "session-1")
        assert loaded is not None
        save_runs(storage, loaded, [_run("session-1", "run-4")])
        # A trigger stands in for any failure of the blob rewrite.
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TRIGGER refuse_blob_rewrite BEFORE UPDATE OF runs ON code_sessions "
                "BEGIN SELECT RAISE(ABORT, 'blob rewrite refused'); END",
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(Exception, match="blob rewrite refused"):
            storage.delete_runs(["run-1", "run-4"])
        after_refusal = _loaded_run_ids(storage, "session-1")
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("DROP TRIGGER refuse_blob_rewrite")
            connection.commit()
        finally:
            connection.close()
        storage.delete_runs(["run-1", "run-4"])
        after_delete = _loaded_run_ids(storage, "session-1")
    finally:
        storage.close()

    assert after_refusal == ["run-1", "run-2", "run-3", "run-4"]
    assert after_delete == ["run-2", "run-3"]
    assert _run_rows(db_path) == []


def test_save_runs_refuses_a_run_object_loaded_from_the_session() -> None:
    """Loaded runs are shared and immutable; only a copy may be edited and written back."""
    session = _session("s1", ["r1"])
    storage = MagicMock()
    loaded_run = (session.runs or [])[0]
    loaded_run.metadata = {"edited": "in place"}

    with pytest.raises(ValueError, match="edit a copy"):
        save_runs(storage, session, [loaded_run])
    storage.upsert_run.assert_not_called()


def test_deleting_a_team_run_takes_its_member_rows_along(tmp_path: Path) -> None:
    """Member runs are rows whose parent_run_id is the team run; deleting the parent must not orphan them."""
    storage = create_state_storage("eng", tmp_path, subdir="sessions", session_table="eng_sessions")
    member = RunOutput(run_id="m1", agent_id="code", session_id="t1", parent_run_id="team1")
    grandchild = RunOutput(run_id="m1-child", agent_id="code", session_id="t1", parent_run_id="m1")
    team_run = TeamRunOutput(run_id="team1", team_id="eng", session_id="t1")
    other = TeamRunOutput(run_id="team2", team_id="eng", session_id="t1")
    try:
        session = seed_session(
            storage,
            TeamSession(session_id="t1", team_id="eng", runs=[team_run, member, grandchild, other]),
        )
        kept = runs_without(session.runs or [], ["team1"])
        removed = replace_runs(storage, session, kept)
        reloaded = get_team_session(storage, "t1")
        storage.delete_runs(["team2"])
        after_direct_delete = get_team_session(storage, "t1")
    finally:
        storage.close()

    assert [run.run_id for run in kept] == ["team2"]
    assert sorted(removed) == ["m1", "m1-child", "team1"]
    assert reloaded is not None
    assert [run.run_id for run in reloaded.runs or []] == ["team2"]
    assert after_direct_delete is not None
    assert after_direct_delete.runs == []


def test_delete_runs_scrubs_legacy_blob_descendants_too(tmp_path: Path) -> None:
    """A 2.x blob can hold member entries under a team run; scrubbing the parent scrubs them."""
    db_path = tmp_path / "sessions" / "code.db"
    db_path.parent.mkdir(parents=True)
    legacy_runs = [
        {"run_id": "team1", "session_id": "s1"},
        {"run_id": "m1", "session_id": "s1", "parent_run_id": "team1"},
        {"run_id": "team2", "session_id": "s1"},
    ]
    connection = sqlite3.connect(create_agno_2_sessions_db(db_path))
    try:
        connection.execute(
            "UPDATE code_sessions SET runs = ? WHERE session_id = 'session-1'",
            (json.dumps(json.dumps(legacy_runs)),),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    try:
        storage.delete_runs(["team1"])
    finally:
        storage.close()

    connection = sqlite3.connect(db_path)
    try:
        (blob,) = connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone()
    finally:
        connection.close()
    assert [run["run_id"] for run in json.loads(json.loads(blob))] == ["team2"]


def test_runs_without_drops_a_child_that_has_no_run_id() -> None:
    """A descendant is identified by its parent_run_id even when it never got a run_id of its own."""
    root = TeamRunOutput(run_id="root", team_id="eng", session_id="t1")
    orphan_child = RunOutput(agent_id="code", session_id="t1", parent_run_id="root")
    other = RunOutput(run_id="other", agent_id="code", session_id="t1")

    assert runs_without([root, orphan_child, other], ["root"]) == [other]


def test_delete_runs_tolerates_malformed_legacy_blob_entries(tmp_path: Path) -> None:
    """Entries with missing or non-string ids never match; they are carried along, not crashed on."""
    db_path = tmp_path / "sessions" / "code.db"
    db_path.parent.mkdir(parents=True)
    legacy_runs = [
        {"run_id": "team1", "session_id": "s1"},
        {"session_id": "s1", "parent_run_id": "team1"},
        {"run_id": 7, "parent_run_id": ["team1"]},
        {"run_id": None, "parent_run_id": None},
        "not a dict",
        {"run_id": "keep", "parent_run_id": {"nested": True}},
    ]
    connection = sqlite3.connect(create_agno_2_sessions_db(db_path))
    try:
        connection.execute(
            "UPDATE code_sessions SET runs = ? WHERE session_id = 'session-1'",
            (json.dumps(json.dumps(legacy_runs)),),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    try:
        storage.delete_runs(["team1"])
    finally:
        storage.close()

    connection = sqlite3.connect(db_path)
    try:
        (blob,) = connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone()
    finally:
        connection.close()
    assert json.loads(json.loads(blob)) == legacy_runs[2:]
