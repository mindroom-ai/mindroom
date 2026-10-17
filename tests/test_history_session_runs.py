"""Background workers read every finished Matrix turn of a session, compacted or not, once."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY
from mindroom.history.session_runs import session_runs, unredacted_runs, updated_sessions
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage, get_agent_session
from tests.conftest import seed_session
from tests.history_helpers import compact_runs

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


def test_session_runs_read_compacted_and_later_runs_in_order(storage: SqliteDb) -> None:
    """Runs a summary folded are still read, in time order with the later ones, each once."""
    session = _seed(storage, [_run("r1", 100), _run("r2", 200), _run("r3", 300)])
    compact_runs(storage, session, HistoryScope(kind="agent", scope_id="mind"), ["r1", "r2"], "summary")
    loaded = get_agent_session(storage, SESSION)
    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r3"]

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


@pytest.mark.asyncio
async def test_unredacted_runs_drop_redacted_turns_or_wait_while_the_agent_cannot_tell() -> None:
    """A turn that answered a redacted event is dropped, and nothing is returned while a room's redactions are unknown."""
    kept, redacted = _run("kept", 100), _run("gone", 200)
    kept.metadata = {**(kept.metadata or {}), MATRIX_EVENT_ID_METADATA_KEY: "$kept"}
    redacted.metadata = {**(redacted.metadata or {}), MATRIX_EVENT_ID_METADATA_KEY: "$gone"}
    asked: list[tuple[str, str, tuple[str, ...]]] = []

    async def journal(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        asked.append((agent_name, room_id, event_ids))
        return frozenset({"$gone"})

    async def not_running(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id, event_ids
        return None

    assert await unredacted_runs([kept, redacted], "mind", journal) == [kept]
    assert asked == [("mind", ROOM, ("$gone", "$kept"))]
    assert await unredacted_runs([kept, redacted], "mind", not_running) is None
