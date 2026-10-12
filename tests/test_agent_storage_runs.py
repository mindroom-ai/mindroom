"""Runs-table behavior of MindRoom's SQLite session adapter under Agno 3."""

from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.session_storage.agent_storage import (
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


def test_loaded_runs_drop_their_stored_tool_schemas(tmp_path: Path) -> None:
    """Older runs carry a copy of every tool schema; loading drops it and keeps the rest of the metadata."""
    storage = _storage(tmp_path)
    try:
        run = _run("s1", "r1")
        run.metadata = {"room_id": "!room:example.test", "tools_schema": [{"name": "demo", "description": "Demo"}]}
        storage.upsert_session(_session("s1", []))
        storage.upsert_run(run=run, session_id="s1", user_id="@alice:example.test", run_index=0)
        storage.close()
        storage = _storage(tmp_path)
        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert loaded is not None
    assert [loaded_run.metadata for loaded_run in loaded.runs or []] == [{"room_id": "!room:example.test"}]


def test_loaded_team_runs_drop_tool_schemas_of_embedded_member_responses(tmp_path: Path) -> None:
    """A paused team run stores its paused member responses inside its row; those lose the copy too."""
    storage = create_state_storage("eng", tmp_path, subdir="sessions", session_table="eng_sessions")
    schema = [{"name": "demo", "description": "Demo"}]
    member_run = RunOutput(
        run_id="m1",
        agent_id="code",
        session_id="t1",
        parent_run_id="team1",
        status=RunStatus.paused,
        metadata={"tools_schema": schema},
    )
    team_run = TeamRunOutput(
        run_id="team1",
        team_id="eng",
        session_id="t1",
        status=RunStatus.paused,
        member_responses=[member_run],
        metadata={"tools_schema": schema},
    )
    try:
        seed_session(storage, TeamSession(session_id="t1", team_id="eng", runs=[team_run]))
        storage.close()
        storage = create_state_storage("eng", tmp_path, subdir="sessions", session_table="eng_sessions")
        loaded = get_team_session(storage, "t1")
    finally:
        storage.close()

    assert loaded is not None
    [loaded_team_run] = loaded.runs or []
    assert isinstance(loaded_team_run, TeamRunOutput)
    assert loaded_team_run.metadata == {}
    assert [member.metadata for member in loaded_team_run.member_responses] == [{}]


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
    """Bulk writes preserve the caller's timestamp only when requested."""
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


@pytest.mark.parametrize("session_type", [SessionType.AGENT, SessionType.TEAM])
@pytest.mark.parametrize("bulk", [False, True])
def test_session_json_is_written_once_and_legacy_json_still_loads(
    tmp_path: Path,
    session_type: SessionType,
    bulk: bool,
) -> None:
    """New session fields are JSON objects; pre-upgrade string-encoded objects still load."""
    storage = _storage(tmp_path)
    db_path = tmp_path / "sessions" / "code.db"
    session_data = {"session_state": {"topic": "café", "enabled": True}}
    metadata = {"requester": "@alice:example.test", "nested": {"count": 2}}
    entity_data = {"name": "Code", "description": "Helpful assistant"}
    summary = SessionSummary(summary="Earlier discussion", topics=["weather"])
    session_class = AgentSession if session_type == SessionType.AGENT else TeamSession
    session = session_class(
        session_id="json-session",
        user_id="@alice:example.test",
        session_data=session_data,
        metadata=metadata,
        summary=summary,
        created_at=1_700_000_000,
    )
    if isinstance(session, AgentSession):
        session.agent_data = entity_data
    else:
        session.team_data = entity_data
    expected = (
        session_data,
        metadata,
        entity_data if session_type == SessionType.AGENT else None,
        entity_data if session_type == SessionType.TEAM else None,
        {"summary": "Earlier discussion", "topics": ["weather"]},
    )
    select_json = "SELECT session_data, metadata, agent_data, team_data, summary FROM code_sessions"
    try:
        if bulk:
            assert len(storage.upsert_sessions([session])) == 1
        else:
            assert storage.upsert_session(session) is not None
        with sqlite3.connect(db_path) as connection:
            stored = connection.execute(select_json).fetchone()
            assert tuple(json.loads(value) if value is not None else None for value in stored) == expected
            connection.execute(
                "UPDATE code_sessions SET session_data = ?, metadata = ?, agent_data = ?, team_data = ?, summary = ?",
                tuple(json.dumps(value) if value is not None else None for value in stored),
            )
        loaded = storage.get_session("json-session", session_type)
        assert isinstance(loaded, session_class)
        assert loaded.session_data == session_data
        assert loaded.metadata == metadata
        assert loaded.summary == summary
        assert (loaded.agent_data if isinstance(loaded, AgentSession) else loaded.team_data) == entity_data
        assert storage.upsert_session(loaded) is not None
        with sqlite3.connect(db_path) as connection:
            rewritten = connection.execute(select_json).fetchone()
        assert tuple(json.loads(value) if value is not None else None for value in rewritten) == expected
    finally:
        storage.close()


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


def _blob(db_path: Path) -> object:
    connection = sqlite3.connect(db_path)
    try:
        (blob,) = connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone()
    finally:
        connection.close()
    return blob


def _set_blob(db_path: Path, legacy_runs: list[object]) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE code_sessions SET runs = ? WHERE session_id = 'session-1'",
            (json.dumps(json.dumps(legacy_runs)),),
        )
        connection.commit()
    finally:
        connection.close()


def test_agno_2_blob_runs_become_rows_once(tmp_path: Path) -> None:
    """A real 2.6.12 database's blob runs become run rows in blob order when it opens; the blob is cleared."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")

    storage = _storage(tmp_path)
    try:
        loaded = get_agent_session(storage, "session-1")
        assert loaded is not None
        assert [run.run_id for run in loaded.runs or []] == ["run-1", "run-2", "run-3"]
        assert _run_rows(db_path) == [("run-1", 0), ("run-2", 1), ("run-3", 2)]
        assert _blob(db_path) is None
        replace_runs(storage, loaded, [run for run in loaded.runs or [] if run.run_id != "run-2"])
        save_runs(storage, loaded, [_run("session-1", "run-4")])
    finally:
        storage.close()

    storage = _storage(tmp_path)
    try:
        after_restart = _loaded_run_ids(storage, "session-1")
    finally:
        storage.close()

    assert after_restart == ["run-1", "run-3", "run-4"]
    assert _run_rows(db_path) == [("run-1", 0), ("run-3", 2), ("run-4", 3)]
    connection = sqlite3.connect(db_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()
    # Opening a pre-existing file must not let agno's connect listener switch it to WAL.
    assert journal_mode == ("delete",)


def test_blob_runs_keep_the_order_agno_read_them_in(tmp_path: Path) -> None:
    """Blob order with table copies substituted, then table-only rows, as Agno's merge returned them."""
    template = _storage(tmp_path / "template")
    try:
        seed_session(template, _session("session-1", ["template"]))
    finally:
        template.close()
    connection = sqlite3.connect(tmp_path / "template" / "sessions" / "code.db")
    try:
        (runs_ddl,) = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'code_sessions_runs'",
        ).fetchone()
    finally:
        connection.close()
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(runs_ddl)
        for run_id, run_index, content in (("run-9", 0, "table only"), ("run-2", 1, "table copy")):
            run = {"run_id": run_id, "agent_id": "code", "session_id": "session-1", "status": "COMPLETED"}
            connection.execute(
                "INSERT INTO code_sessions_runs (run_id, session_id, run_type, agent_id, status, run_index, "
                "run_data, created_at) VALUES (?, 'session-1', 'agent', 'code', 'COMPLETED', ?, ?, 1700000009)",
                (run_id, run_index, json.dumps({**run, "content": content})),
            )
        connection.commit()
    finally:
        connection.close()

    storage = _storage(tmp_path)
    try:
        loaded = get_agent_session(storage, "session-1")
    finally:
        storage.close()

    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["run-1", "run-2", "run-3", "run-9"]
    assert loaded.runs[1].content == "table copy"
    assert _run_rows(db_path) == [("run-1", 0), ("run-2", 1), ("run-3", 2), ("run-9", 3)]


def test_blob_entries_without_a_string_run_id_are_dropped(tmp_path: Path) -> None:
    """Only entries a read could address by id become rows; member entries keep their parent."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    _set_blob(
        db_path,
        [
            {"run_id": "team1", "agent_id": "code", "session_id": "session-1"},
            {"session_id": "session-1", "parent_run_id": "team1"},
            {"run_id": 7, "parent_run_id": ["team1"]},
            {"run_id": None, "parent_run_id": None},
            "not a dict",
            {"run_id": "m1", "agent_id": "helper", "session_id": "session-1", "parent_run_id": "team1"},
        ],
    )

    _storage(tmp_path).close()

    assert _run_rows(db_path) == [("team1", 0), ("m1", 1)]
    assert _blob(db_path) is None


def _status_run(run_id: str, status: RunStatus, *, content: str | None = None) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="code",
        session_id="s1",
        status=status,
        content=content,
        messages=[Message(role="user", content=run_id)],
    )


def _summary_after(db_path: Path, run_id: str) -> None:
    """Position one summary row of the agent scope right after ``run_id``, as compaction would."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO code_sessions_summaries (session_id, scope_key, after_run_index, summary, created_at) "
            "SELECT 's1', 'agent:code', run_index, 'summary', 1700000000 FROM code_sessions_runs WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _stored_run(db_path: Path, run_id: str) -> tuple[int, str, str]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT run_index, status, run_data FROM code_sessions_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()


def test_new_runs_sort_after_every_summary_row(tmp_path: Path) -> None:
    """Deleting the newest runs must not let a new run reuse an index a summary row sorts after."""
    db_path = tmp_path / "sessions" / "code.db"
    storage = _storage(tmp_path)
    try:
        seed_session(
            storage,
            AgentSession(
                session_id="s1",
                agent_id="code",
                runs=[_status_run(f"r{i}", RunStatus.completed) for i in range(3)],
            ),
        )
        _summary_after(db_path, "r2")
        storage.delete_runs(["r1", "r2"])
        storage.upsert_run(_status_run("r3", RunStatus.completed), session_id="s1")
        loaded = _loaded_run_ids(storage, "s1")
    finally:
        storage.close()

    assert _stored_run(db_path, "r3")[0] == 3
    assert loaded == ["r3"]


@pytest.mark.parametrize(
    "stored_status",
    [RunStatus.paused, RunStatus.cancelled, RunStatus.error],
    ids=["approval", "cli-checkpoint", "interrupted-record"],
)
def test_a_non_visible_run_saved_visible_moves_to_the_end(tmp_path: Path, stored_status: RunStatus) -> None:
    """A continuation finishing after newer turns and a compaction replays after the summary."""
    db_path = tmp_path / "sessions" / "code.db"
    storage = _storage(tmp_path)
    try:
        runs = [_status_run("r0", stored_status), *(_status_run(f"r{i}", RunStatus.completed) for i in range(1, 4))]
        seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))
        _summary_after(db_path, "r3")
        storage.upsert_run(_status_run("r0", RunStatus.completed, content="approved"), session_id="s1")
        loaded = get_agent_session(storage, "s1")
    finally:
        storage.close()

    assert _stored_run(db_path, "r0")[0] == 4
    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r0"]


def test_a_continuation_without_a_summary_replays_after_the_turns_it_waited_for(tmp_path: Path) -> None:
    """The move holds without compaction too: the resumed run sorts after the turns its continuation saw."""
    storage = _storage(tmp_path)
    try:
        runs = [_status_run("r0", RunStatus.paused), _status_run("r1", RunStatus.completed)]
        seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))
        storage.upsert_run(_status_run("r0", RunStatus.completed), session_id="s1")
        loaded = _loaded_run_ids(storage, "s1")
    finally:
        storage.close()

    assert loaded == ["r1", "r0"]


def test_a_save_keeping_visibility_never_moves_a_run(tmp_path: Path) -> None:
    """Re-saving a visible run, or a paused run that stays paused, keeps its place."""
    db_path = tmp_path / "sessions" / "code.db"
    storage = _storage(tmp_path)
    try:
        runs = [
            _status_run("r0", RunStatus.paused),
            _status_run("r1", RunStatus.completed),
            _status_run("r2", RunStatus.completed),
        ]
        seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))
        storage.upsert_run(_status_run("r1", RunStatus.completed, content="linked"), session_id="s1")
        storage.upsert_run(_status_run("r0", RunStatus.paused, content="still waiting"), session_id="s1")
    finally:
        storage.close()

    assert _stored_run(db_path, "r0")[0] == 0
    assert _stored_run(db_path, "r1")[0] == 1


def test_a_folded_run_is_frozen(tmp_path: Path) -> None:
    """A stale copy of a run behind the latest summary row changes neither its row nor its usage."""
    db_path = tmp_path / "sessions" / "code.db"
    storage = _storage(tmp_path)
    try:
        runs = [
            _status_run("p0", RunStatus.paused),
            _status_run("r1", RunStatus.completed, content="original"),
            _status_run("r2", RunStatus.completed),
        ]
        seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))
        _summary_after(db_path, "r2")
        before = _stored_run(db_path, "r1")
        with sqlite3.connect(db_path) as connection:
            usage_before = connection.execute(
                "SELECT usage_data FROM code_sessions_usage WHERE run_id = 'r1'",
            ).fetchone()
        storage.upsert_run(_status_run("r1", RunStatus.paused, content="stale paused"), session_id="s1")
        storage.upsert_run(_status_run("r1", RunStatus.completed, content="stale completed"), session_id="s1")
        storage.upsert_run(_status_run("p0", RunStatus.paused, content="still waiting"), session_id="s1")
        loaded = _loaded_run_ids(storage, "s1")
    finally:
        storage.close()

    assert _stored_run(db_path, "r1") == before
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT usage_data FROM code_sessions_usage WHERE run_id = 'r1'").fetchone()
            == usage_before
        )
    assert json.loads(_stored_run(db_path, "p0")[2])["content"] == "still waiting"
    assert loaded == ["p0"]


def test_a_member_of_a_folded_team_run_is_frozen(tmp_path: Path) -> None:
    """A folded team run's member rows are as final as the team run."""
    db_path = tmp_path / "sessions" / "code.db"
    storage = _storage(tmp_path)
    member = RunOutput(run_id="m0", agent_id="helper", session_id="s1", parent_run_id="t0", content="original")
    try:
        seed_session(
            storage,
            TeamSession(
                session_id="s1",
                team_id="eng",
                runs=[member, TeamRunOutput(run_id="t0", team_id="eng", session_id="s1", status=RunStatus.completed)],
            ),
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "INSERT INTO code_sessions_summaries (session_id, scope_key, after_run_index, summary, created_at) "
                "SELECT 's1', 'team:eng', run_index, 'summary', 1700000000 FROM code_sessions_runs WHERE run_id = 't0'",
            )
            connection.commit()
        finally:
            connection.close()
        stale = RunOutput(run_id="m0", agent_id="helper", session_id="s1", parent_run_id="t0", content="stale")
        storage.upsert_run(stale, session_id="s1")
    finally:
        storage.close()

    assert json.loads(_stored_run(db_path, "m0")[2])["content"] == "original"


def test_a_removed_run_is_never_written_again(tmp_path: Path) -> None:
    """A run a cut deleted, and a member row under it, stay gone when a late save arrives."""
    db_path = tmp_path / "sessions" / "code.db"
    storage = _storage(tmp_path)
    try:
        seed_session(
            storage,
            AgentSession(session_id="s1", agent_id="code", runs=[_status_run("r0", RunStatus.completed)]),
        )
        with sqlite3.connect(db_path) as connection:
            connection.execute("INSERT INTO code_sessions_removed_runs (run_id, session_id) VALUES ('gone', 's1')")
        storage.upsert_run(_status_run("gone", RunStatus.completed), session_id="s1")
        child = RunOutput(run_id="gone-member", agent_id="helper", session_id="s1", parent_run_id="gone")
        storage.upsert_run(child, session_id="s1")
        loaded = _loaded_run_ids(storage, "s1")
    finally:
        storage.close()

    assert loaded == ["r0"]
    assert _stored_run(db_path, "gone") is None
    assert _stored_run(db_path, "gone-member") is None


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


def test_runs_without_removes_every_descendant() -> None:
    """Grandchildren leave with their root even when stored before it."""
    runs = [
        RunOutput(run_id="unrelated"),
        RunOutput(run_id="child", parent_run_id="root"),
        RunOutput(run_id="grandchild", parent_run_id="child"),
        RunOutput(run_id="root"),
    ]

    assert [run.run_id for run in runs_without(runs, ["root"])] == ["unrelated"]


def test_runs_without_drops_a_child_that_has_no_run_id() -> None:
    """A descendant is identified by its parent_run_id even when it never got a run_id of its own."""
    root = TeamRunOutput(run_id="root", team_id="eng", session_id="t1")
    orphan_child = RunOutput(agent_id="code", session_id="t1", parent_run_id="root")
    other = RunOutput(run_id="other", agent_id="code", session_id="t1")

    assert runs_without([root, orphan_child, other], ["root"]) == [other]


def test_cache_diagnostics_count_history_without_retaining_closed_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counts cover current adapters, not closed stores or private conversation identifiers."""
    from structlog.testing import capture_logs  # noqa: PLC0415

    from mindroom.session_storage import agent_storage  # noqa: PLC0415

    # Scope diagnostics to this test's adapters; other tests may retain an
    # otherwise unreachable Agno adapter cycle until the next GC collection.
    monkeypatch.setattr(agent_storage, "_CACHE_DIAGNOSTICS", weakref.WeakKeyDictionary())
    monkeypatch.setattr(agent_storage, "_CACHE_DIAGNOSTICS_NEXT_REPORT", 0.0, raising=False)
    first = _storage(tmp_path / "first")
    second = _storage(tmp_path / "second")
    try:
        seed_session(first, _session("private-first", ["r1", "r2"]))
        seed_session(second, _session("private-second", ["r3"]))
        with capture_logs() as logs:
            get_agent_session(first, "private-first")
            get_agent_session(second, "private-second")
        summaries = [entry for entry in logs if entry["event"] == "conversation_cache_summary"]
        assert len(summaries) == 1
        assert summaries[0]["observed_cached_runs"] == 2
        first.close()
        monkeypatch.setattr(agent_storage, "_CACHE_DIAGNOSTICS_NEXT_REPORT", 0.0)
        with capture_logs() as logs:
            get_agent_session(second, "private-second")
        summary = next(entry for entry in logs if entry["event"] == "conversation_cache_summary")
        assert summary["adapters"] == 1
        assert summary["observed_cached_sessions"] == 1
        assert summary["observed_cached_runs"] == 1
        assert "private" not in str(summary)
    finally:
        first.close()
        second.close()


def test_cache_diagnostics_ignore_read_finishing_after_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read paused before publishing cannot restore a closed adapter's snapshot."""
    from mindroom.session_storage import agent_storage  # noqa: PLC0415

    storage = _storage(tmp_path)
    seed_session(storage, _session("s1", ["r1"]))
    report_started = threading.Event()
    resume_report = threading.Event()
    report_cache_counts = storage._report_cache_counts

    def paused_report() -> None:
        report_started.set()
        assert resume_report.wait(timeout=5)
        report_cache_counts()

    monkeypatch.setattr(storage, "_report_cache_counts", paused_report)
    with ThreadPoolExecutor(max_workers=1) as executor:
        read = executor.submit(get_agent_session, storage, "s1")
        try:
            assert report_started.wait(timeout=5)
            storage.close()
            resume_report.set()
            loaded = read.result(timeout=5)
            assert loaded is not None
            assert [run.run_id for run in loaded.runs or []] == ["r1"]
            with agent_storage._CACHE_DIAGNOSTICS_LOCK:
                assert storage not in agent_storage._CACHE_DIAGNOSTICS
            # Disposing SQLAlchemy's pool still permits later SQL reads, but
            # explicit close ends this adapter's diagnostic lifetime.
            assert get_agent_session(storage, "s1") is not None
            with agent_storage._CACHE_DIAGNOSTICS_LOCK:
                assert storage not in agent_storage._CACHE_DIAGNOSTICS
        finally:
            resume_report.set()
            read.result(timeout=5)
            storage.close()
