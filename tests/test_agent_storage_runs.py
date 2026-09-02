"""Runs-table behavior of MindRoom's SQLite session adapter under Agno 3."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage, get_agent_session

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
            run_id=f"r{index}", agent_id="code", session_id="s1", messages=[Message(role="user", content=str(index))]
        ).to_dict()
        for index in range(3)
    ]
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE code_sessions ({_LEGACY_SESSION_COLUMNS})")
        connection.execute(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, user_id, runs, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "agent", "code", "@alice:example.test", json.dumps(legacy_runs), 1),
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
