"""Summary rows of the conversation log: loads, appends and their compare, folds, cuts, retirement, and legacy summaries."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.storage import read_chunk_basis, strip_kept_replay_fields
from mindroom.history.summary_rows import CoveredRun, Fold
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session_row

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import SaveOutcome
    from mindroom.session_storage.types import SessionKind

_AGENT = "agent:code"
_TEAM = "team:crew"
_SCOPE = HistoryScope(kind="agent", scope_id="code")
_NON_VISIBLE = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}


def _session(kind: SessionKind = "agent") -> ConversationSession:
    return ConversationSession(session_id="s1", kind=kind, owner_id="crew" if kind == "team" else "code")


def _run(
    run_id: str,
    status: RunStatus = RunStatus.completed,
    *,
    seen: Sequence[str] = (),
    answered: Sequence[str] = (),
    messages: Sequence[Message] | None = None,
    agent_id: str = "code",
    parent: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        session_id="s1",
        parent_run_id=parent,
        status=status,
        messages=list(messages) if messages is not None else [Message(role="user", content=f"question {run_id}")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: [f"${run_id}", *answered],
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}", *seen],
        },
    )


def _team_run(run_id: str, status: RunStatus = RunStatus.completed, **fields: Any) -> TeamRunRecord:  # noqa: ANN401
    return TeamRunRecord(
        run_id=run_id,
        team_id="crew",
        session_id="s1",
        status=status,
        metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}"},
        **fields,
    )


def _save(store: ConversationStore, *runs: AnyRunRecord, kind: SessionKind = "agent") -> list[SaveOutcome]:
    return [store.save_run(_session(kind), run, record_usage=False) for run in runs]


def _latest_id(store: ConversationStore, scope_key: str = _AGENT) -> int | None:
    latest = summary_rows.latest_row(store, session_id="s1", scope_key=scope_key)
    return latest.id if latest is not None else None


def _chunk(store: ConversationStore, run_ids: list[str], summary: str, scope_key: str = _AGENT) -> int | None:
    """Append a pre-reply compaction chunk folding ``run_ids`` onto the latest row, as compaction commits one."""
    return summary_rows.append_summary(
        store,
        session_id="s1",
        scope_key=scope_key,
        based_on=_latest_id(store, scope_key),
        covered=summary_rows.covered_runs(store, session_id="s1", run_ids=run_ids),
        summary=summary,
    )


def _basis(store: ConversationStore, scope_key: str) -> tuple[int | None, list[CoveredRun]]:
    """Read a fold's basis: the latest row and the visible top-level runs a load returns after it."""
    loaded = store.load_session("s1", "team" if scope_key == _TEAM else "agent")
    run_ids = [
        run.run_id
        for run in (loaded.runs if loaded is not None else [])
        if run.parent_run_id is None and run.status not in _NON_VISIBLE
    ]
    return _latest_id(store, scope_key), summary_rows.covered_runs(store, session_id="s1", run_ids=run_ids)


def _fold(
    store: ConversationStore,
    run: AnyRunRecord,
    summary: str,
    *,
    folded: Sequence[Message] = (),
    members: Sequence[str] = (),
    basis: tuple[int | None, list[CoveredRun]] | None = None,
) -> int | None:
    """Fold the stored runs a load replays and part of the running ``run``, as a mid-turn fold commits."""
    scope_key = _TEAM if isinstance(run, TeamRunRecord) else _AGENT
    based_on, covered = basis or _basis(store, scope_key)
    return summary_rows.append_summary(
        store,
        session_id="s1",
        scope_key=scope_key,
        based_on=based_on,
        covered=covered,
        summary=summary,
        fold=Fold(run=run, messages=tuple(folded), member_run_ids=frozenset(members)),
    )


def _query(store: ConversationStore, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(store.path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _execute(store: ConversationStore, sql: str, *parameters: object) -> None:
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(sql, parameters)


def _log(store: ConversationStore, scope_key: str = _AGENT) -> list[str]:
    """Return the scope's top-level runs and summary rows in log order, as ``run:<id>`` and ``row:<summary>``."""
    entries = [
        ((seq, 0), f"run:{run_id}")
        for run_id, seq in _query(
            store,
            "SELECT run_id, seq FROM code_sessions_log_runs WHERE session_id = 's1' AND parent_run_id IS NULL "
            "AND scope_key = ?",
            scope_key,
        )
    ]
    entries += [
        ((through, row_id), f"row:{summary}")
        for row_id, through, summary in _query(
            store,
            "SELECT id, through, summary FROM code_sessions_log_summaries WHERE session_id = 's1' AND scope_key = ?",
            scope_key,
        )
    ]
    return [label for _key, label in sorted(entries)]


def _keys(store: ConversationStore) -> dict[str, tuple[int, int]]:
    """Return the key of every run and summary row of the agent scope, by run id or summary text."""
    runs = _query(store, "SELECT run_id, seq, 0 FROM code_sessions_log_runs WHERE scope_key = ?", _AGENT)
    rows = _query(store, "SELECT summary, through, id FROM code_sessions_log_summaries WHERE scope_key = ?", _AGENT)
    return {name: (seq, row_id) for name, seq, row_id in [*runs, *rows]}


def _loaded(store: ConversationStore, kind: SessionKind = "agent") -> ConversationSession:
    session = store.load_session("s1", kind)
    assert session is not None
    return session


def _loaded_ids(store: ConversationStore, kind: SessionKind = "agent") -> list[str]:
    return [run.run_id for run in _loaded(store, kind).runs]


def _replayed(store: ConversationStore) -> str | None:
    summary = _loaded(store).summary
    return summary.text if summary is not None else None


def _edit(store: ConversationStore, event_id: str, scope_key: str = _AGENT) -> bool:
    return summary_rows.cut_at_event(store, session_id="s1", scope_key=scope_key, event_id=event_id, match="answered")


def _redact(store: ConversationStore, event_id: str, scope_key: str = _AGENT) -> bool:
    """Cut what a redacted event built, deciding about a legacy summary in force as redaction cleanup does."""
    session = _session("team" if scope_key == _TEAM else "agent")
    legacy = summary_rows.legacy_event_ids(store, session_id="s1", scope_key=scope_key)
    if legacy is not None and event_id in legacy:
        summary_rows.cut_at_legacy_row(store, session=session, scope_key=scope_key)
        return True
    if not summary_rows.cut_at_event(store, session_id="s1", scope_key=scope_key, event_id=event_id, match="any"):
        return False
    if legacy is not None:
        summary_rows.retire(store, session=session, scope_key=scope_key)
    return True


def _removed(store: ConversationStore) -> set[str]:
    return {run_id for (run_id,) in _query(store, "SELECT run_id FROM code_sessions_log_removed")}


def _seed_legacy_row(store: ConversationStore, summary: str, event_ids: Sequence[str]) -> None:
    """Store a legacy summary row as a conversion adopts one, sorting before every run."""
    seed_session_row(store, _session())
    _execute(
        store,
        "INSERT INTO code_sessions_log_summaries (session_id, scope_key, through, summary, legacy, event_ids, "
        "seen_event_ids, created_at) VALUES ('s1', ?, 0, ?, 1, ?, ?, 1700000000)",
        _AGENT,
        summary,
        json.dumps(list(event_ids)),
        json.dumps(list(event_ids)),
    )


def _call(call_id: str) -> Message:
    return Message(role="assistant", tool_calls=[{"id": call_id, "type": "function", "function": {"name": "probe"}}])


def _result(call_id: str) -> Message:
    return Message(role="tool", tool_call_id=call_id, content=f"{call_id} output")


# --- Loads ---------------------------------------------------------------------------------------


def test_fresh_storage_has_summary_tables(store: ConversationStore, tmp_path: Path) -> None:
    """Every conversation database opens with the summary-row and removed-run tables, which cascade from the session row."""
    _save(store, _run("r0"))
    assert _chunk(store, ["r0"], "earlier") is not None
    _execute(store, "INSERT INTO code_sessions_log_removed (session_id, run_id) VALUES ('s1', 'gone')")

    _execute(store, "DELETE FROM code_sessions WHERE session_id = 's1'")
    create_state_storage("code", tmp_path)

    tables = {name for (name,) in _query(store, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"code_sessions_log_summaries", "code_sessions_log_removed"} <= tables
    assert _query(store, "SELECT COUNT(*) FROM code_sessions_log_summaries") == [(0,)]
    assert _query(store, "SELECT COUNT(*) FROM code_sessions_log_removed") == [(0,)]


def test_load_returns_runs_after_the_latest_summary_and_every_non_visible_run(store: ConversationStore) -> None:
    """A summary row hides the visible runs it covers; paused runs and later runs still load."""
    _save(store, _run("r0"), _run("r1", RunStatus.paused), _run("r2"), _run("r3"), _run("r4"))
    assert _chunk(store, ["r0"], "first") is not None
    assert _chunk(store, ["r2"], "second") is not None

    assert _loaded_ids(store) == ["r1", "r3", "r4"]


def test_load_without_summary_rows_returns_every_run(store: ConversationStore) -> None:
    """A conversation that never compacted loads every run."""
    _save(store, _run("r0"), _run("r1"))

    assert _loaded_ids(store) == ["r0", "r1"]


def test_load_keeps_member_rows_with_their_team_run(store: ConversationStore) -> None:
    """Member runs load with their team run wherever their ``seq`` sits, and stay hidden with a covered one."""
    _save(
        store,
        _run("m0", parent="t0", agent_id="helper"),
        _team_run("t0"),
        _run("m1", parent="t1", agent_id="helper"),
        _team_run("t1"),
        kind="team",
    )
    assert _chunk(store, ["t0"], "team summary", _TEAM) is not None

    assert _loaded_ids(store, "team") == ["m1", "t1"]


# --- Appends and their compare -------------------------------------------------------------------


def test_a_chunk_covers_its_runs_without_changing_any_run_row(store: ConversationStore) -> None:
    """Each chunk appends a row through its newest run; run rows stay as they were and replay starts after it."""
    _save(store, *(_run(f"r{index}") for index in range(5)))
    before = _query(store, "SELECT * FROM code_sessions_log_runs ORDER BY run_id")

    assert _chunk(store, ["r0", "r1"], "S1") is not None
    assert _chunk(store, ["r3", "r2"], "S2") is not None

    assert _query(store, "SELECT * FROM code_sessions_log_runs ORDER BY run_id") == before
    assert _query(store, "SELECT through FROM code_sessions_log_summaries ORDER BY id") == [(2,), (4,)]
    assert _loaded_ids(store) == ["r4"]
    assert _replayed(store) == "S2"


def test_covered_runs_read_seq_and_status_in_log_order(store: ConversationStore) -> None:
    """A basis reads where the runs sort and whether they replay, in ``seq`` order, and nothing of another session."""
    _save(store, _run("r0"), _run("p1", RunStatus.paused), _run("r2"))

    covered = summary_rows.covered_runs(store, session_id="s1", run_ids=["r2", "p1", "r0", "missing"])

    assert covered == [
        CoveredRun("r0", 1, RunStatus.completed.value),
        CoveredRun("p1", 2, RunStatus.paused.value),
        CoveredRun("r2", 3, RunStatus.completed.value),
    ]
    assert [run.visible for run in covered] == [True, False, True]
    assert summary_rows.covered_runs(store, session_id="s2", run_ids=["r0"]) == []


@pytest.mark.parametrize(
    "change",
    ["stale base", "covered behind the latest row", "covered run gone", "covered run saved again", "status"],
)
def test_an_append_refuses_input_that_changed_since_it_was_read(store: ConversationStore, change: str) -> None:
    """A summary covers what its basis read; a newer row, a folded run, or a deleted or changed run voids it."""
    _save(store, _run("r0"), _run("r1"), _run("r2"))
    covered = summary_rows.covered_runs(store, session_id="s1", run_ids=["r1"])
    based_on = None
    if change == "stale base":
        assert _chunk(store, ["r0"], "another response's chunk") is not None
    elif change == "covered behind the latest row":
        assert _chunk(store, ["r0", "r1"], "another response's chunk") is not None
        based_on = _latest_id(store)
    elif change == "covered run gone":
        assert store.delete_runs(["r1"], session_id="s1") == {"r1"}
    elif change == "covered run saved again":
        # A discard fences nothing, so a late save stores the run again, at a new place.
        assert store.delete_runs(["r1"], session_id="s1") == {"r1"}
        assert _save(store, _run("r1")) == ["written"]
    else:
        interrupted = _run("r1", RunStatus.cancelled)
        assert store.save_interrupted_record(_session(), interrupted) == "written"
    rows_before = _query(store, "SELECT id, summary FROM code_sessions_log_summaries")

    appended = summary_rows.append_summary(
        store,
        session_id="s1",
        scope_key=_AGENT,
        based_on=based_on,
        covered=covered,
        summary="stale",
    )

    assert appended is None
    assert _query(store, "SELECT id, summary FROM code_sessions_log_summaries") == rows_before


@pytest.mark.parametrize("change", ["redaction", "retirement", "another compaction", "interrupted record"])
def test_a_chunk_refuses_history_that_changed_after_the_session_loaded(store: ConversationStore, change: str) -> None:
    """A chunk summarizes the session as it loaded, so a change that landed before its basis read voids it."""
    _save(store, *(_run(f"r{index}") for index in range(4)))
    assert _chunk(store, ["r0"], "S1") is not None
    session = _loaded(store)
    if change == "redaction":
        assert _redact(store, "$r2")
    elif change == "retirement":
        summary_rows.retire(store, session=_session(), scope_key=_AGENT)
    elif change == "another compaction":
        assert _chunk(store, ["r1"], "S2 by another response") is not None
    else:
        assert store.save_interrupted_record(_session(), _run("r2", RunStatus.cancelled)) == "written"

    assert read_chunk_basis(store, session, _SCOPE, ["r1", "r2", "r3"], previous_summary="S1") is None


def test_a_chunk_builds_on_the_summary_it_loaded(store: ConversationStore) -> None:
    """With nothing changed since the session loaded, the chunk reads its basis."""
    _save(store, *(_run(f"r{index}") for index in range(3)))
    assert _chunk(store, ["r0"], "S1") is not None
    session = _loaded(store)

    basis = read_chunk_basis(store, session, _SCOPE, ["r1", "r2"], previous_summary="S1")

    assert basis is not None
    assert [run.run_id for run in basis.covered] == ["r1", "r2"]


def test_the_replay_field_strip_keeps_what_another_writer_stored_meanwhile(store: ConversationStore) -> None:
    """An approval that completed and a reply linked during a summary call stay; compaction's strip changes messages."""
    answer = Message(
        role="assistant",
        content="answer r2",
        provider_data={"signature": "sig", "keep": "yes"},
        reasoning_content="thinking",
    )
    _save(store, _run("r0"), _run("p1", RunStatus.paused), _run("r2", messages=[Message(role="user"), answer]))
    session = _loaded(store)
    assert _save(store, _run("p1")) == ["written"]
    assert store.patch_run_metadata("s1", "r2", {constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply-r2"})
    assert _chunk(store, ["r0"], "S1") is not None

    strip_kept_replay_fields(store, session=session, scope=_SCOPE)

    stored = {run.run_id: run for run in _loaded(store).runs}
    assert stored["p1"].status is RunStatus.completed
    assert stored["r2"].metadata[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$reply-r2"
    [_question, stripped] = stored["r2"].messages
    assert (stripped.content, stripped.provider_data, stripped.reasoning_content) == (
        "answer r2",
        {"keep": "yes"},
        None,
    )
    assert [run.run_id for run in session.runs] == ["r2", "p1"]


@dataclass(frozen=True, slots=True)
class _RewriteRecordingStore(ConversationStore):
    """A conversation store that records the run ids each message rewrite's edit hands back to store."""

    returned: list[list[str]] = field(default_factory=list)

    def rewrite_run_messages(
        self,
        session_id: str,
        kind: SessionKind,
        edit: Callable[[list[AnyRunRecord]], list[AnyRunRecord]],
        *,
        session: ConversationSession | None = None,
    ) -> None:
        def recorded(runs: list[AnyRunRecord]) -> list[AnyRunRecord]:
            edited = edit(runs)
            self.returned.append([run.run_id for run in edited])
            return edited

        ConversationStore.rewrite_run_messages(self, session_id, kind, recorded, session=session)


@pytest.mark.parametrize("stale", [False, True])
def test_the_replay_field_strip_stores_the_kept_runs_only_when_a_field_is_stale(
    store: ConversationStore,
    stale: bool,
) -> None:
    """Without a stale replay field the strip hands back no run, so compaction rewrites no message."""
    answer = Message(role="assistant", content="answer r1", provider_data={"signature": "sig"} if stale else {})
    _save(store, _run("r0"), _run("r1", messages=[Message(role="user"), answer]), _run("r2"))
    recording = _RewriteRecordingStore(store.path)
    session = _loaded(recording)

    strip_kept_replay_fields(recording, session=session, scope=_SCOPE)

    assert recording.returned == [["r0", "r1", "r2"] if stale else []]
    assert [run.run_id for run in session.runs] == ["r0", "r1", "r2"]


def test_a_summary_id_is_never_handed_out_again_after_a_cut_deletes_the_newest_row(store: ConversationStore) -> None:
    """A fold built on a summary a cut deleted is refused even when another chunk lands in the same window."""
    _save(store, _run("r0"), _run("r1"))
    assert _chunk(store, ["r0"], "S1") is not None
    newest = _chunk(store, ["r1"], "S2 with the redacted turn")
    basis = _basis(store, _AGENT)
    assert basis == (newest, [])
    assert _redact(store, "$r1")
    _save(store, _run("r3"))
    replacement = _chunk(store, ["r3"], "S3 by another response")

    assert replacement is not None
    assert replacement > newest  # type: ignore[operator]
    assert _fold(store, _run("live"), "S4 on the redacted text", basis=basis) is None
    assert _log(store) == ["run:r0", "row:S1", "run:r3", "row:S3 by another response"]


def test_the_fold_fence_checks_the_run_and_its_team_run_within_the_session(store: ConversationStore) -> None:
    """A fold whose running run or team run a cut removed writes nothing; another session's removed set does not fence."""
    seed_session_row(store, _session())
    seed_session_row(store, ConversationSession(session_id="s2", kind="agent", owner_id="code"))
    _execute(store, "INSERT INTO code_sessions_log_removed (run_id, session_id) VALUES ('gone', 's1')")
    _execute(store, "INSERT INTO code_sessions_log_removed (run_id, session_id) VALUES ('elsewhere', 's2')")

    assert _fold(store, _run("gone"), "S") is None
    assert _fold(store, _run("member", parent="gone"), "S") is None
    assert _fold(store, _run("elsewhere"), "S") is not None
    assert _log(store) == ["row:S", "run:elsewhere"]


def test_through_is_the_newest_covered_run_or_the_latest_row(store: ConversationStore) -> None:
    """A chunk or fold sorts after its newest covered run; a fold that covers none, and a barrier, sort with the latest row."""
    first = _fold(store, _run("first-turn"), "F0")
    _save(store, _run("first-turn"), _run("r1"), _run("r2"))
    assert _chunk(store, ["r2", "r1"], "S1") is not None
    _save(store, _run("r3"))
    covering = _fold(store, _run("live"), "F1")
    empty = _fold(store, _run("live"), "F2")
    summary_rows.retire(store, session=_session(), scope_key=_AGENT)

    throughs = _query(store, "SELECT id, summary, through FROM code_sessions_log_summaries ORDER BY id")
    seqs = dict(_query(store, "SELECT run_id, seq FROM code_sessions_log_runs"))
    assert first is not None
    assert covering is not None
    assert empty is not None
    assert [(summary, through) for _id, summary, through in throughs] == [
        ("F0", 0),
        ("S1", seqs["r2"]),
        ("F1", seqs["r3"]),
        ("F2", seqs["r3"]),
        (None, seqs["r3"]),
    ]


def test_a_rows_event_sets_come_from_the_stored_runs_and_the_fold(store: ConversationStore) -> None:
    """A reply linked during the summary call counts, and a fold's row adds its running run's events and keeps them."""
    _save(store, _run("r0", seen=["$context-r0"]), _run("r1"))
    basis = _basis(store, _AGENT)
    reply = {constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply-r0"}
    assert store.patch_run_metadata("s1", "r0", reply)
    live = _run("live", seen=["$context-live"], answered=["$coalesced-live"])

    assert _fold(store, live, "S", basis=basis) is not None

    [(event_ids, seen, fold_run_id, fold_event_ids, fold_seen)] = _query(
        store,
        "SELECT event_ids, seen_event_ids, fold_run_id, fold_event_ids, fold_seen_event_ids "
        "FROM code_sessions_log_summaries",
    )
    run_events = {"$r0", "$context-r0", "$reply-r0", "$r1"}
    live_seen = {"$live", "$context-live"}
    assert set(json.loads(seen)) == run_events | live_seen
    assert set(json.loads(event_ids)) == run_events | live_seen | {"$coalesced-live"}
    assert fold_run_id == "live"
    assert set(json.loads(fold_event_ids)) == {"$live", "$coalesced-live"}
    assert set(json.loads(fold_seen)) == live_seen
    assert (
        summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="seen") == run_events | live_seen
    )


def test_a_chunk_row_keeps_no_fold_provenance(store: ConversationStore) -> None:
    """Only a fold's row names a running run and its provenance."""
    _save(store, _run("r0"))
    assert _chunk(store, ["r0"], "S") is not None

    latest = summary_rows.latest_row(store, session_id="s1", scope_key=_AGENT)

    assert latest is not None
    assert (latest.fold_run_id, latest.fold_event_ids, latest.fold_seen_event_ids) == (None, frozenset(), frozenset())
    assert not latest.legacy
    assert summary_rows.legacy_event_ids(store, session_id="s1", scope_key=_AGENT) is None
    assert _query(store, "SELECT fold_event_ids, fold_seen_event_ids FROM code_sessions_log_summaries") == [
        (None, None),
    ]


def test_the_latest_row_is_the_scopes_newest_barrier_included(store: ConversationStore) -> None:
    """The latest row decides replay, a barrier included, and another scope's rows do not count."""
    assert summary_rows.latest_row(store, session_id="s1", scope_key=_AGENT) is None
    _save(store, _run("r0"), _run("other", agent_id="other"))
    assert _chunk(store, ["r0"], "S") is not None
    assert _chunk(store, ["other"], "other scope's", scope_key="agent:other") is not None
    latest = summary_rows.latest_row(store, session_id="s1", scope_key=_AGENT)
    assert latest is not None
    assert (latest.summary, latest.key, latest.event_ids) == ("S", (1, 1), {"$r0"})

    summary_rows.retire(store, session=_session(), scope_key=_AGENT)

    barrier = summary_rows.latest_row(store, session_id="s1", scope_key=_AGENT)
    assert barrier is not None
    assert (barrier.summary, barrier.key) == (None, (1, 3))


# --- Folds ----------------------------------------------------------------------------------------


def test_a_fold_sorts_after_the_runs_it_covers_and_its_run_after_it(store: ConversationStore) -> None:
    """Replay is the fold's summary and the rest of the turn, which sorts after it once it saves."""
    _save(store, _run("r1"), _run("r2"))
    running = _run("live")

    assert _fold(store, running, "S") is not None
    assert _save(store, running) == ["written"]

    assert _log(store) == ["run:r1", "run:r2", "row:S", "run:live"]
    assert _loaded_ids(store) == ["live"]
    assert _replayed(store) == "S"


def test_a_fold_without_a_stored_run_stores_it_cancelled_with_its_folded_messages(store: ConversationStore) -> None:
    """The running run gets a cancelled row with its live scalars and metadata, which never replays what it folded."""
    prompt = Message(role="user", content="do it")
    live = _run("live", RunStatus.running, messages=[prompt, _call("c1"), _result("c1")])
    live.tools = [ToolExecution(tool_call_id="c1", tool_name="probe")]

    row_id = _fold(store, live, "S", folded=[_call("c1"), _result("c1")])

    [(status, tools, requirements, metadata)] = _query(
        store,
        "SELECT status, tools, requirements, metadata FROM code_sessions_log_runs WHERE run_id = 'live'",
    )
    assert (status, tools, requirements) == (RunStatus.cancelled.value, None, None)
    assert json.loads(metadata) == live.metadata
    assert _query(store, "SELECT DISTINCT folded_by FROM code_sessions_log_messages") == [(row_id,)]
    [stored] = _loaded(store).runs
    assert (stored.run_id, stored.status, stored.messages) == ("live", RunStatus.cancelled, [])


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_a_first_turn_fold_creates_the_session_row_of_its_scope(store: ConversationStore, kind: SessionKind) -> None:
    """A first turn whose summary call reported no usage still commits its fold, on a row of the scope's kind."""
    running: AnyRunRecord = _team_run("live", RunStatus.running) if kind == "team" else _run("live", RunStatus.running)

    assert _fold(store, running, "S") is not None

    loaded = _loaded(store, kind)
    assert (loaded.kind, loaded.owner_id) == (kind, "crew" if kind == "team" else "code")
    assert loaded.summary is not None
    assert loaded.summary.text == "S"
    assert _log(store, _TEAM if kind == "team" else _AGENT) == ["row:S", "run:live"]


def test_a_stored_paused_run_moves_after_its_fold_and_stays_there_once_completed(store: ConversationStore) -> None:
    """A resumed approval folds the turns that happened while it waited, and sorts after them."""
    paused = _run("paused", RunStatus.paused)
    _save(store, _run("r1"), paused, _run("r2"))

    assert _fold(store, paused, "S") is not None

    assert _log(store) == ["run:r1", "run:r2", "row:S", "run:paused"]
    assert _loaded_ids(store) == ["paused"]
    paused.status = RunStatus.completed
    assert _save(store, paused) == ["written"]
    assert _log(store) == ["run:r1", "run:r2", "row:S", "run:paused"]
    assert _loaded_ids(store) == ["paused"]


def test_a_second_fold_of_a_running_run_covers_no_run_twice(store: ConversationStore) -> None:
    """The log knows the first fold covered the stored runs, so the second covers none and sorts with it."""
    _save(store, _run("r1"))
    running = _run("live")

    assert _fold(store, running, "S1") is not None
    assert _fold(store, running, "S2") is not None
    assert _save(store, running) == ["written"]

    assert _log(store) == ["run:r1", "row:S1", "row:S2", "run:live"]
    assert _replayed(store) == "S2"


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_a_run_loads_without_the_messages_its_folds_took(store: ConversationStore, kind: SessionKind) -> None:
    """A copy saved before a fold, such as a paused approval, replays only what the fold left in the run."""
    prompt, later = Message(role="user", content="do it"), Message(role="assistant", content="step after the fold")
    messages = [prompt, _call("c1"), _result("c1"), later]
    paused: AnyRunRecord = (
        _team_run("paused", RunStatus.paused, messages=messages)
        if kind == "team"
        else _run("paused", RunStatus.paused, messages=messages)
    )
    _save(store, paused, kind=kind)

    assert _fold(store, paused, "S", folded=messages[1:3]) is not None

    for _ in range(2):
        [stored] = _loaded(store, kind).runs
        assert [message.content for message in stored.messages] == ["do it", "step after the fold"]
    # The paused copy saved again keeps the folded rows folded.
    assert _save(store, paused, kind=kind) == ["written"]
    [stored] = _loaded(store, kind).runs
    assert [message.content for message in stored.messages] == ["do it", "step after the fold"]


def test_a_fold_flags_the_member_runs_that_ended_and_keeps_an_earlier_folds_flag(store: ConversationStore) -> None:
    """Members a fold covered keep the id of the first fold that took them."""
    team_run = _team_run("team-run", RunStatus.running)
    _save(store, _run("m1", parent="team-run", agent_id="helper"), kind="team")

    first = _fold(store, team_run, "S1", members=["m1"])
    _save(store, _run("m2", parent="team-run", agent_id="helper"), kind="team")
    second = _fold(store, team_run, "S2", members=["m1", "m2"])

    assert dict(
        _query(store, "SELECT run_id, folded_by FROM code_sessions_log_runs WHERE parent_run_id IS NOT NULL"),
    ) == {
        "m1": first,
        "m2": second,
    }


def test_a_fold_flags_no_run_but_its_own_members(store: ConversationStore) -> None:
    """A named top-level run, or a member of another team run, is not the fold's to take."""
    _save(store, _team_run("t0"), _run("other-member", parent="t0", agent_id="helper"), kind="team")

    assert _fold(store, _team_run("live", RunStatus.running), "S", members=["t0", "other-member"]) is not None

    assert _query(store, "SELECT run_id FROM code_sessions_log_runs WHERE folded_by IS NOT NULL") == []


def test_a_crash_after_a_fold_replays_its_summary_and_a_late_pre_fold_copy_hides_what_it_folded(
    store: ConversationStore,
) -> None:
    """The restarted attempt replays the summary; a copy saved from before the fold cannot replay its part twice."""
    _save(store, _run("r1"), _run("r2"))
    prompt = Message(role="user", content="do it")
    crashed = _run("crashed", RunStatus.running, messages=[prompt, _call("c1"), _result("c1")])
    assert _fold(store, crashed, "S", folded=crashed.messages[1:]) is not None

    loaded = _loaded(store)
    assert loaded.summary is not None
    assert loaded.summary.text == "S"
    assert [(run.run_id, run.status) for run in loaded.runs] == [("crashed", RunStatus.cancelled)]
    crashed.status = RunStatus.cancelled
    assert _save(store, crashed) == ["written"]
    [stored] = _loaded(store).runs
    assert [message.content for message in stored.messages] == ["do it"]


def test_a_fold_keeps_what_its_turn_read_seen_after_the_turn_stops(store: ConversationStore) -> None:
    """Thread context a stopped turn folded into the summary does not return as unseen context."""
    _save(store, _run("r1"))
    running = _run("live", seen=["$context"])
    assert _fold(store, running, "S") is not None
    running.status = RunStatus.cancelled
    running.metadata = {}
    assert _save(store, running) == ["written"]

    assert "$context" in summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="seen")


def test_folded_tool_call_ids_are_the_folded_messages_and_members_calls(store: ConversationStore) -> None:
    """Interrupted records leave out every call a fold summarized: folded calls, results, and folded members' tools."""
    team_run = _team_run("team-run", RunStatus.running)
    folded_member = _run("m1", parent="team-run", agent_id="helper")
    folded_member.tools = [ToolExecution(tool_call_id="member-call", tool_name="probe")]
    later_member = _run("m2", parent="team-run", agent_id="helper")
    later_member.tools = [ToolExecution(tool_call_id="later-member-call", tool_name="probe")]
    toolless_member = _run("m0", parent="team-run", agent_id="helper")
    _save(store, folded_member, later_member, toolless_member, kind="team")
    orphan_result = _result("orphan-result")
    unnamed_call = Message(role="assistant", tool_calls=[{"type": "function", "function": {"name": "probe"}}])
    folded = [_call("c1"), _result("c1"), unnamed_call]
    assert _fold(store, team_run, "S1", folded=folded, members=["m1", "m0"]) is not None
    assert _fold(store, team_run, "S2", folded=[_call("c2"), orphan_result]) is not None
    team_run.messages = [Message(role="user", content="go"), _call("kept-call")]
    team_run.status = RunStatus.completed
    assert _save(store, team_run, kind="team") == ["written"]

    calls = summary_rows.folded_tool_call_ids(store, session_id="s1", run_id="team-run")

    assert calls == {"c1", "c2", "orphan-result", "member-call"}
    assert summary_rows.folded_tool_call_ids(store, session_id="s1", run_id="m1") == set()
    assert summary_rows.folded_tool_call_ids(store, session_id="s2", run_id="team-run") == set()


# --- Cuts -----------------------------------------------------------------------------------------


def test_redacting_a_compacted_run_restores_the_runs_before_it(store: ConversationStore) -> None:
    """A cut removes the run holding the event and everything after it; earlier covered runs replay again."""
    _save(store, *(_run(f"r{index}") for index in range(5)))
    assert _chunk(store, ["r0", "r1"], "S1") is not None
    assert _chunk(store, ["r2", "r3"], "S2") is not None

    assert _redact(store, "$r3")

    assert _loaded_ids(store) == ["r2"]
    assert _replayed(store) == "S1"
    assert _save(store, _run("r3"), _run("r4")) == ["removed", "removed"]
    assert _log(store) == ["run:r0", "run:r1", "row:S1", "run:r2"]
    assert _removed(store) == {"r3", "r4"}


def test_a_pending_approval_before_the_cut_survives(store: ConversationStore) -> None:
    """A run paused before the redacted turn did not see it and stays resumable."""
    _save(store, _run("p0", RunStatus.paused), _run("r1"), _run("r2"), _run("r3"))
    assert _chunk(store, ["r1", "r2"], "S1") is not None

    assert _redact(store, "$r2")

    assert _loaded_ids(store) == ["p0", "r1"]
    assert _replayed(store) is None
    assert _keys(store)["p0"] == (1, 0)


def test_a_cut_takes_a_run_that_folded_on_both_sides_of_its_pause_whole(store: ConversationStore) -> None:
    """Redacting a turn the resumed run folded removes that run, its earlier fold, and everything after it."""
    _save(store, _run("r1"))
    running = _run("approval", RunStatus.running)
    assert _fold(store, running, "S1") is not None
    running.status = RunStatus.paused
    assert _save(store, running) == ["written"]
    _save(store, _run("r2"), _run("r3"))
    assert _fold(store, running, "S2") is not None
    assert _keys(store) == {"r1": (1, 0), "S1": (1, 1), "r2": (3, 0), "r3": (4, 0), "S2": (4, 2), "approval": (5, 0)}

    assert _redact(store, "$r3")

    assert _log(store) == ["run:r1"]
    assert _loaded_ids(store) == ["r1"]
    assert _replayed(store) is None
    assert _removed(store) == {"r2", "r3", "approval"}


def test_editing_a_turn_an_older_resumed_run_folded_removes_that_run(store: ConversationStore) -> None:
    """The resumed run's fold summarized the edited turn, so the run built on it goes; the runs before both stay."""
    paused = _run("paused", RunStatus.paused)
    _save(store, _run("r1"), paused, _run("r2"))
    assert _chunk(store, ["r1"], "S0") is not None
    assert _fold(store, paused, "S1") is not None
    assert _log(store) == ["run:r1", "row:S0", "run:r2", "row:S1", "run:paused"]

    assert _edit(store, "$r2")

    assert _log(store) == ["run:r1", "row:S0"]
    assert _replayed(store) == "S0"


@pytest.mark.parametrize("discarded", [False, True], ids=["run-stored", "run-discarded"])
def test_redacting_an_event_only_the_running_turn_read_cuts_at_its_fold(
    store: ConversationStore,
    discarded: bool,
) -> None:
    """The fold's provenance holds what its turn read, even once that run's row is discarded, and the cut fences it."""
    _save(store, _run("r1"))
    running = _run("live", seen=["$context"])
    assert _fold(store, running, "S") is not None
    _save(store, _run("r3"))
    if discarded:
        assert store.delete_runs(["live"], session_id="s1") == {"live"}

    assert _redact(store, "$context")

    assert _log(store) == ["run:r1"]
    assert _save(store, running) == ["removed"]
    assert _log(store) == ["run:r1"]
    assert _replayed(store) is None


def test_a_fold_row_holds_what_its_turn_answered_for_an_edit_and_what_it_read_for_a_redaction(
    store: ConversationStore,
) -> None:
    """A message the running turn only read is no reply of it, so editing that message keeps the fold."""
    _save(store, _run("r1"))
    running = _run("live", seen=["$context"])
    assert _fold(store, running, "S") is not None
    assert store.delete_runs(["live"], session_id="s1") == {"live"}

    assert not _edit(store, "$context")
    assert _log(store) == ["run:r1", "row:S"]
    assert _edit(store, "$live")
    assert _log(store) == ["run:r1"]


def test_a_redaction_of_what_a_discarded_fold_read_cuts_at_its_row(store: ConversationStore) -> None:
    """Either provenance set holds an event for a redaction."""
    _save(store, _run("r1"))
    assert _fold(store, _run("live", seen=["$context"]), "S") is not None
    assert store.delete_runs(["live"], session_id="s1") == {"live"}

    assert _redact(store, "$context")

    assert _log(store) == ["run:r1"]


def test_editing_a_retried_turn_whose_second_attempt_folded_the_first_removes_both(store: ConversationStore) -> None:
    """Both attempts answered the edited message, so neither the first attempt nor the fold survives."""
    _save(store, _run("r0"), _run("attempt-1"))
    second = _run("attempt-2", answered=["$attempt-1"])
    assert _fold(store, second, "S") is not None
    assert _save(store, second) == ["written"]

    assert _edit(store, "$attempt-1")

    assert _log(store) == ["run:r0"]


def test_editing_a_message_a_later_turn_folded_removes_that_turn(store: ConversationStore) -> None:
    """A later turn's fold covered the edited turn, so the edit removes the fold and the later turn."""
    _save(store, _run("r0"), _run("r1"))
    later = _run("bot-turn")
    assert _fold(store, later, "S") is not None
    assert _save(store, later) == ["written"]

    assert _edit(store, "$r1")

    assert _log(store) == ["run:r0"]


def test_a_reply_linked_to_a_covered_run_is_cut_when_redacted(store: ConversationStore) -> None:
    """The link joins the covering row's event sets, so redacting the reply cuts that run and what came after it."""
    _save(store, _run("r1"), _run("r2"), _run("r3"))
    assert _chunk(store, ["r1", "r2"], "S") is not None
    assert store.patch_run_metadata("s1", "r2", {constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply-r2"})
    assert "$reply-r2" in summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="all")

    assert _redact(store, "$reply-r2")

    assert _log(store) == ["run:r1"]
    assert _replayed(store) is None


def test_edit_cuts_a_summary_whose_range_answered_the_event(store: ConversationStore) -> None:
    """Editing a message a covered run answered removes that run, its summary, and what followed."""
    _save(store, _run("r0"), _run("r1"), _run("r2"))
    assert _chunk(store, ["r0", "r1"], "S1") is not None

    assert _edit(store, "$r1")

    assert _loaded_ids(store) == ["r0"]
    assert _replayed(store) is None


def test_an_edit_of_a_message_a_run_only_read_cuts_nothing(store: ConversationStore) -> None:
    """An edit cuts what answered the message; a redaction also cuts what only read it."""
    _save(store, _run("r0", seen=["$context"]), _run("r1"))
    assert _chunk(store, ["r0"], "S1") is not None

    assert not _edit(store, "$context")
    assert _replayed(store) == "S1"
    assert _redact(store, "$context")
    assert _loaded_ids(store) == []
    assert _log(store) == []


def test_an_edit_removes_the_following_runs_even_without_metadata(store: ConversationStore) -> None:
    """Everything after the run that answered the edited message goes, including runs with no metadata."""
    bare = RunRecord(run_id="bare-after", agent_id="code", session_id="s1", status=RunStatus.completed)
    _save(store, _run("before"), _run("matched"), bare, _run("after"))

    assert _edit(store, "$matched")

    assert _loaded_ids(store) == ["before"]


def test_an_edit_removes_a_team_run_with_its_members(store: ConversationStore) -> None:
    """Team edit regeneration deletes the stale team run and its member rows from the team session."""
    _save(
        store,
        _run("member", parent="original", agent_id="helper"),
        _team_run("original"),
        _team_run("other"),
        kind="team",
    )

    assert _edit(store, "$original", _TEAM)

    assert _loaded_ids(store, "team") == []
    assert _query(store, "SELECT run_id FROM code_sessions_log_runs") == []
    assert _removed(store) == {"original", "other", "member"}


@pytest.mark.parametrize(
    ("metadata_key", "edited"),
    [
        (constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, "$first"),
        (constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY, "$selection"),
    ],
    ids=["coalesced-source", "discovery-alias"],
)
def test_an_edit_matches_every_event_a_run_answered(store: ConversationStore, metadata_key: str, edited: str) -> None:
    """A coalesced batch member or an interactive discovery alias identifies the run that answered it."""
    run = RunRecord(
        run_id="answer",
        agent_id="code",
        session_id="s1",
        status=RunStatus.completed,
        metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$primary", metadata_key: [edited, "$primary"]},
    )
    _save(store, run)

    assert _edit(store, edited)

    assert _loaded_ids(store) == []


def test_a_cut_stays_in_its_scope_and_session(store: ConversationStore) -> None:
    """Runs and rows of another agent's scope, and of another session, sort apart and stay."""
    _save(store, _run("r0"), _run("other-0", agent_id="other"), _run("r1"), _run("other-1", agent_id="other"))
    assert _chunk(store, ["other-0"], "other scope's", scope_key="agent:other") is not None
    elsewhere = ConversationSession(session_id="s2", kind="agent", owner_id="code")
    assert store.save_run(elsewhere, _run("s2-holder"), record_usage=False) == "written"
    assert not _redact(store, "$s2-holder")
    store.save_run(
        elsewhere,
        RunRecord(run_id="s2-run", agent_id="code", session_id="s2", status=RunStatus.completed),
        record_usage=False,
    )

    assert _redact(store, "$r0")

    assert _log(store) == []
    assert _log(store, "agent:other") == ["run:other-0", "row:other scope's", "run:other-1"]
    assert _query(store, "SELECT run_id FROM code_sessions_log_runs WHERE session_id = 's2' ORDER BY seq") == [
        ("s2-holder",),
        ("s2-run",),
    ]
    assert not _redact(store, "$other-1")


def test_a_member_run_holds_no_event_of_its_own(store: ConversationStore) -> None:
    """Only top-level runs are cut at; a member run goes with its team run."""
    _save(store, _run("m1", parent="t1", agent_id="helper", seen=["$only-the-member"]), _team_run("t1"), kind="team")

    assert not _redact(store, "$only-the-member", "agent:helper")
    assert not _redact(store, "$only-the-member", _TEAM)

    assert _query(store, "SELECT run_id FROM code_sessions_log_runs ORDER BY seq") == [("m1",), ("t1",)]


def test_a_late_save_of_a_cut_run_or_its_member_writes_nothing(store: ConversationStore) -> None:
    """A stale copy of a run a cut removed, or of its member, is never written back."""
    _save(store, _run("m1", parent="t1", agent_id="helper"), _team_run("t1"), _team_run("t2"), kind="team")

    assert _redact(store, "$t1", _TEAM)

    late_member = _run("m2", parent="t1", agent_id="helper")
    assert _save(store, _team_run("t1"), late_member, kind="team") == ["removed", "removed"]
    assert _loaded_ids(store, "team") == []
    assert _fold(store, _team_run("t2", RunStatus.running), "S") is None


def test_redacting_a_team_turn_that_folded_removes_its_member_runs(store: ConversationStore) -> None:
    """A team turn's fold goes with the turn, and its delegated member runs go with it."""
    _save(store, _team_run("t0"), kind="team")
    team_run = _team_run("team-run", RunStatus.running)
    _save(store, _run("member", parent="team-run", agent_id="helper"), kind="team")
    assert _fold(store, team_run, "S", members=["member"]) is not None
    team_run.status = RunStatus.completed
    assert _save(store, team_run, kind="team") == ["written"]

    assert _redact(store, "$team-run", _TEAM)

    assert _log(store, _TEAM) == ["run:t0"]
    assert _loaded_ids(store, "team") == ["t0"]
    assert _query(store, "SELECT run_id FROM code_sessions_log_runs ORDER BY seq") == [("t0",)]


# --- Retirement and legacy summaries --------------------------------------------------------------


def test_retiring_writes_a_barrier_and_removes_every_run_that_folded_mid_turn(store: ConversationStore) -> None:
    """A run whose stored part starts mid-turn would replay without what it built on once its summary retires."""
    _save(store, _run("r1"))
    done = _run("done")
    assert _fold(store, done, "S1") is not None
    assert _save(store, done) == ["written"]
    running = _run("live")
    assert _fold(store, running, "S2") is not None
    # The running turn discarded its row, as an empty run does; its fold still names it.
    assert store.delete_runs(["live"], session_id="s1") == {"live"}

    summary_rows.retire(store, session=_session(), scope_key=_AGENT)

    assert _log(store) == ["run:r1", "row:S1", "row:S2", "row:None"]
    assert _removed(store) == {"done", "live"}
    assert _save(store, running) == ["removed"]
    assert _replayed(store) is None
    assert _loaded_ids(store) == []
    # The retired folds' provenance holds nothing any more.
    assert not _redact(store, "$done")
    assert not _redact(store, "$live")


def test_a_barrier_hides_what_it_retired_from_replay_seen_ids_and_cuts(store: ConversationStore) -> None:
    """Retired summaries and the runs behind them count for nothing, while runs after the barrier still do."""
    _save(store, _run("r1", seen=["$context"]), _run("p2", RunStatus.paused), _run("r3"))
    assert _chunk(store, ["r1", "r3"], "S1") is not None
    assert summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="all") >= {"$r1", "$context"}

    summary_rows.retire(store, session=_session(), scope_key=_AGENT)
    _save(store, _run("r4"))

    assert summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="all") == set()
    assert summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="seen") == set()
    assert _loaded_ids(store) == ["p2", "r4"]
    assert not _redact(store, "$context")
    assert _chunk(store, ["r4"], "S2") is not None
    assert summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="seen") == {"$r4"}
    summary_rows.retire(store, session=_session(), scope_key=_AGENT)
    assert not _redact(store, "$r4")
    assert summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="seen") == set()
    assert _redact(store, "$p2")
    assert _log(store) == ["run:r1", "row:None", "row:None"]


def test_a_fold_racing_a_retirement_is_refused(store: ConversationStore) -> None:
    """Retirement appends a barrier, so a fold read before it would summarize retired history again."""
    _save(store, _run("r1"))
    basis = _basis(store, _AGENT)
    summary_rows.retire(store, session=_session(), scope_key=_AGENT)

    assert _fold(store, _run("live"), "S", basis=basis) is None
    # A barrier with no summary before it covers nothing, so the runs still replay.
    assert _log(store) == ["row:None", "run:r1"]


def test_legacy_event_ids_report_a_legacy_summary_while_it_is_in_force(store: ConversationStore) -> None:
    """A legacy summary is in force while it has text and no barrier follows it, summaries on top of it included."""
    assert summary_rows.legacy_event_ids(store, session_id="s1", scope_key=_AGENT) is None
    _seed_legacy_row(store, "legacy summary", ["$legacy"])
    _save(store, _run("r1"), _run("r2"))
    assert _chunk(store, ["r1"], "legacy summary and r1") is not None

    assert summary_rows.legacy_event_ids(store, session_id="s1", scope_key=_AGENT) == {"$legacy"}
    assert _log(store) == ["row:legacy summary", "run:r1", "row:legacy summary and r1", "run:r2"]

    summary_rows.retire(store, session=_session(), scope_key=_AGENT)

    assert summary_rows.legacy_event_ids(store, session_id="s1", scope_key=_AGENT) is None


@pytest.mark.parametrize("legacy_rows", [1, 2])
def test_cutting_at_the_legacy_row_removes_the_whole_scope(store: ConversationStore, legacy_rows: int) -> None:
    """A legacy summary cannot be split by run, so redacting what it may contain clears every run and row after it."""
    for _ in range(legacy_rows):
        _seed_legacy_row(store, "legacy summary", ["$legacy"])
    _save(store, _run("r1"), _run("p2", RunStatus.paused), _run("r3"))
    assert _chunk(store, ["r1"], "legacy summary and r1") is not None

    assert _redact(store, "$legacy")

    assert _log(store) == []
    assert _loaded_ids(store) == []
    assert _replayed(store) is None
    assert _removed(store) == {"r1", "p2", "r3"}


def test_cutting_at_the_legacy_row_does_nothing_once_it_retired(store: ConversationStore) -> None:
    """A retired legacy summary is not in force, so there is nothing to cut at."""
    _seed_legacy_row(store, "legacy summary", ["$legacy"])
    _save(store, _run("r1"))
    summary_rows.retire(store, session=_session(), scope_key=_AGENT)

    summary_rows.cut_at_legacy_row(store, session=_session(), scope_key=_AGENT)

    assert _log(store) == ["row:legacy summary", "row:None", "run:r1"]


def test_redacting_a_loaded_run_retires_a_legacy_summary(store: ConversationStore) -> None:
    """Legacy summaries lack complete provenance, so removing a run for any other event retires them."""
    _seed_legacy_row(store, "legacy summary", ["$legacy"])
    _save(store, _run("r1"), _run("r2"), _run("r3"))
    assert _chunk(store, ["r1"], "legacy summary and r1") is not None

    assert _redact(store, "$r3")

    assert _replayed(store) is None
    assert _loaded_ids(store) == ["r2"]
    assert summary_rows.legacy_event_ids(store, session_id="s1", scope_key=_AGENT) is None
    assert summary_rows.history_event_ids(store, session_id="s1", scope_key=_AGENT, kind="seen") == set()
    assert _log(store) == ["row:legacy summary", "run:r1", "row:legacy summary and r1", "row:None", "run:r2"]


def test_a_retired_legacy_summary_stays_retired_after_a_later_cut(store: ConversationStore) -> None:
    """A cut before the retirement barrier brings back neither the retired summaries nor the runs behind it."""
    _seed_legacy_row(store, "legacy summary", ["$legacy"])
    _save(store, _run("r1"), _run("c1", RunStatus.cancelled, seen=["$late-redaction"]), _run("r2"), _run("r3"))
    assert _chunk(store, ["r1"], "legacy summary and r1") is not None
    assert _redact(store, "$r3")

    assert _redact(store, "$late-redaction")

    assert _replayed(store) is None
    assert _loaded_ids(store) == []
    assert _log(store) == ["row:legacy summary", "run:r1", "row:legacy summary and r1", "row:None"]


def test_an_edit_keeps_a_legacy_summary_in_force(store: ConversationStore) -> None:
    """An edit cuts what answered the message and never clears or retires a legacy summary, even one that may hold it."""
    _seed_legacy_row(store, "legacy summary", ["$r1"])
    _save(store, _run("r0"), _run("r1"))

    assert _edit(store, "$r1")

    assert _replayed(store) == "legacy summary"
    assert _loaded_ids(store) == ["r0"]
    assert summary_rows.legacy_event_ids(store, session_id="s1", scope_key=_AGENT) == {"$r1"}
