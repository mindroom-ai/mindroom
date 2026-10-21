"""Conversation reads through the summary rows of the run log."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.storage import read_chunk_basis, remove_runs_answering, save_working_run_changes
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore

_SESSION_ROW = ConversationSession(kind="agent", session_id="s1", owner_id="code")


def _open(tmp_path: Path) -> ConversationStore:
    return create_state_storage("code", tmp_path, session_table="code_sessions")


def _save(storage: ConversationStore, run: RunRecord) -> None:
    storage.save_run(_SESSION_ROW, run, record_usage=True)


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "code.db"


def _run(run_id: str, *, status: RunStatus = RunStatus.completed) -> RunRecord:
    return RunRecord(
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
def storage(tmp_path: Path) -> ConversationStore:
    """Open a fresh conversation database."""
    return _open(tmp_path)


def test_fresh_storage_has_summary_tables(tmp_path: Path) -> None:
    """Every conversation database opens with both summary-log tables, which cascade from the session row."""
    storage = _open(tmp_path)
    seed_session(storage, ConversationSession(kind="agent", session_id="s1", owner_id="code", runs=[_run("r0")]))
    _add_summary_row(tmp_path, after="r0", summary="earlier")
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute("INSERT INTO code_sessions_removed_runs (session_id, run_id) VALUES ('s1', 'gone')")
    with storage.transaction(write=True) as connection:
        connection.execute("DELETE FROM code_sessions WHERE session_id = 's1'")
    _open(tmp_path)

    assert {"code_sessions_summaries", "code_sessions_removed_runs"} <= _tables(tmp_path)
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_sessions_summaries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM code_sessions_removed_runs").fetchone() == (0,)


def test_load_returns_runs_after_the_latest_summary_and_every_non_visible_run(
    storage: ConversationStore,
    tmp_path: Path,
) -> None:
    """A summary row hides the visible runs it covers; paused runs and later runs still load."""
    runs = [_run("r0"), _run("r1", status=RunStatus.paused), _run("r2"), _run("r3"), _run("r4")]
    seed_session(storage, ConversationSession(kind="agent", session_id="s1", owner_id="code", runs=runs))
    _add_summary_row(tmp_path, after="r0", summary="first")
    _add_summary_row(tmp_path, after="r2", summary="second")

    session = storage.load_session("s1", "agent")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r1", "r3", "r4"]


def test_load_without_summary_rows_returns_every_run(storage: ConversationStore) -> None:
    """A conversation that never compacted reads exactly as before."""
    seed_session(
        storage,
        ConversationSession(kind="agent", session_id="s1", owner_id="code", runs=[_run("r0"), _run("r1")]),
    )

    session = storage.load_session("s1", "agent")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r0", "r1"]


def test_load_keeps_member_rows_with_their_team_run(storage: ConversationStore, tmp_path: Path) -> None:
    """Member rows load with their team run wherever their index sits, and stay hidden with a folded one."""

    def member(run_id: str, parent: str) -> RunRecord:
        return RunRecord(run_id=run_id, agent_id="helper", session_id="s1", parent_run_id=parent)

    def team_run(run_id: str) -> TeamRunRecord:
        return TeamRunRecord(run_id=run_id, team_id="eng", session_id="s1", status=RunStatus.completed)

    seed_session(
        storage,
        ConversationSession(
            kind="team",
            session_id="s1",
            owner_id="eng",
            runs=[member("m0", "t0"), team_run("t0"), member("m1", "t1"), team_run("t1")],
        ),
    )
    _add_summary_row(tmp_path, after="m1", summary="team summary", scope_key="team:eng")

    session = storage.load_session("s1", "team")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["m1", "t1"]


def test_barrier_row_hides_the_runs_behind_it(storage: ConversationStore, tmp_path: Path) -> None:
    """A summary row without text retires what it covers: the runs behind it stay hidden."""
    seed_session(
        storage,
        ConversationSession(kind="agent", session_id="s1", owner_id="code", runs=[_run("r0"), _run("r1")]),
    )
    _add_summary_row(tmp_path, after="r0", summary="retired")
    _add_summary_row(tmp_path, after="r0", summary=None)

    session = storage.load_session("s1", "agent")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["r1"]


# --- Summary rows written by compaction, edits, and redactions ---------------------------

_SCOPE = HistoryScope(kind="agent", scope_id="code")


def _event_run(run_id: str, *, status: RunStatus = RunStatus.completed, seen: tuple[str, ...] = ()) -> RunRecord:
    return RunRecord(
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


def _seed_events(storage: ConversationStore, runs: list[RunRecord]) -> ConversationSession:
    return seed_session(storage, ConversationSession(kind="agent", session_id="s1", owner_id="code", runs=runs))


def _fold(storage: ConversationStore, run_ids: list[str], summary: str) -> int | None:
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


def _loaded(storage: ConversationStore) -> ConversationSession:
    session = storage.load_session("s1", "agent")
    assert session is not None
    return session


def _loaded_ids(storage: ConversationStore) -> list[str]:
    return [run.run_id or "" for run in _loaded(storage).runs or []]


def _row_payloads(tmp_path: Path) -> list[tuple[str, int, str]]:
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        return connection.execute(
            "SELECT run_id, run_index, run_data FROM code_sessions_runs ORDER BY run_id",
        ).fetchall()


def test_compaction_moves_no_run_row(storage: ConversationStore, tmp_path: Path) -> None:
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
    assert session.summary.text == "S2"


def test_load_derives_the_summary_from_the_latest_row(storage: ConversationStore, tmp_path: Path) -> None:
    """A summary an older release wrote into the session row never replays; only summary rows do."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE code_sessions SET summary = ? WHERE session_id = 's1'",
            ('{"summary": "stale session-row summary"}',),
        )
    assert _loaded(storage).summary is None

    _fold(storage, ["r0"], "S1")

    assert _loaded(storage).summary.text == "S1"  # type: ignore[union-attr]
    _add_summary_row(tmp_path, after="r0", summary=None)
    assert _loaded(storage).summary is None


@pytest.mark.parametrize("change", ["redaction", "retirement", "another compaction", "stale paused copy"])
def test_a_chunk_refuses_history_that_changed_after_the_session_loaded(storage: ConversationStore, change: str) -> None:
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
        _save(storage, _event_run("r2", status=RunStatus.paused))

    basis = read_chunk_basis(storage, session, _SCOPE, ["r1", "r2", "r3"], previous_summary="S1")

    assert basis is None


def test_a_chunk_builds_on_the_summary_it_loaded(storage: ConversationStore) -> None:
    """With nothing changed since the session loaded, the chunk reads its basis."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(3)])
    assert _fold(storage, ["r0"], "S1") is not None
    session = _loaded(storage)

    basis = read_chunk_basis(storage, session, _SCOPE, ["r1", "r2"], previous_summary="S1")

    assert basis is not None
    assert [run.run_id for run in basis.covered] == ["r1", "r2"]


def test_compaction_writes_back_only_the_runs_it_edited(storage: ConversationStore) -> None:
    """An approval that completes during a summary call keeps its result; compaction saves only runs it changed."""
    _seed_events(storage, [_event_run("r0"), _event_run("p1", status=RunStatus.paused), _event_run("r2")])
    session = _loaded(storage)
    loaded_runs = list(session.runs or [])
    working = deepcopy(session)
    edited = next(run for run in working.runs or [] if run.run_id == "r2")
    edited.messages = [Message(role="user", content="question r2, stripped")]
    _save(storage, _event_run("p1"))
    # The edited run's reply event is linked while the summary call runs.
    linked = _event_run("r2")
    linked.metadata = {**(linked.metadata or {}), constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply-r2"}
    _save(storage, linked)
    assert _fold(storage, ["r0"], "S1") is not None

    save_working_run_changes(storage, _loaded(storage), loaded_runs, working)

    stored = {run.run_id: run for run in _loaded(storage).runs or []}
    assert stored["p1"].status == RunStatus.completed
    assert [message.content for message in stored["r2"].messages or []] == ["question r2, stripped"]
    assert (stored["r2"].metadata or {})[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$reply-r2"


def test_append_never_sorts_behind_the_latest_row(storage: ConversationStore) -> None:
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


def test_append_refuses_a_stale_base(storage: ConversationStore) -> None:
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
    assert _loaded(storage).summary.text == "S1"  # type: ignore[union-attr]


@pytest.mark.parametrize("change", ["stale paused copy", "moved past a new run"])
def test_append_refuses_a_covered_run_changed_during_the_summary_call(storage: ConversationStore, change: str) -> None:
    """The summary covers what was read; a covered run that changed status or place since then voids it."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    covered = summary_rows.covered_runs(storage, session_id="s1", run_ids=["r0"])
    _save(storage, _event_run("r0", status=RunStatus.paused))
    if change == "moved past a new run":
        _save(storage, _event_run("r2"))
        _save(storage, _event_run("r0"))

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


def test_append_reads_event_sets_from_the_stored_rows(storage: ConversationStore) -> None:
    """A response event id linked while the summary call ran still counts as seen behind the summary."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    covered = summary_rows.covered_runs(storage, session_id="s1", run_ids=["r0"])
    linked = _event_run("r0")
    assert linked.metadata is not None
    linked.metadata[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = "$reply-r0"
    _save(storage, linked)

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


def test_redacting_a_compacted_run_restores_the_runs_before_it(storage: ConversationStore, tmp_path: Path) -> None:
    """A cut removes the run holding the event and everything after it; earlier folded runs replay again."""
    _seed_events(storage, [_event_run(f"r{index}") for index in range(5)])
    _fold(storage, ["r0", "r1"], "S1")
    _fold(storage, ["r2", "r3"], "S2")

    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r3", match="any")

    session = _loaded(storage)
    assert [run.run_id for run in session.runs or []] == ["r2"]
    assert session.summary.text == "S1"  # type: ignore[union-attr]
    _save(storage, _event_run("r3"))
    assert _loaded_ids(storage) == ["r2"]
    assert "r3" not in {run_id for run_id, _index, _data in _row_payloads(tmp_path)}


def test_a_pending_approval_before_the_cut_survives(storage: ConversationStore) -> None:
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


def test_edit_cuts_a_summary_whose_range_answered_the_event(storage: ConversationStore) -> None:
    """Editing a message a folded run answered removes that run, its summary, and what followed."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1"), _event_run("r2")])
    _fold(storage, ["r0", "r1"], "S1")

    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r1", match="answered")

    session = _loaded(storage)
    assert [run.run_id for run in session.runs or []] == ["r0"]
    assert session.summary is None


def test_edit_of_a_message_a_range_only_read_cuts_nothing_there(storage: ConversationStore) -> None:
    """An edit cuts what answered the message; a redaction also cuts what only read it."""
    _seed_events(storage, [_event_run("r0", seen=("$context",)), _event_run("r1")])
    _fold(storage, ["r0"], "S1")

    assert not summary_rows.cut_at_event(
        storage,
        session_id="s1",
        scope_key=_SCOPE.key,
        event_id="$context",
        match="answered",
    )
    assert _loaded(storage).summary.text == "S1"  # type: ignore[union-attr]
    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$context", match="any")
    assert _loaded_ids(storage) == []


def test_edit_keeps_a_legacy_summary_in_force(storage: ConversationStore, tmp_path: Path) -> None:
    """An edit cuts what answered the message and never clears or retires a legacy summary, even one that may contain it."""
    _seed_events(storage, [_event_run("r0"), _event_run("r1")])
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "INSERT INTO code_sessions_summaries "
            "(session_id, scope_key, after_run_index, summary, legacy, event_ids, seen_event_ids, created_at) "
            "VALUES ('s1', 'agent:code', -1, 'legacy summary', 1, '[\"$r1\"]', '[\"$r1\"]', 1700000000)",
        )

    assert remove_runs_answering(storage, "s1", _SCOPE, "$r1")

    assert _loaded(storage).summary.text == "legacy summary"  # type: ignore[union-attr]
    assert _loaded_ids(storage) == ["r0"]
    assert summary_rows.legacy_event_ids(storage, session_id="s1", scope_key=_SCOPE.key) == {"$r1"}


def test_edit_removes_following_runs_even_without_metadata(storage: ConversationStore) -> None:
    """Everything after the run that answered the edited message goes, including runs with no metadata."""
    runs = [
        RunRecord(run_id="before", agent_id="code", session_id="s1", metadata={"matrix_event_id": "$before"}),
        RunRecord(run_id="matched", agent_id="code", session_id="s1", metadata={"matrix_event_id": "$target"}),
        RunRecord(run_id="bare-after", agent_id="code", session_id="s1"),
        RunRecord(run_id="after", agent_id="code", session_id="s1", metadata={"matrix_event_id": "$after"}),
    ]
    _seed_events(storage, runs)

    assert remove_runs_answering(storage, "s1", _SCOPE, "$target")

    assert _loaded_ids(storage) == ["before"]


def test_edit_removes_a_team_run_with_its_members(storage: ConversationStore) -> None:
    """Team edit regeneration deletes the stale team run and its member rows from the team session."""
    seed_session(
        storage,
        ConversationSession(
            kind="team",
            session_id="s1",
            owner_id="eng",
            runs=[
                TeamRunRecord(
                    run_id="original",
                    team_id="eng",
                    session_id="s1",
                    metadata={"matrix_event_id": "$original"},
                ),
                RunRecord(run_id="member", agent_id="helper", session_id="s1", parent_run_id="original"),
                TeamRunRecord(run_id="other", team_id="eng", session_id="s1", metadata={"matrix_event_id": "$other"}),
            ],
        ),
    )

    assert remove_runs_answering(storage, "s1", HistoryScope(kind="team", scope_id="eng"), "$original")

    session = storage.load_session("s1", "team")
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
def test_edit_matches_every_event_a_run_answered(storage: ConversationStore, metadata_key: str, edited: str) -> None:
    """A coalesced batch member or an interactive discovery alias identifies the run that answered it."""
    run = RunRecord(
        run_id="answer",
        agent_id="code",
        session_id="s1",
        metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$primary", metadata_key: [edited, "$primary"]},
    )
    _seed_events(storage, [run])

    assert remove_runs_answering(storage, "s1", _SCOPE, edited)

    assert _loaded_ids(storage) == []
