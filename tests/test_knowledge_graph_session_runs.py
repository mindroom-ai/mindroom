"""The graph reads every finished Matrix turn of a session, live or compacted, once."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.history import archive
from mindroom.knowledge_graph.session_runs import session_runs, updated_sessions
from mindroom.session_storage.agent_storage import create_state_storage
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ROOM = "!room:example.test"
SESSION = f"{ROOM}:$thread"


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[SqliteDb]:
    """The production conversation storage owner."""
    db = create_state_storage("mind", tmp_path, subdir="sessions", session_table="mind_sessions")
    assert isinstance(db, SqliteDb)
    try:
        yield db
    finally:
        db.close()


def _run(
    run_id: str,
    created_at: int,
    *,
    room_id: str | None = ROOM,
    parent_run_id: str | None = None,
    status: RunStatus = RunStatus.completed,
) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        session_id=SESSION,
        agent_id="mind",
        parent_run_id=parent_run_id,
        created_at=created_at,
        status=status,
        metadata={"room_id": room_id, "thread_id": "$thread"} if room_id is not None else {},
        messages=[Message(role="user", content=f"q {run_id}"), Message(role="assistant", content=f"a {run_id}")],
    )


def _seed(storage: SqliteDb, runs: list[RunOutput], session_id: str = SESSION) -> AgentSession:
    return seed_session(storage, AgentSession(session_id=session_id, agent_id="mind", runs=runs))


def test_session_runs_merge_live_and_archived_runs_in_order(storage: SqliteDb) -> None:
    """Compacted runs are still read, in time order with the live ones, each once."""
    session = _seed(storage, [_run("r1", 100), _run("r2", 200), _run("r3", 300)])
    archive.archive_runs(
        storage,
        session_id=SESSION,
        scope_key="agent:mind",
        summary="summary",
        summary_model="m",
        runs=[run for run in session.runs or [] if run.run_id in {"r1", "r2"}],
        event_ids={},
        seen_event_ids={},
    )

    assert [run.run_id for run in session_runs(storage, SESSION)] == ["r1", "r2", "r3"]


def test_unfinished_member_and_non_matrix_runs_are_excluded(storage: SqliteDb) -> None:
    """Only finished top-level Matrix turns are evidence."""
    _seed(
        storage,
        [
            _run("done", 100),
            _run("failed", 200, status=RunStatus.error),
            _run("member", 300, parent_run_id="done"),
            _run("api", 400, room_id=None),
        ],
    )

    assert [run.run_id for run in session_runs(storage, SESSION)] == ["done"]


def test_updated_sessions_lists_sessions_changed_after_the_cursor(storage: SqliteDb) -> None:
    """Sessions are listed with their last update, oldest first, and workflow participant sessions are skipped."""
    _seed(storage, [_run("r1", 100)], session_id="!a:x:$t1")
    _seed(storage, [_run("r2", 100)], session_id="!a:x:$t1:dynamic_workflow:w:p")

    listed = updated_sessions(storage, "mind", since=0)

    assert [session_id for session_id, _ in listed] == ["!a:x:$t1"]
    assert updated_sessions(storage, "mind", since=listed[0][1]) == []
