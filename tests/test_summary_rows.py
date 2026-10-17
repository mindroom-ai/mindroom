"""Conversation reads through the summary rows of the run log."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from sqlalchemy import event

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.storage import read_chunk_basis, remove_runs_answering, save_working_run_changes
from mindroom.history.types import HistoryScope
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
    after: str,
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
            "SELECT ?, ?, run_index, ?, 'model', '[]', '[]', 1700000000 FROM code_sessions_runs WHERE run_id = ?",
            (session_id, scope_key, summary, after),
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
        _add_summary_row(tmp_path, after="r0", summary="earlier")
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
    _add_summary_row(tmp_path, after="r0", summary="first")
    _add_summary_row(tmp_path, after="r2", summary="second")

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
    _add_summary_row(tmp_path, after="m1", summary="team summary", scope_key="team:eng")

    session = get_team_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["m1", "t1"]


def test_barrier_row_hides_the_runs_behind_it(storage: SqliteDb, tmp_path: Path) -> None:
    """A summary row without text retires what it covers: the runs behind it stay hidden."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))
    _add_summary_row(tmp_path, after="r0", summary="retired")
    _add_summary_row(tmp_path, after="r0", summary=None)

    session = get_agent_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r1"]


def test_load_reads_dict_sessions_through_the_same_filter(storage: SqliteDb, tmp_path: Path) -> None:
    """``deserialize=False`` reads see the same runs as object reads."""
    seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=[_run("r0"), _run("r1")]))
    _add_summary_row(tmp_path, after="r0", summary="first")

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
    _add_summary_row(tmp_path, after="r2", summary="folded")

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
    _add_summary_row(tmp_path, after="t1", summary="folded", scope_key="team:eng")

    session = get_team_session(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["m0", "m0-child", "t0"]


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


# --- Summary rows written by compaction, edits, and redactions ---------------------------

_SCOPE = HistoryScope(kind="agent", scope_id="code")


def _event_run(run_id: str, *, status: RunStatus = RunStatus.completed, seen: tuple[str, ...] = ()) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="code",
        session_id="s1",
        status=status,
        messages=[Message(role="user", content=f"question {run_id}")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}", *seen],
        },
    )


def _seed_events(storage: SqliteDb, runs: list[RunOutput]) -> AgentSession:
    return seed_session(storage, AgentSession(session_id="s1", agent_id="code", runs=runs))


def _fold(storage: SqliteDb, run_ids: list[str], summary: str) -> int | None:
    latest = summary_rows.latest_row(storage, session_id="s1", scope_key=_SCOPE.key)
    return summary_rows.append_summary(
        storage,
        session_id="s1",
        scope_key=_SCOPE.key,
        based_on=latest.id if latest is not None else None,
        covered=summary_rows.covered_runs(storage, session_id="s1", run_ids=run_ids),
        summary=summary,
        summary_model="model",
    )


def _loaded(storage: SqliteDb) -> AgentSession:
    session = get_agent_session(storage, "s1")
    assert session is not None
    return session


def _loaded_ids(storage: SqliteDb) -> list[str]:
    return [run.run_id or "" for run in _loaded(storage).runs or []]


def _row_payloads(tmp_path: Path) -> list[tuple[str, int, str]]:
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        return connection.execute(
            "SELECT run_id, run_index, run_data FROM code_sessions_runs ORDER BY run_id",
        ).fetchall()


def test_compaction_moves_no_run_row(storage: SqliteDb, tmp_path: Path) -> None:
    """Each chunk appends a summary row; every run row keeps its index and payload, and replay starts after it."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(5)])
    before = _row_payloads(tmp_path)

    first = _fold(storage, ["r0", "r1"], "S1")
    second = _fold(storage, ["r2", "r3"], "S2")

    assert first is not None
    assert second is not None
    assert _row_payloads(tmp_path) == before
    session = _loaded(storage)
    assert [run.run_id for run in session.runs or []] == ["r4"]
    assert session.summary is not None
    assert session.summary.summary == "S2"


def test_load_derives_the_summary_from_the_latest_row(storage: SqliteDb, tmp_path: Path) -> None:
    """A summary written into the session row never replays; only summary rows do."""
    session = _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    session.summary = SessionSummary(summary="stale session-row summary")
    storage.upsert_session(session)
    assert _loaded(storage).summary is None

    _fold(storage, ["r0"], "S1")
    stale = _loaded(storage)
    stale.summary = SessionSummary(summary="stale session-row summary")
    storage.upsert_session(stale)
    raw = storage.get_session("s1", SessionType.AGENT, deserialize=False)

    assert _loaded(storage).summary.summary == "S1"  # type: ignore[union-attr]
    assert isinstance(raw, dict)
    assert raw["summary"]["summary"] == "S1"
    _add_summary_row(tmp_path, after="r0", summary=None)
    assert _loaded(storage).summary is None


@pytest.mark.parametrize("change", ["redaction", "retirement", "another compaction", "stale paused copy"])
def test_a_chunk_refuses_history_that_changed_after_the_session_loaded(storage: SqliteDb, change: str) -> None:
    """A chunk summarizes the session as it loaded, so a change that landed before its basis read voids it."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(4)])
    assert _fold(storage, ["r0"], "S1") is not None
    session = _loaded(storage)
    if change == "redaction":
        assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r2", match="any")
    elif change == "retirement":
        summary_rows.retire(storage, session_id="s1", scope_key=_SCOPE.key)
    elif change == "another compaction":
        assert _fold(storage, ["r1"], "S2 by another response") is not None
    else:
        storage.upsert_run(_event_run("r2", status=RunStatus.paused), session_id="s1")

    basis = read_chunk_basis(storage, session, _SCOPE, ["r1", "r2", "r3"], previous_summary="S1")

    assert basis is None


def test_a_chunk_builds_on_the_summary_it_loaded(storage: SqliteDb) -> None:
    """With nothing changed since the session loaded, the chunk reads its basis."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(3)])
    assert _fold(storage, ["r0"], "S1") is not None
    session = _loaded(storage)

    basis = read_chunk_basis(storage, session, _SCOPE, ["r1", "r2"], previous_summary="S1")

    assert basis is not None
    assert [run.run_id for run in basis.covered] == ["r1", "r2"]


def test_compaction_writes_back_only_the_runs_it_edited(storage: SqliteDb) -> None:
    """An approval that completes during a summary call keeps its result; compaction saves only runs it changed."""
    _seed_events(storage, [_event_run("r0"), _event_run("p1", status=RunStatus.paused), _event_run("r2")])
    session = _loaded(storage)
    loaded_runs = list(session.runs or [])
    working = deepcopy(session)
    edited = next(run for run in working.runs or [] if run.run_id == "r2")
    edited.messages = [Message(role="user", content="question r2, stripped")]
    storage.upsert_run(_event_run("p1"), session_id="s1")
    # The edited run's reply event is linked while the summary call runs.
    linked = _event_run("r2")
    linked.metadata = {**(linked.metadata or {}), constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply-r2"}
    storage.upsert_run(linked, session_id="s1")
    assert _fold(storage, ["r0"], "S1") is not None

    save_working_run_changes(storage, _loaded(storage), loaded_runs, working)

    stored = {run.run_id: run for run in _loaded(storage).runs or []}
    assert stored["p1"].status == RunStatus.completed
    assert [message.content for message in stored["r2"].messages or []] == ["question r2, stripped"]
    assert (stored["r2"].metadata or {})[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$reply-r2"


def test_append_never_sorts_behind_the_latest_row(storage: SqliteDb) -> None:
    """A chunk whose runs another compaction folded meanwhile writes nothing, even when that summary reads the same."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(4)])
    assert _fold(storage, ["r0"], "S") is not None
    covered = summary_rows.covered_runs(storage, session_id="s1", run_ids=["r1"])
    assert _fold(storage, ["r1", "r2", "r3"], "S") is not None
    latest = summary_rows.latest_row(storage, session_id="s1", scope_key=_SCOPE.key)
    assert latest is not None

    raced = summary_rows.append_summary(
        storage,
        session_id="s1",
        scope_key=_SCOPE.key,
        based_on=latest.id,
        covered=covered,
        summary="S and r1",
        summary_model="model",
    )

    assert raced is None
    assert _loaded_ids(storage) == []


def test_append_refuses_a_stale_base(storage: SqliteDb) -> None:
    """A chunk built on a summary another append replaced writes nothing."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    covered = summary_rows.covered_runs(storage, session_id="s1", run_ids=["r1"])
    assert _fold(storage, ["r0"], "S1") is not None

    raced = summary_rows.append_summary(
        storage,
        session_id="s1",
        scope_key=_SCOPE.key,
        based_on=None,
        covered=covered,
        summary="built without S1",
        summary_model="model",
    )

    assert raced is None
    assert _loaded(storage).summary.summary == "S1"  # type: ignore[union-attr]


@pytest.mark.parametrize("change", ["stale paused copy", "moved past a new run"])
def test_append_refuses_a_covered_run_changed_during_the_summary_call(storage: SqliteDb, change: str) -> None:
    """The summary covers what was read; a covered run that changed status or place since then voids it."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    covered = summary_rows.covered_runs(storage, session_id="s1", run_ids=["r0"])
    storage.upsert_run(_event_run("r0", status=RunStatus.paused), session_id="s1")
    if change == "moved past a new run":
        storage.upsert_run(_event_run("r2"), session_id="s1")
        storage.upsert_run(_event_run("r0"), session_id="s1")

    appended = summary_rows.append_summary(
        storage,
        session_id="s1",
        scope_key=_SCOPE.key,
        based_on=None,
        covered=covered,
        summary="S1",
        summary_model="model",
    )

    assert appended is None
    assert _loaded(storage).summary is None


def test_append_reads_event_sets_from_the_stored_rows(storage: SqliteDb) -> None:
    """A response event id linked while the summary call ran still counts as seen behind the summary."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    covered = summary_rows.covered_runs(storage, session_id="s1", run_ids=["r0"])
    linked = _event_run("r0")
    assert linked.metadata is not None
    linked.metadata[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = "$reply-r0"
    storage.upsert_run(linked, session_id="s1")

    assert (
        summary_rows.append_summary(
            storage,
            session_id="s1",
            scope_key=_SCOPE.key,
            based_on=None,
            covered=covered,
            summary="S1",
            summary_model="model",
        )
        is not None
    )
    assert summary_rows.history_event_ids(storage, session_id="s1", scope_key=_SCOPE.key, kind="seen") == {
        "$r0",
        "$reply-r0",
    }


def test_redacting_a_compacted_run_restores_the_runs_before_it(storage: SqliteDb, tmp_path: Path) -> None:
    """A cut removes the run holding the event and everything after it; earlier folded runs replay again."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(5)])
    _fold(storage, ["r0", "r1"], "S1")
    _fold(storage, ["r2", "r3"], "S2")

    result = summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r3", match="any")

    assert result is not None
    session = _loaded(storage)
    assert [run.run_id for run in session.runs or []] == ["r2"]
    assert session.summary.summary == "S1"  # type: ignore[union-attr]
    storage.upsert_run(_event_run("r3"), session_id="s1")
    assert _loaded_ids(storage) == ["r2"]
    assert "r3" not in {run_id for run_id, _index, _data in _row_payloads(tmp_path)}


def test_a_pending_approval_before_the_cut_survives(storage: SqliteDb) -> None:
    """A run paused before the redacted turn did not see it and stays resumable."""
    _seed_events(
        storage,
        [_event_run("p0", status=RunStatus.paused), _event_run("r1"), _event_run("r2"), _event_run("r3")],
    )
    _fold(storage, ["r1", "r2"], "S1")

    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r2", match="any")

    session = _loaded(storage)
    assert [run.run_id for run in session.runs or []] == ["p0", "r1"]
    assert session.summary is None


def test_edit_cuts_a_summary_whose_range_answered_the_event(storage: SqliteDb) -> None:
    """Editing a message a folded run answered removes that run, its summary, and what followed."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1"), _event_run("r2")])
    _fold(storage, ["r0", "r1"], "S1")

    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r1", match="answered")

    session = _loaded(storage)
    assert [run.run_id for run in session.runs or []] == ["r0"]
    assert session.summary is None


def test_edit_of_a_message_a_range_only_read_cuts_nothing_there(storage: SqliteDb) -> None:
    """An edit cuts what answered the message; a redaction also cuts what only read it."""
    _seed_events(storage, [_event_run("r0", seen=("$context",)), _event_run("r1")])
    _fold(storage, ["r0"], "S1")

    assert (
        summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$context", match="answered")
        is None
    )
    assert _loaded(storage).summary.summary == "S1"  # type: ignore[union-attr]
    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$context", match="any")
    assert _loaded_ids(storage) == []


def test_edit_keeps_a_legacy_summary_in_force(storage: SqliteDb, tmp_path: Path) -> None:
    """An edit cuts what answered the message and never clears or retires a legacy summary, even one that may contain it."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "INSERT INTO code_sessions_summaries "
            "(session_id, scope_key, after_run_index, summary, legacy, event_ids, seen_event_ids, created_at) "
            "VALUES ('s1', 'agent:code', -1, 'legacy summary', 1, '[\"$r1\"]', '[\"$r1\"]', 1700000000)",
        )

    assert remove_runs_answering(storage, "s1", _SCOPE, "$r1")

    assert _loaded(storage).summary.summary == "legacy summary"  # type: ignore[union-attr]
    assert _loaded_ids(storage) == ["r0"]
    assert summary_rows.legacy_event_ids(storage, session_id="s1", scope_key=_SCOPE.key) == {"$r1"}


def test_edit_removes_following_runs_even_without_metadata(storage: SqliteDb) -> None:
    """Everything after the run that answered the edited message goes, including runs with no metadata."""
    runs = [
        RunOutput(run_id="before", agent_id="code", session_id="s1", metadata={"matrix_event_id": "$before"}),
        RunOutput(run_id="matched", agent_id="code", session_id="s1", metadata={"matrix_event_id": "$target"}),
        RunOutput(run_id="bare-after", agent_id="code", session_id="s1"),
        RunOutput(run_id="after", agent_id="code", session_id="s1", metadata={"matrix_event_id": "$after"}),
    ]
    _seed_events(storage, runs)

    assert remove_runs_answering(storage, "s1", _SCOPE, "$target")

    assert _loaded_ids(storage) == ["before"]


def test_edit_removes_a_team_run_with_its_members(storage: SqliteDb) -> None:
    """Team edit regeneration deletes the stale team run and its member rows from the team session."""
    seed_session(
        storage,
        TeamSession(
            session_id="s1",
            team_id="eng",
            runs=[
                TeamRunOutput(
                    run_id="original",
                    team_id="eng",
                    session_id="s1",
                    metadata={"matrix_event_id": "$original"},
                ),
                RunOutput(run_id="member", agent_id="helper", session_id="s1", parent_run_id="original"),
                TeamRunOutput(run_id="other", team_id="eng", session_id="s1", metadata={"matrix_event_id": "$other"}),
            ],
        ),
    )

    assert remove_runs_answering(storage, "s1", HistoryScope(kind="team", scope_id="eng"), "$original")

    session = get_team_session(storage, "s1")
    assert session is not None
    assert session.runs == []


@pytest.mark.parametrize(
    ("metadata_key", "edited"),
    [
        (constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, "$first"),
        (constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY, "$selection"),
    ],
    ids=["coalesced-source", "discovery-alias"],
)
def test_edit_matches_every_event_a_run_answered(storage: SqliteDb, metadata_key: str, edited: str) -> None:
    """A coalesced batch member or an interactive discovery alias identifies the run that answered it."""
    run = RunOutput(
        run_id="answer",
        agent_id="code",
        session_id="s1",
        metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$primary", metadata_key: [edited, "$primary"]},
    )
    _seed_events(storage, [run])

    assert remove_runs_answering(storage, "s1", _SCOPE, edited)

    assert _loaded_ids(storage) == []
