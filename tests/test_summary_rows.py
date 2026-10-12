"""Conversation reads through the summary rows of the run log."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from sqlalchemy import event

from mindroom.session_storage.agent_storage import create_state_storage, get_agent_session, get_team_session
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.sqlite import SqliteDb


def _open(tmp_path: Path) -> SqliteDb:
    return create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")  # type: ignore[return-value]


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "code.db"


def _run(run_id: str, *, status: RunStatus = RunStatus.completed) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="code",
        session_id="s1",
        status=status,
        messages=[Message(role="user", content=f"question {run_id}")],
    )


def _run_index(tmp_path: Path, run_id: str) -> int:
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        (run_index,) = connection.execute(
            "SELECT run_index FROM code_sessions_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return run_index


def _add_summary_row(
    tmp_path: Path,
    *,
    after_run_index: int,
    summary: str | None,
    scope_key: str = "agent:code",
    session_id: str = "s1",
) -> None:
    connection = sqlite3.connect(_db_path(tmp_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO code_sessions_summaries "
            "(session_id, scope_key, after_run_index, summary, summary_model, event_ids, seen_event_ids, created_at) "
            "VALUES (?, ?, ?, ?, 'model', '[]', '[]', 1700000000)",
            (session_id, scope_key, after_run_index, summary),
        )
        connection.commit()
    finally:
        connection.close()


def _tables(tmp_path: Path) -> set[str]:
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.fixture
def storage(tmp_path: Path) -> SqliteDb:
    """Open a fresh conversation database."""
    db = _open(tmp_path)
    yield db
    db.close()


def test_fresh_storage_has_summary_tables(tmp_path: Path) -> None:
    """Every conversation database opens with both summary-log tables, which cascade from the session row."""
    storage = _open(tmp_path)
    try:
        seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0")]))
        _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r0"), summary="earlier")
        with sqlite3.connect(_db_path(tmp_path)) as connection:
            connection.execute("INSERT INTO code_sessions_removed_runs (session_id, run_id) VALUES ('s1', 'gone')")
        storage.delete_session("s1")
    finally:
        storage.close()
    _open(tmp_path).close()

    assert {"code_sessions_summaries", "code_sessions_removed_runs"} <= _tables(tmp_path)
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_sessions_summaries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM code_sessions_removed_runs").fetchone() == (0,)


def test_load_returns_runs_after_the_latest_summary_and_every_non_visible_run(
    storage: SqliteDb,
    tmp_path: Path,
) -> None:
    """A summary row hides the visible runs it covers; paused runs and later runs still load."""
    runs = [_run("r0"), _run("r1", status=RunStatus.paused), _run("r2"), _run("r3"), _run("r4")]
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r0"), summary="first")
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r2"), summary="second")

    session = get_agent_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r1", "r3", "r4"]


def test_load_without_summary_rows_returns_every_run(storage: SqliteDb) -> None:
    """A conversation that never compacted reads exactly as before."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))

    session = get_agent_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r0", "r1"]


def test_load_keeps_member_rows_with_their_team_run(storage: SqliteDb, tmp_path: Path) -> None:
    """Member rows load with their team run wherever their index sits, and stay hidden with a folded one."""

    def member(run_id: str, parent: str) -> RunOutput:
        return RunOutput(run_id=run_id, agent_id="helper", session_id="s1", parent_run_id=parent)

    def team_run(run_id: str) -> TeamRunOutput:
        return TeamRunOutput(run_id=run_id, team_id="eng", session_id="s1", status=RunStatus.completed)

    seed_session(
        storage,
        TeamSession(
            session_id="s1",
            team_id="eng",
            runs=[member("m0", "t0"), team_run("t0"), member("m1", "t1"), team_run("t1")],
        ),
    )
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "m1"), summary="team summary", scope_key="team:eng")

    session = get_team_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["m1", "t1"]


def test_barrier_row_hides_the_runs_behind_it(storage: SqliteDb, tmp_path: Path) -> None:
    """A summary row without text retires what it covers: the runs behind it stay hidden."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r0"), summary="retired")
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r0"), summary=None)

    session = get_agent_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r1"]


def test_load_reads_dict_sessions_through_the_same_filter(storage: SqliteDb, tmp_path: Path) -> None:
    """``deserialize=False`` reads see the same runs as object reads."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r0"), summary="first")

    raw = storage.get_session("s1", SessionType.AGENT, deserialize=False)

    assert isinstance(raw, dict)
    assert [run["run_id"] for run in raw["runs"]] == ["r1"]


def test_load_reads_one_snapshot(storage: SqliteDb, tmp_path: Path) -> None:
    """A commit cannot land between the reads of one session load, so runs and summary always agree."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))
    boundary = _run_index(tmp_path, "r0")
    selects = 0
    writer_errors: list[str] = []

    def write_between_reads(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        nonlocal selects
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        selects += 1
        if selects != 2:
            return
        writer = sqlite3.connect(_db_path(tmp_path), timeout=0)
        try:
            writer.execute(
                "INSERT INTO code_sessions_summaries "
                "(session_id, scope_key, after_run_index, summary, summary_model, event_ids, seen_event_ids, "
                "created_at) VALUES ('s1', 'agent:code', ?, 'raced', 'model', '[]', '[]', 1700000000)",
                (boundary,),
            )
            writer.commit()
        except sqlite3.OperationalError as error:
            writer_errors.append(str(error))
        finally:
            writer.close()

    event.listen(storage.db_engine, "before_cursor_execute", write_between_reads)
    try:
        session = get_agent_session(storage, "s1")
    finally:
        event.remove(storage.db_engine, "before_cursor_execute", write_between_reads)

    assert writer_errors == ["database is locked"]
    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r0", "r1"]


def test_cancelled_and_errored_runs_behind_the_summary_still_load(storage: SqliteDb, tmp_path: Path) -> None:
    """Every non-visible run loads wherever it sits, as `main` keeps such runs live."""
    runs = [_run("c0", status=RunStatus.cancelled), _run("e1", status=RunStatus.error), _run("r2"), _run("r3")]
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r2"), summary="folded")

    session = get_agent_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["c0", "e1", "r3"]


def test_load_follows_member_rows_to_any_depth_and_drops_orphans(storage: SqliteDb, tmp_path: Path) -> None:
    """A paused team run behind the summary loads with its members' members; a member without a stored parent does not."""

    def member(run_id: str, parent: str) -> RunOutput:
        return RunOutput(run_id=run_id, agent_id="helper", session_id="s1", parent_run_id=parent)

    seed_session(
        storage,
        TeamSession(
            session_id="s1",
            team_id="eng",
            runs=[
                member("m0", "t0"),
                member("m0-child", "m0"),
                TeamRunOutput(run_id="t0", team_id="eng", session_id="s1", status=RunStatus.paused),
                member("orphan", "missing"),
                TeamRunOutput(run_id="t1", team_id="eng", session_id="s1", status=RunStatus.completed),
            ],
        ),
    )
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "t1"), summary="folded", scope_key="team:eng")

    session = get_team_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["m0", "m0-child", "t0"]


def test_a_run_without_an_index_loads_first(storage: SqliteDb, tmp_path: Path) -> None:
    """A row an old writer stored without an index has no place behind a summary, so it loads."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1"), _run("r2")]))
    _add_summary_row(tmp_path, after_run_index=_run_index(tmp_path, "r1"), summary="folded")
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute("UPDATE code_sessions_runs SET run_index = NULL WHERE run_id = 'r0'")

    session = get_agent_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r0", "r2"]


def test_load_finds_member_rows_without_scanning_the_session_per_run(storage: SqliteDb) -> None:
    """The descendant step searches a materialized child set, so a load stays linear in the session's rows."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))
    statements: list[tuple[str, tuple[object, ...]]] = []

    def capture(_conn: object, _cursor: object, statement: str, parameters: object, *_args: object) -> None:
        if "WITH RECURSIVE" in statement:
            statements.append((statement, tuple(parameters)))  # type: ignore[arg-type]

    event.listen(storage.db_engine, "before_cursor_execute", capture)
    try:
        get_agent_session(storage, "s1")
    finally:
        event.remove(storage.db_engine, "before_cursor_execute", capture)

    assert len(statements) == 1
    statement, parameters = statements[0]
    with storage.db_engine.connect() as connection:
        plan = [row[3] for row in connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)]
    step = plan[plan.index("RECURSIVE STEP") + 1 :]
    # The runs table is read once, to materialize the child set; each step only probes that set's index.
    assert "SEARCH children USING AUTOMATIC COVERING INDEX (parent_run_id=?)" in step
