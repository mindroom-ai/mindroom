"""The conversation store's run log rules, session fields, usage, and concurrency guarantees.

Each test's ``store`` is the database ``code`` opened as production opens one, so it holds every table.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import TOKEN_FIELDS, HelperUsage, MessageMetrics, RequestUsage, RunMetrics, TokenCounts
from mindroom.session_storage import store as store_module
from mindroom.session_storage.schema import LOG_VERSION
from mindroom.session_storage.store import (
    ConversationStore,
    ensure_session_row,
    persist_to,
    runs_without,
    store_fold,
    watch_session_row,
)
from mindroom.session_storage.types import ConversationSession, session_state_snapshot
from tests.conftest import delete_runs, seed_session_row, seed_session_state, stored_session_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import SaveOutcome
    from mindroom.session_storage.types import SessionKind

_NON_VISIBLE = [RunStatus.paused, RunStatus.cancelled, RunStatus.error]


def _session(session_id: str = "s1", *, kind: SessionKind = "agent", owner: str = "code") -> ConversationSession:
    return ConversationSession(session_id=session_id, kind=kind, owner_id=owner, created_at=1_700_000_000)


def _team() -> ConversationSession:
    return _session(kind="team", owner="crew")


def _usage(input_tokens: int, *, model: str = "code-model") -> RunMetrics:
    metrics = RunMetrics()
    metrics.add(
        MessageMetrics(tokens=TokenCounts(input_tokens=input_tokens, output_tokens=1, total_tokens=input_tokens + 1)),
        model_id=model,
        provider="P",
    )
    return metrics


def _run(
    run_id: str,
    status: RunStatus = RunStatus.completed,
    *,
    content: str | None = None,
    parent: str | None = None,
    agent_id: str = "code",
    metrics: RunMetrics | None = None,
    metadata: dict[str, Any] | None = None,
    messages: list[Message] | None = None,
    tools: list[ToolExecution] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        session_id="s1",
        parent_run_id=parent,
        status=status,
        content=content,
        messages=messages
        if messages is not None
        else [
            Message(role="system", content="prompt", id=f"{run_id}-system", created_at=1),
            Message(role="user", content=run_id, id=f"{run_id}-user", created_at=1),
        ],
        metrics=metrics,
        metadata=metadata,
        tools=tools,
        created_at=1_700_000_000,
    )


def _member(run_id: str, team_run_id: str, status: RunStatus = RunStatus.completed, **fields: Any) -> RunRecord:  # noqa: ANN401
    return _run(run_id, status, parent=team_run_id, agent_id="helper", **fields)


def _team_run(run_id: str, status: RunStatus = RunStatus.completed, **fields: Any) -> TeamRunRecord:  # noqa: ANN401
    return TeamRunRecord(
        run_id=run_id,
        team_id="crew",
        session_id="s1",
        status=status,
        created_at=1_700_000_000,
        **fields,
    )


def _waiting(call_id: str) -> ToolExecution:
    return ToolExecution(tool_call_id=call_id, tool_name="shell", requires_confirmation=True)


def _save(
    store: ConversationStore,
    *runs: AnyRunRecord,
    session: ConversationSession | None = None,
    record_usage: bool = False,
) -> list[SaveOutcome]:
    session = session or _session()
    return [store.save_run(session, run, record_usage=record_usage) for run in runs]


def _query(store: ConversationStore, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(store.path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _execute(store: ConversationStore, sql: str, *parameters: object) -> None:
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(sql, parameters)


def _seqs(store: ConversationStore) -> dict[str, int]:
    return dict(_query(store, "SELECT run_id, seq FROM code_sessions_log_runs"))


def _row(store: ConversationStore, run_id: str) -> tuple[Any, ...] | None:
    rows = _query(
        store,
        "SELECT seq, status, record, metadata, folded_by, created_at, updated_at FROM code_sessions_log_runs "
        "WHERE run_id = ?",
        run_id,
    )
    return rows[0] if rows else None


def _message_rows(store: ConversationStore, run_id: str) -> list[tuple[str, int, int | None, dict[str, Any]]]:
    return [
        (message_id, position, folded_by, json.loads(data))
        for message_id, position, folded_by, data in _query(
            store,
            "SELECT message_id, ord, folded_by, data FROM code_sessions_log_messages WHERE run_id = ? "
            "ORDER BY ord, message_id",
            run_id,
        )
    ]


def _summary(
    store: ConversationStore,
    through: int,
    *,
    scope_key: str = "agent:code",
    summary: str | None = "summary",
    event_ids: tuple[str, ...] = (),
    seen: tuple[str, ...] = (),
) -> int:
    """Append one summary row, a barrier when ``summary`` is None, as compaction or retirement would."""
    with closing(sqlite3.connect(store.path)) as connection, connection:
        return connection.execute(
            "INSERT INTO code_sessions_log_summaries (session_id, scope_key, through, summary, created_at, event_ids, "
            "seen_event_ids) VALUES ('s1', ?, ?, ?, 1700000100, ?, ?)",
            (scope_key, through, summary, json.dumps(list(event_ids)), json.dumps(list(seen))),
        ).lastrowid


def _summary_sets(store: ConversationStore, row_id: int) -> tuple[list[str], list[str]]:
    [(event_ids, seen)] = _query(
        store,
        "SELECT event_ids, seen_event_ids FROM code_sessions_log_summaries WHERE id = ?",
        row_id,
    )
    return json.loads(event_ids), json.loads(seen)


def _remove(store: ConversationStore, run_id: str, session_id: str = "s1") -> None:
    _execute(store, "INSERT INTO code_sessions_log_removed (run_id, session_id) VALUES (?, ?)", run_id, session_id)


def _usage_row(store: ConversationStore, run_id: str) -> dict[str, Any]:
    [(usage_data,)] = _query(store, "SELECT usage_data FROM code_sessions_usage WHERE run_id = ?", run_id)
    return json.loads(usage_data)


def _has_usage(store: ConversationStore, run_id: str) -> bool:
    return bool(_query(store, "SELECT 1 FROM code_sessions_usage WHERE run_id = ?", run_id))


def _totals(store: ConversationStore, session_id: str = "s1") -> dict[str, int]:
    """Return the session's totals: the token counters of its usage rows, added up."""
    totals = dict.fromkeys(TOKEN_FIELDS, 0)
    for (usage_data,) in _query(store, "SELECT usage_data FROM code_sessions_usage WHERE session_id = ?", session_id):
        for name, value in json.loads(usage_data)["metrics"].items():
            if name in totals:
                totals[name] += value
    return totals


def _loaded(store: ConversationStore) -> ConversationSession:
    session = store.load_session("s1")
    assert session is not None
    return session


def _loaded_ids(store: ConversationStore) -> list[str]:
    return [run.run_id for run in _loaded(store).runs]


def _as_stored(runs: list[AnyRunRecord]) -> list[dict[str, Any]]:
    return [run.to_dict() for run in runs]


def _persist(store: ConversationStore, session: ConversationSession, *runs: AnyRunRecord) -> None:
    persist = persist_to(store, session)

    async def persist_all() -> None:
        for run in runs:
            await persist(run)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, persist_all()).result()


def _fold(
    store: ConversationStore,
    record: AnyRunRecord,
    messages: list[Message],
    *,
    member_run_ids: tuple[str, ...] = (),
) -> int:
    """Append a fold summary row and store the fold beside it, in one transaction, as a fold's append does."""
    with store.transaction(write=True) as connection:
        summary_id = connection.execute(
            "INSERT INTO code_sessions_log_summaries (session_id, scope_key, through, summary, created_at, "
            "fold_run_id) VALUES ('s1', 'agent:code', 0, 'folded', 1700000100, ?)",
            (record.run_id,),
        ).lastrowid
        assert summary_id is not None
        store_fold(connection, store.names, "s1", record, messages, member_run_ids, summary_id)
    return summary_id


# --- seq ----------------------------------------------------------------------------------------------------------


def test_a_first_write_takes_the_next_seq_whatever_its_status(team_store: ConversationStore) -> None:
    """A new run gets the next number in its insert, paused, finished, cancelled, or a member alike."""
    _save(team_store, _run("p0", RunStatus.paused), _run("r1"), _run("c2", RunStatus.cancelled), session=_team())
    _save(team_store, _member("m3", "t4"), _team_run("t4"), session=_team())

    assert _seqs(team_store) == {"p0": 1, "r1": 2, "c2": 3, "m3": 4, "t4": 5}
    assert _query(team_store, "SELECT version, next_seq FROM code_sessions_log_meta") == [(LOG_VERSION, 6)]


def test_a_deleted_runs_number_is_never_handed_out_again(store: ConversationStore) -> None:
    """The counter only grows, so a run saved after the newest runs were deleted sorts after every number used."""
    _save(store, _run("r1"), _run("r2"), _run("r3"))
    delete_runs(store, ["r2", "r3"], session_id="s1")

    _save(store, _run("r4"))

    assert _seqs(store) == {"r1": 1, "r4": 4}


def test_concurrent_first_saves_each_take_their_own_seq(store: ConversationStore) -> None:
    """The counter moves under the write lock, so racing saves from many owners never share a number."""
    start = threading.Barrier(8)

    def first_save(index: int) -> None:
        start.wait()
        store.save_run(_session(f"s{index}"), _run(f"r{index}"), record_usage=False)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(first_save, range(8)))

    assert sorted(_seqs(store).values()) == list(range(1, 9))


def _status_table() -> list[tuple[str, RunStatus | None, RunStatus, SaveOutcome, bool]]:
    """Spec 3.6's table: who saves, the stored status, the saved status, the outcome, and whether the run moves."""
    cases: list[tuple[str, RunStatus | None, RunStatus, SaveOutcome, bool]] = []
    for saver in ("P1", "P4"):
        cases += [(saver, None, saved, "written", True) for saved in (RunStatus.paused, RunStatus.completed)]
        cases += [(saver, stored, saved, "written", False) for stored in _NON_VISIBLE for saved in _NON_VISIBLE]
        cases += [(saver, stored, RunStatus.completed, "written", True) for stored in _NON_VISIBLE]
        cases += [
            (saver, stored, saved, "final" if saver == "P1" else "written", False)
            for stored in (RunStatus.completed, RunStatus.running)
            for saved in (RunStatus.paused, RunStatus.completed)
        ]
    return cases


@pytest.mark.parametrize(("saver", "stored", "saved", "outcome", "moves"), _status_table())
def test_every_row_of_the_save_status_table(
    store: ConversationStore,
    saver: str,
    stored: RunStatus | None,
    saved: RunStatus,
    outcome: SaveOutcome,
    moves: bool,
) -> None:
    """P1 refuses any save over a visible run; P4 replaces it; a non-visible run saved visible moves to the end."""
    if stored is not None:
        _save(store, _run("r0", stored, content="stored"))
    _save(store, _run("r1"))
    record = _run("r0", saved, content="saved")
    session = _session()

    result = (
        store.save_run(session, record, record_usage=False)
        if saver == "P1"
        else store.save_interrupted_record(session, record)
    )

    row = _row(store, "r0")
    assert row is not None
    assert result == outcome
    assert row[0] == (3 if moves and stored is not None else 2 if stored is None else 1)
    assert (row[1], json.loads(row[2])["content"]) == (
        (stored.value, "stored") if outcome == "final" and stored is not None else (saved.value, "saved")
    )


@pytest.mark.parametrize("stored_status", _NON_VISIBLE, ids=["approval", "cli-checkpoint", "interrupted-record"])
def test_a_non_visible_run_saved_visible_moves_to_the_end(store: ConversationStore, stored_status: RunStatus) -> None:
    """A continuation finishing after newer turns and a compaction replays after the summary."""
    _save(store, _run("r0", stored_status), *(_run(f"r{index}") for index in range(1, 4)))
    _summary(store, 4)

    _save(store, _run("r0", content="approved"))

    session = _loaded(store)
    assert _seqs(store)["r0"] == 5
    assert [run.run_id for run in session.runs] == ["r0"]
    assert session.summary is not None
    assert session.summary.text == "summary"


def test_a_member_saved_visible_keeps_its_seq(team_store: ConversationStore) -> None:
    """Only a top-level run moves when it finishes; a member's number only orders it among the session's rows."""
    team = _team()
    _save(
        team_store,
        _member("m0", "t0", RunStatus.paused),
        _team_run("t0", RunStatus.paused),
        _team_run("t1"),
        session=team,
    )

    assert _save(team_store, _member("m0", "t0", RunStatus.completed), session=team) == ["written"]

    assert _seqs(team_store)["m0"] == 1


# --- rules 1, 2, and 4 ----------------------------------------------------------------------------------------------


def test_a_removed_run_or_a_member_of_one_writes_nothing_but_records_its_usage(store: ConversationStore) -> None:
    """The provider calls behind a late save of a run a cut removed were made, so they still count."""
    _save(store, _run("r0", metrics=_usage(10)), record_usage=True)
    _remove(store, "gone")
    session = _session()

    outcomes = [
        store.save_run(session, _run("gone", metrics=_usage(5)), record_usage=True),
        store.save_run(session, _member("gone-member", "gone", metrics=_usage(2)), record_usage=True),
        store.save_interrupted_record(session, _run("gone", content="interrupted")),
    ]

    assert outcomes == ["removed", "removed", "removed"]
    assert set(_seqs(store)) == {"r0"}
    assert _usage_row(store, "gone")["metrics"]["input_tokens"] == 5
    assert _usage_row(store, "gone-member")["metrics"]["input_tokens"] == 2
    assert _totals(store)["input_tokens"] == 17


def test_a_run_removed_in_another_session_is_not_fenced(store: ConversationStore) -> None:
    """The removed set is per session."""
    seed_session_row(store, _session("s2"))
    _remove(store, "r1", "s2")

    assert _save(store, _run("r1")) == ["written"]


def test_a_final_save_writes_nothing_and_records_no_usage(store: ConversationStore) -> None:
    """A stale copy of a finished run, paused or finished, changes neither its row, its usage, nor the totals."""
    _save(store, _run("r1", content="original", metrics=_usage(10)), record_usage=True)
    before, usage_before, totals_before = _row(store, "r1"), _usage_row(store, "r1"), _totals(store)
    session = _loaded(store)
    kept = session.runs[0]

    outcomes = [
        store.save_run(session, _run("r1", RunStatus.paused, content="stale paused"), record_usage=True),
        store.save_run(session, _run("r1", content="stale", metrics=_usage(99)), record_usage=True),
    ]

    assert outcomes == ["final", "final"]
    assert _row(store, "r1") == before
    assert _usage_row(store, "r1") == usage_before
    assert _totals(store) == totals_before
    assert session.runs[0] is kept


def test_a_member_of_a_visible_team_run_is_final(team_store: ConversationStore) -> None:
    """Members persist when they end, before their team run's terminal save; after it, they are final."""
    team = _team()
    _save(team_store, _member("m0", "t0", content="original"), _team_run("t0"), session=team)
    _save(team_store, _member("p0", "tp", RunStatus.paused), _team_run("tp", RunStatus.paused), session=team)

    outcomes = _save(
        team_store,
        _member("m0", "t0", content="stale"),
        _member("m-new", "t0"),
        _member("p0", "tp", content="checked"),
        session=team,
    )

    assert outcomes == ["final", "final", "written"]
    row = _row(team_store, "m0")
    assert row is not None
    assert json.loads(row[2])["content"] == "original"
    assert _row(team_store, "m-new") is None


def test_a_paused_team_run_behind_the_summary_keeps_writable_members_and_moves_on_completion(
    team_store: ConversationStore,
) -> None:
    """Only a visible run is final; a paused team approval's members still save, and the team run then moves."""
    team = _team()
    _save(
        team_store,
        _member("m0", "t0", content="before"),
        _team_run("t0", RunStatus.paused),
        _team_run("t1"),
        session=team,
    )
    _summary(team_store, 3, scope_key="team:crew")

    outcomes = _save(team_store, _member("m0", "t0", content="after"), _team_run("t0"), session=team)

    assert outcomes == ["written", "written"]
    row = _row(team_store, "m0")
    assert row is not None
    assert json.loads(row[2])["content"] == "after"
    assert _seqs(team_store)["t0"] == 4
    assert _loaded_ids(team_store) == ["m0", "t0"]


def test_a_member_of_a_member_raises_and_writes_nothing(team_store: ConversationStore) -> None:
    """Team members are agents, so a run's root is itself or its parent."""
    team = _team()
    _save(team_store, _member("m0", "t0", RunStatus.paused), session=team)

    with pytest.raises(ValueError, match="names member run 'm0'"):
        team_store.save_run(team, _run("deep", parent="m0", agent_id="helper"), record_usage=True)

    assert _row(team_store, "deep") is None
    assert not _has_usage(team_store, "deep")


# --- rule 5: the replay view ------------------------------------------------------------------------------------


def test_system_messages_are_kept_only_while_paused_and_deleted_when_the_run_completes(
    store: ConversationStore,
) -> None:
    """A paused run keeps its prompt for its continuation; finishing deletes that row; the caller's run is untouched."""
    paused = _run("r1", RunStatus.paused)
    _save(store, paused)
    assert [row[0] for row in _message_rows(store, "r1")] == ["r1-system", "r1-user"]
    assert [message.role for message in _loaded(store).runs[0].messages] == ["system", "user"]

    _save(store, _run("r1"))

    assert [row[0] for row in _message_rows(store, "r1")] == ["r1-user"]
    assert [message.role for message in paused.messages] == ["system", "user"]


def test_a_run_reads_its_messages_in_saved_order(store: ConversationStore) -> None:
    """Message rows replay in list order, whatever their ids."""
    messages = [Message(role="user", content=name, id=name, created_at=1) for name in ("b", "c", "a")]
    _save(store, _run("r1", messages=messages))

    for read in (_loaded(store).runs[0], store.get_run("r1"), store.list_runs("s1")[0]):
        assert read is not None
        assert [message.id for message in read.messages] == ["b", "c", "a"]


def test_messages_replayed_from_history_are_never_stored(store: ConversationStore) -> None:
    """A CLI checkpoint's batch holds the provider history it replayed; only the run's own messages are stored."""
    replayed = Message(role="assistant", content="older answer", id="old-answer", from_history=True, created_at=1)
    _save(store, _run("r1", messages=[replayed, Message(role="user", content="now", id="r1-user", created_at=1)]))

    assert [row[0] for row in _message_rows(store, "r1")] == ["r1-user"]


def test_a_message_id_held_twice_raises_and_writes_nothing(store: ConversationStore) -> None:
    """One copy would silently win; the save refuses the list instead."""
    twice = Message(role="user", content="once", id="dup", created_at=1)

    with pytest.raises(ValueError, match="holds a message id twice"):
        _save(store, _run("r1", messages=[twice, copy.copy(twice)]))

    assert _row(store, "r1") is None


def _audited(store: ConversationStore, table: str = "code_sessions_log_messages") -> None:
    _execute(store, "CREATE TABLE audit (change TEXT)")
    for change in ("INSERT", "UPDATE", "DELETE"):
        _execute(
            store,
            f"CREATE TRIGGER audit_{change.lower()} AFTER {change} ON {table} "  # noqa: S608
            f"BEGIN INSERT INTO audit VALUES ('{change}'); END",
        )


def _changes(store: ConversationStore) -> list[str]:
    changes = [change for (change,) in _query(store, "SELECT change FROM audit")]
    _execute(store, "DELETE FROM audit")
    return changes


def test_a_save_rewrites_only_the_message_rows_that_changed(store: ConversationStore) -> None:
    """A checkpoint resaves its whole batch; rows it did not change are not written again."""
    messages = [Message(role="user", content=f"m{index}", id=f"m{index}", created_at=1) for index in range(3)]
    _save(store, _run("r1", RunStatus.cancelled, messages=messages))
    _audited(store)

    _save(store, _run("r1", RunStatus.cancelled, messages=copy.deepcopy(messages)))
    assert _changes(store) == []

    changed = copy.deepcopy(messages)
    changed[1].content = "edited"
    _save(store, _run("r1", RunStatus.cancelled, messages=changed))
    assert _changes(store) == ["UPDATE"]

    _save(store, _run("r1", RunStatus.cancelled, messages=[changed[0], changed[2]]))
    assert sorted(_changes(store)) == ["DELETE", "UPDATE"]
    assert [(row[0], row[1]) for row in _message_rows(store, "r1")] == [("m0", 0), ("m2", 1)]


def _tool(call_id: str, result: str) -> ToolExecution:
    return ToolExecution(tool_call_id=call_id, tool_name="shell", tool_args={"command": call_id}, result=result)


def test_a_save_writes_only_the_tool_rows_that_changed(store: ConversationStore) -> None:
    """A checkpoint after each tool call writes that call's row; earlier calls and their results are not written again."""
    tools = [_tool("c0", "out0"), _tool("c1", "out1")]
    _save(store, _run("r1", RunStatus.cancelled, tools=tools))
    _audited(store, "code_sessions_log_tools")

    grown = [*copy.deepcopy(tools), _tool("c2", "out2")]
    _save(store, _run("r1", RunStatus.cancelled, tools=grown))
    assert _changes(store) == ["INSERT"]

    grown[0].result = "edited"
    _save(store, _run("r1", RunStatus.cancelled, tools=grown))
    assert _changes(store) == ["UPDATE"]

    _save(store, _run("r1", RunStatus.cancelled, tools=grown[:1]))
    assert _changes(store) == ["DELETE", "DELETE"]
    assert _loaded(store).runs[0].tools == grown[:1]


@pytest.mark.parametrize(
    "tools",
    [None, [], [_tool("c0", "first"), _tool("c0", "second")]],
    ids=["none", "empty", "same-id"],
)
def test_tool_executions_load_as_saved_in_their_order(
    store: ConversationStore,
    tools: list[ToolExecution] | None,
) -> None:
    """No tool list stays apart from an empty one, and calls sharing an id, as legacy ones can, keep their own rows."""
    _save(store, _run("r1", RunStatus.cancelled, tools=[_tool("old", "stale")]))

    _save(store, _run("r1", tools=tools))

    assert _loaded(store).runs[0].tools == tools
    assert _query(store, "SELECT ord FROM code_sessions_log_tools ORDER BY ord") == [
        (i,) for i in range(len(tools or []))
    ]


def test_a_folded_message_row_is_never_touched_by_a_save(store: ConversationStore) -> None:
    """A stale copy that still lists a folded message, changed or not, leaves the row folded with its data."""
    messages = [Message(role="user", content=f"m{index}", id=f"m{index}", created_at=1) for index in range(3)]
    _save(store, _run("r1", RunStatus.paused, messages=messages))
    summary_id = _fold(store, _run("r1", RunStatus.running), messages[:2])

    stale = copy.deepcopy(messages)
    stale[0].content = "rewritten"
    _save(store, _run("r1", RunStatus.paused, messages=stale))
    _save(store, _run("r1", RunStatus.paused, messages=[messages[2]]))

    assert sorted((row[0], row[2], row[3]["content"]) for row in _message_rows(store, "r1")) == [
        ("m0", summary_id, "m0"),
        ("m1", summary_id, "m1"),
        ("m2", None, "m2"),
    ]
    assert [message.id for message in _loaded(store).runs[0].messages] == ["m2"]


# --- P4 ---------------------------------------------------------------------------------------------------------


def test_an_interrupted_record_replaces_a_visible_run_in_place(store: ConversationStore) -> None:
    """Voice playout reconciles a stored answer to what was spoken; the run keeps its place and records no usage."""
    _save(store, _run("r0", content="It is sunny."), _run("r1"))
    session = _loaded(store)

    assert store.save_interrupted_record(session, _run("r0", content="It is", metrics=_usage(5))) == "written"

    assert _seqs(store) == {"r0": 1, "r1": 2}
    assert [(run.run_id, run.content) for run in session.runs] == [("r0", "It is"), ("r1", None)]
    assert _as_stored(session.runs) == _as_stored(_loaded(store).runs)
    assert not _has_usage(store, "r0")


def test_an_interrupted_record_replaces_a_covered_run_without_changing_replay(store: ConversationStore) -> None:
    """A covered run does not replay, so replacing it changes what readers of the run see, not the replay."""
    _save(store, _run("r0", content="It is sunny."), _run("r1"))
    _summary(store, 1)

    assert store.save_interrupted_record(_session(), _run("r0", content="It is")) == "written"

    assert _loaded_ids(store) == ["r1"]
    stored = store.get_run("r0")
    assert stored is not None
    assert stored.content == "It is"


# --- P2 ---------------------------------------------------------------------------------------------------------


def test_a_metadata_patch_sets_its_keys_and_keeps_the_rest(store: ConversationStore) -> None:
    """Any stored run takes the patch, a finished one included; a run without a row does not."""
    _save(store, _run("r1", metadata={"a": 1, "b": 2}))
    before = _row(store, "r1")

    assert store.patch_run_metadata("s1", "r1", {"b": 3, "c": 4})
    assert not store.patch_run_metadata("s1", "missing", {"b": 3})
    assert not store.patch_run_metadata("s2", "r1", {"b": 5})

    row = _row(store, "r1")
    assert before is not None
    assert row is not None
    assert json.loads(row[3]) == {"a": 1, "b": 3, "c": 4}
    assert row[:3] == before[:3]


def test_a_metadata_patch_of_a_covered_run_joins_the_covering_summarys_event_sets(store: ConversationStore) -> None:
    """A reply linked to a covered run becomes a redaction match of the summary that covers the run."""
    _save(store, _run("r1"), _run("r2"), _run("r3"))
    first = _summary(store, 1, event_ids=("$e1",), seen=("$e1",))
    second = _summary(store, 2, event_ids=("$e2",))

    store.patch_run_metadata("s1", "r2", {"matrix_response_event_id": "$reply", "matrix_source_event_ids": ["$asked"]})
    store.patch_run_metadata("s1", "r1", {"matrix_seen_event_ids": ["$read"]})
    store.patch_run_metadata("s1", "r3", {"matrix_response_event_id": "$uncovered"})

    assert _summary_sets(store, first) == (["$e1", "$read"], ["$e1", "$read"])
    assert _summary_sets(store, second) == (["$asked", "$e2", "$reply"], ["$reply"])


def test_a_metadata_patch_joins_nothing_behind_the_latest_barrier(store: ConversationStore) -> None:
    """A retired run is no summary's; a later summary that reaches past it did not cover it."""
    _save(store, _run("r1"), _run("r2"), _run("r3"))
    retired = _summary(store, 1)
    _summary(store, 1, summary=None)
    middle = _summary(store, 2)
    _summary(store, 2, summary=None)
    later = _summary(store, 3)

    store.patch_run_metadata("s1", "r1", {"matrix_response_event_id": "$first"})
    store.patch_run_metadata("s1", "r2", {"matrix_response_event_id": "$second"})
    store.patch_run_metadata("s1", "r3", {"matrix_response_event_id": "$third"})

    assert [_summary_sets(store, row_id) for row_id in (retired, middle, later)] == [
        ([], []),
        ([], []),
        (["$third"], ["$third"]),
    ]


def test_a_metadata_patch_of_a_run_that_is_not_visible_joins_nothing(team_store: ConversationStore) -> None:
    """Only a visible top-level run is covered; a paused run and a member are not, even by their own scope's row."""
    team = _team()
    _save(team_store, _member("m1", "t2"), _team_run("t2", RunStatus.paused), _team_run("t3"), session=team)
    row_id = _summary(team_store, 3, scope_key="team:crew")
    member_scope = _summary(team_store, 3, scope_key="agent:helper")

    team_store.patch_run_metadata("s1", "m1", {"matrix_response_event_id": "$member"})
    team_store.patch_run_metadata("s1", "t2", {"matrix_response_event_id": "$paused"})
    team_store.patch_run_metadata("s1", "t3", {"matrix_response_event_id": "$covered"})

    assert _summary_sets(team_store, row_id) == (["$covered"], ["$covered"])
    assert _summary_sets(team_store, member_scope) == ([], [])


def test_a_metadata_patch_replaces_the_sessions_copy_in_a_new_list(store: ConversationStore) -> None:
    """Loaded runs are shared, so the patch swaps in a changed copy and leaves the loaded object as it was."""
    _save(store, _run("r1", metadata={"a": 1}), _run("r2"))
    session = _loaded(store)
    runs, loaded = session.runs, session.runs[0]

    store.patch_run_metadata("s1", "r1", {"b": 2}, session=session)

    assert session.runs is not runs
    assert loaded.metadata == {"a": 1}
    assert session.runs[0] is not loaded
    assert session.runs[1] is runs[1]
    assert _as_stored(session.runs) == _as_stored(_loaded(store).runs)


# --- deletes ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_run_that_is_no_turn_takes_its_member_rows_and_fences_nothing(
    team_store: ConversationStore,
) -> None:
    """Its terminal save deletes the rows it held, its member rows too; it records no removed run and keeps summaries."""
    team = _team()
    _save(team_store, _member("m0", "t0"), _member("m1", "t0"), _team_run("t0"), _team_run("t1"), session=team)
    seed_session_row(team_store, _session("s2", kind="team", owner="crew"))
    team_store.save_run(_session("s2", kind="team", owner="crew"), _member("elsewhere", "t0"), record_usage=False)
    _summary(team_store, 4, scope_key="team:crew")
    session = _loaded(team_store)
    session.runs = [*session.runs, _member("m0", "t0"), _member("m1", "t0"), _team_run("t0")]

    await persist_to(team_store, session, replays=lambda _record: False)(_team_run("t0"))

    assert _query(team_store, "SELECT run_id FROM code_sessions_log_runs WHERE session_id = 's1'") == [("t1",)]
    assert _query(team_store, "SELECT run_id FROM code_sessions_log_runs WHERE parent_run_id = 't0'") == [
        ("elsewhere",),
    ]
    assert _query(team_store, "SELECT DISTINCT run_id FROM code_sessions_log_messages") == [("elsewhere",)]
    assert _query(team_store, "SELECT * FROM code_sessions_log_removed") == []
    assert _query(team_store, "SELECT COUNT(*) FROM code_sessions_log_summaries") == [(1,)]
    assert [run.run_id for run in session.runs] == []


def test_runs_without_removes_the_runs_and_their_members() -> None:
    """Team members are agents, so one level of members goes with each run."""
    runs: list[AnyRunRecord] = [
        RunRecord(run_id="unrelated"),
        RunRecord(run_id="member", parent_run_id="root"),
        RunRecord(run_id="root"),
    ]

    assert [run.run_id for run in runs_without(runs, ["root"])] == ["unrelated"]


# --- reads ------------------------------------------------------------------------------------------------------


def test_load_returns_uncovered_and_non_visible_runs_with_their_members_in_seq_order(
    team_store: ConversationStore,
) -> None:
    """Every non-visible run loads wherever it sits, members follow loaded runs only, and orphans never load."""
    team = _team()
    _save(
        team_store,
        _member("m-paused", "t-paused", RunStatus.paused),
        _team_run("t-paused", RunStatus.paused),
        _member("m-covered", "t-covered"),
        _team_run("t-covered"),
        _team_run("t-cancelled", RunStatus.cancelled),
        _member("orphan", "missing"),
        _team_run("t-new"),
        session=team,
    )
    _summary(team_store, 6, scope_key="team:crew")

    assert _loaded_ids(team_store) == ["m-paused", "t-paused", "t-cancelled", "t-new"]


def test_cancelled_and_errored_runs_behind_the_summary_still_load(store: ConversationStore) -> None:
    """Every non-visible run loads wherever it sits."""
    _save(store, _run("c0", RunStatus.cancelled), _run("e1", RunStatus.error), _run("r2"), _run("r3"))
    _summary(store, 3)

    assert _loaded_ids(store) == ["c0", "e1", "r3"]


def test_load_covers_runs_by_their_own_scopes_latest_summary_and_replays_the_owner_scopes(
    store: ConversationStore,
) -> None:
    """Another scope's summary covers none of this scope's runs; a barrier covers but replays no text."""
    _save(store, _run("r1"), _run("r2"), _run("r3"), _run("unowned", agent_id=""))
    _summary(store, 1, summary="first")
    _summary(store, 4, scope_key="agent:other", summary="another scope")
    session = _loaded(store)
    assert session.summary is not None
    assert session.summary.text == "first"
    assert [run.run_id for run in session.runs] == ["r2", "r3", "unowned"]

    _summary(store, 2, summary=None)

    session = _loaded(store)
    assert session.summary is None
    assert [run.run_id for run in session.runs] == ["r3", "unowned"]


def test_a_load_names_the_databases_owner_and_returns_none_for_a_missing_session(team_store: ConversationStore) -> None:
    """A loaded session's kind and owner are the database's; a session it does not hold loads as None."""
    _save(team_store, _team_run("t1"), session=_team())

    assert team_store.load_session("missing") is None
    loaded = _loaded(team_store)
    assert (loaded.kind, loaded.owner_id, [run.run_id for run in loaded.runs]) == ("team", "crew", ["t1"])


def test_a_new_database_reads_as_empty(store: ConversationStore) -> None:
    """A database opened before anything was written to it reads as empty instead of failing."""
    assert store.load_session("s1") is None
    assert store.read_session_metadata("s1") is None
    assert store.list_runs("s1") == []
    assert store.get_run("r1") is None
    assert store.list_updated_sessions(since=0) == []


def test_get_run_returns_a_paused_team_run_with_its_paused_member_runs(team_store: ConversationStore) -> None:
    """The members are the team run's paused member rows in log order, each a copy of its own row.

    A member that finished and a paused run another team run owns are no member, and the team waits on its members'
    paused calls.
    """
    team = _team()
    replying = Message(role="assistant", content="ok", id="m1-reply", provider_data={"k": ["v"]}, created_at=1)
    m2 = _member("m2", "t0", RunStatus.paused, tools=[_waiting("c1"), _waiting("c3")])
    m1 = _member("m1", "t0", RunStatus.paused, tools=[_waiting("c2")])
    m1.messages.append(replying)
    _save(team_store, m2, _member("done", "t0"), m1, session=team)
    _save(team_store, _member("other", "t9", RunStatus.paused), _team_run("t9", RunStatus.paused), session=team)
    _save(team_store, _team_run("t0", RunStatus.paused), session=team)

    paused = team_store.get_run("t0")

    assert isinstance(paused, TeamRunRecord)
    assert [member.run_id for member in paused.member_responses] == ["m2", "m1"]
    assert [message.id for message in paused.member_responses[0].messages] == ["m2-system", "m2-user"]
    assert [(item.member_run_id, item.tool_execution.tool_call_id) for item in paused.requirements] == [
        ("m2", "c1"),
        ("m2", "c3"),
        ("m1", "c2"),
    ]
    loaded = {run.run_id: run for run in _loaded(team_store).runs}
    loaded_team = loaded["t0"]
    assert isinstance(loaded_team, TeamRunRecord)
    assert loaded_team.member_responses[1] is not loaded["m1"]
    assert loaded_team.member_responses[1].messages[-1].provider_data is not loaded["m1"].messages[-1].provider_data
    assert loaded_team.member_responses[1].to_dict() == loaded["m1"].to_dict()


def test_a_team_run_that_is_not_paused_reads_without_members(team_store: ConversationStore) -> None:
    """Only a paused team run's resume reads its members from the team run, even a member that stayed paused."""
    team = _team()
    _save(team_store, _member("m1", "t0", RunStatus.paused, tools=[_waiting("c1")]), session=team)
    _save(team_store, _team_run("t0"), session=team)

    finished = team_store.get_run("t0")

    assert isinstance(finished, TeamRunRecord)
    assert finished.member_responses == []
    assert team_store.get_run("missing") is None


def test_list_runs_returns_every_run_with_members_in_seq_order_in_its_replay_view(
    team_store: ConversationStore,
) -> None:
    """Covered runs and members are listed too, and a crash-left copy hides what its fold took."""
    team = _team()
    _save(team_store, _team_run("t0"), _member("m1", "t2"), _team_run("t2", RunStatus.cancelled), session=team)
    _summary(team_store, 1, scope_key="team:crew")
    _fold(
        team_store,
        _team_run("t2", RunStatus.running),
        [Message(role="user", content="m", id="folded", created_at=1)],
    )

    listed = team_store.list_runs("s1")

    assert [run.run_id for run in listed] == ["t0", "m1", "t2"]
    assert listed[2].messages == []
    stored = team_store.get_run("t2")
    assert stored is not None
    assert stored.messages == []


def test_logged_runs_list_top_level_runs_in_log_order_with_a_finished_approval_last(
    team_store: ConversationStore,
) -> None:
    """A run that finishes after its approval moves past the runs that finished meanwhile; members are left out."""
    team = _team()
    _save(team_store, _team_run("t0", RunStatus.paused), _member("m1", "t1"), _team_run("t1"), session=team)
    first = team_store.logged_runs("s1")
    assert [(logged.run.run_id, logged.run.status) for logged in first] == [
        ("t0", RunStatus.paused),
        ("t1", RunStatus.completed),
    ]

    _save(team_store, _team_run("t0"), session=team)

    logged = team_store.logged_runs("s1")
    assert [turn.run.run_id for turn in logged] == ["t1", "t0"]
    assert logged[1].seq > first[-1].seq


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[str], None]]:
    """Statement hooks called as each store connection starts executing a statement."""
    hooks: list[Callable[[str], None]] = []
    connect = store_module.connect_conversation_database

    def traced_connect(path: Path) -> sqlite3.Connection:
        connection = connect(path)
        connection.set_trace_callback(lambda statement: [hook(statement) for hook in hooks])
        return connection

    monkeypatch.setattr(store_module, "connect_conversation_database", traced_connect)
    return hooks


def _write_now(store: ConversationStore, sql: str, errors: list[str]) -> None:
    writer = sqlite3.connect(store.path, timeout=0)
    try:
        writer.execute(sql)
        writer.commit()
    except sqlite3.OperationalError as error:
        errors.append(str(error))
    finally:
        writer.close()


def test_load_reads_one_snapshot(store: ConversationStore, traced: list[Callable[[str], None]]) -> None:
    """A commit cannot land between the reads of one load, so runs, messages, and summary always agree."""
    _save(store, _run("r0"), _run("r1"))
    errors: list[str] = []
    selects = 0

    def write_between_reads(statement: str) -> None:
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1
            if selects == 2:
                _write_now(
                    store,
                    "INSERT INTO code_sessions_log_summaries (session_id, scope_key, through, summary, created_at) "
                    "VALUES ('s1', 'agent:code', 2, 'raced', 1700000000)",
                    errors,
                )

    traced.append(write_between_reads)
    session = _loaded(store)

    assert errors == ["database is locked"]
    assert [run.run_id for run in session.runs] == ["r0", "r1"]
    assert session.summary is None


@pytest.mark.parametrize("table", ["code_sessions_log_messages", "code_sessions_log_tools"])
def test_saves_and_loads_read_messages_and_tools_in_order_without_a_sort(
    store: ConversationStore,
    traced: list[Callable[[str], None]],
    table: str,
) -> None:
    """Message and tool reads walk an index in ``ord`` order; a sort would spill a large run's rows to a temp file."""
    reads: list[str] = []

    def record(statement: str) -> None:
        if statement.lstrip().upper().startswith("SELECT") and table in statement:
            reads.append(statement)

    traced.append(record)
    _save(store, _run("r1"))
    _loaded(store)

    assert len(reads) == 2
    for read in reads:
        plan = [step for *_, step in _query(store, f"EXPLAIN QUERY PLAN {read}")]
        assert [step for step in plan if "TEMP B-TREE" in step] == [], plan


# --- the session mirror -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_persisted_run_joins_the_session_as_its_row_holds_it(store: ConversationStore) -> None:
    """The session's copy is read back from the rows, never the record handed over, so its prompt stays out."""
    _save(store, _run("r1"))
    session = _loaded(store)
    record = _run("r2")

    await persist_to(store, session)(record)

    assert _as_stored(session.runs) == _as_stored(_loaded(store).runs)
    assert [message.role for message in session.runs[-1].messages] == ["user"]
    assert session.runs[-1] is not record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saved_status", "expected"),
    [(RunStatus.completed, ["r1", "r0"]), (RunStatus.paused, ["r0", "r1"])],
    ids=["finished-moves-last", "still-paused-stays"],
)
async def test_the_session_places_a_saved_run_where_the_log_does(
    store: ConversationStore,
    saved_status: RunStatus,
    expected: list[str],
) -> None:
    """A paused run saved visible goes after the turns it waited for; one that keeps its place keeps it."""
    _save(store, _run("r0", RunStatus.paused), _run("r1"))
    session = _loaded(store)

    await persist_to(store, session)(_run("r0", saved_status, content="resumed"))

    assert [run.run_id for run in session.runs] == expected
    assert _as_stored(session.runs) == _as_stored(_loaded(store).runs)


@pytest.mark.asyncio
async def test_a_removed_save_takes_the_run_and_its_members_out_of_the_session(team_store: ConversationStore) -> None:
    """A run a cut removed while it waited leaves the session with its member runs, as it left the log."""
    team = _team()
    _save(
        team_store,
        _member("m0", "t0", RunStatus.paused),
        _team_run("t0", RunStatus.paused),
        _team_run("t1"),
        session=team,
    )
    session = _loaded(team_store)
    delete_runs(team_store, ["t0"], session_id="s1")
    _remove(team_store, "t0")

    await persist_to(team_store, session)(_team_run("t0", RunStatus.cancelled))

    assert [run.run_id for run in session.runs] == ["t1"]
    assert _as_stored(session.runs) == _as_stored(_loaded(team_store).runs)


@pytest.mark.asyncio
async def test_a_resumed_team_run_keeps_its_member_runs_in_the_session(team_store: ConversationStore) -> None:
    """A member keeps its place and its copy when its team run's save moves the team run after a later turn."""
    team = _team()
    _save(
        team_store,
        _member("m0", "t0", RunStatus.paused),
        _team_run("t0", RunStatus.paused),
        _team_run("t1"),
        session=team,
    )
    session = _loaded(team_store)
    persist = persist_to(team_store, session)

    await persist(_member("m0", "t0", content="checked"))
    await persist(_team_run("t0", content="answered"))

    assert [run.run_id for run in session.runs] == ["m0", "t1", "t0"]
    assert _as_stored(session.runs) == _as_stored(_loaded(team_store).runs)


@pytest.mark.asyncio
async def test_a_final_save_leaves_the_sessions_copy_as_stored(store: ConversationStore) -> None:
    """A run another writer finished since the session loaded refuses the save; the session keeps its copy."""
    _save(store, _run("r1", RunStatus.paused, content="waiting"))
    session = _loaded(store)
    _save(store, _run("r1", content="finished"))

    await persist_to(store, session)(_run("r1", RunStatus.cancelled, content="stale"))

    [kept] = session.runs
    assert kept.content == "waiting"


def test_a_failed_save_writes_nothing_and_leaves_the_session_as_loaded(
    store: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save is one transaction; when its message write fails, neither the row nor ``session.runs`` change."""
    _save(store, _run("r1"))
    session = _loaded(store)
    loaded = list(session.runs)

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store_module, "set_visible_messages", fail)

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        store.save_run(session, _run("r2"), record_usage=True)

    assert session.runs == loaded
    assert _row(store, "r2") is None
    assert not _has_usage(store, "r2")


@pytest.mark.asyncio
async def test_a_cancelled_persist_still_leaves_the_session_as_stored(
    store: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cancelled while its save runs finds the session holding what the save wrote."""
    _save(store, _run("r1"))
    session = _loaded(store)
    writing, release = threading.Event(), threading.Event()
    original_write = store_module._write_run

    def held_write(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        writing.set()
        release.wait(10)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(store_module, "_write_run", held_write)
    save = asyncio.create_task(persist_to(store, session)(_run("r2")))
    assert await asyncio.to_thread(writing.wait, 10)
    save.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await save
    assert [run.run_id for run in session.runs] == ["r1", "r2"]
    assert _as_stored(session.runs) == _as_stored(_loaded(store).runs)


# --- folds ------------------------------------------------------------------------------------------------------


def test_a_fold_moves_its_stored_run_after_its_summary_and_flags_what_it_took(store: ConversationStore) -> None:
    """A resumed approval's row moves; stored messages keep their data and gain the flag, earlier folds keep theirs."""
    messages = [Message(role="user", content=f"m{index}", id=f"m{index}", created_at=1) for index in range(3)]
    _save(store, _run("r0", RunStatus.paused, messages=messages[:2]), _run("r1"))
    first = _fold(store, _run("r0", RunStatus.running), messages[:1])
    rewritten = [Message(role="user", content="new", id="m1", created_at=1), messages[2]]

    second = _fold(store, _run("r0", RunStatus.running), [messages[0], *rewritten])

    assert _seqs(store) == {"r0": 4, "r1": 2}
    assert [(row[0], row[2], row[3]["content"]) for row in _message_rows(store, "r0")] == [
        ("m0", first, "m0"),
        ("m1", second, "m1"),
        ("m2", second, "m2"),
    ]
    assert _query(store, "SELECT next_seq FROM code_sessions_log_meta") == [(5,)]


def test_a_fold_of_a_run_without_a_row_stores_it_cancelled_with_its_live_scalars_and_metadata(
    store: ConversationStore,
) -> None:
    """A crash after the fold leaves a run that does not replay; its tools wait for its own save."""
    live = _run("r0", RunStatus.running, content="so far", metadata={"matrix_event_id": "$asked"})
    live.tools = [_waiting("c1")]
    folded = [Message(role="user", content="question", id="q", created_at=1)]
    seed_session_row(store, _session())

    summary_id = _fold(store, live, folded)

    stored = store.get_run("r0")
    assert stored is not None
    assert (stored.status, stored.content, stored.metadata) == (
        RunStatus.cancelled,
        "so far",
        {"matrix_event_id": "$asked"},
    )
    assert (stored.tools, stored.messages) == (None, [])
    assert [(row[0], row[2]) for row in _message_rows(store, "r0")] == [("q", summary_id)]
    assert _seqs(store) == {"r0": 1}
    assert _loaded_ids(store) == ["r0"]


def test_a_fold_flags_the_member_runs_it_names_that_have_rows(team_store: ConversationStore) -> None:
    """Members that ended before the fold go with it; another member, and one an earlier fold took, keep theirs."""
    team = _team()
    _save(team_store, _member("m0", "t0"), _member("m1", "t0"), _member("m2", "t0"), session=team)
    first = _fold(team_store, _team_run("t0", RunStatus.running), [], member_run_ids=("m2",))

    second = _fold(team_store, _team_run("t0", RunStatus.running), [], member_run_ids=("m0", "m2", "unstored"))

    assert dict(
        _query(team_store, "SELECT run_id, folded_by FROM code_sessions_log_runs WHERE parent_run_id = 't0'"),
    ) == {
        "m0": second,
        "m1": None,
        "m2": first,
    }


# --- requester, session state, and metadata ---------------------------------------------------------------------


def _requested_run(run_id: str, requester_id: str | None) -> RunRecord:
    run = _run(run_id)
    run.user_id = requester_id
    return run


def _row_requester(store: ConversationStore) -> str | None:
    [(user_id,)] = _query(store, "SELECT user_id FROM code_sessions")
    return user_id


def test_ensure_session_row_creates_a_missing_row_from_the_session(store: ConversationStore) -> None:
    """A new conversation's row is created from the session and returns the state it holds; an existing row stays."""
    session = _session()
    session.session_state = {"key": "value"}
    later = _session(owner="other")
    later.session_state = {"other": True}

    with store.transaction(write=True) as connection:
        created = ensure_session_row(connection, store.names, session)
    with store.transaction(write=True) as connection:
        kept = ensure_session_row(connection, store.names, later)

    loaded = _loaded(store)
    assert (loaded.owner_id, loaded.session_state, loaded.created_at) == ("code", {"key": "value"}, 1_700_000_000)
    assert (created, kept) == ({"key": "value"}, None)
    assert created is not session.session_state


def test_a_session_row_keeps_the_first_requester_a_written_run_named(store: ConversationStore) -> None:
    """A row created without a requester takes its first run's; a later run of another requester or none keeps it."""
    session = _session()
    seed_session_row(store, session)
    assert _row_requester(store) is None

    _save(store, _requested_run("r0", None), _requested_run("r1", "@alice:example.test"), session=session)
    _save(store, _requested_run("r2", "@bob:example.test"), _requested_run("r3", None), session=session)

    assert _row_requester(store) == "@alice:example.test"


def test_a_final_or_removed_run_does_not_name_the_requester(store: ConversationStore) -> None:
    """Only a run the store wrote names the row's requester."""
    _save(store, _run("r0"))
    _remove(store, "gone")

    _save(store, _requested_run("r0", "@alice:example.test"), _requested_run("gone", "@alice:example.test"))

    assert _row_requester(store) is None


@pytest.mark.asyncio
async def test_persist_to_saves_the_record_with_its_usage_and_the_session_state_only_when_it_changed(
    store: ConversationStore,
) -> None:
    """The callback saves each record with its usage, and writes ``session_state`` back only after tools changed it."""
    _persist(store, _session(), _run("r1", metrics=_usage(10)))
    session = _loaded(store)
    persist = persist_to(store, session)

    await persist(_run("r2", metrics=_usage(5)))
    assert _totals(store)["input_tokens"] == 15
    assert _loaded_ids(store) == ["r1", "r2"]
    # Another writer's state stays while this session's tools left theirs alone.
    _execute(
        store,
        """UPDATE code_sessions SET session_state = json('{"other": 1}')""",
    )
    await persist(_run("r3"))
    assert _loaded(store).session_state == {"other": 1}

    session.session_state["cwd"] = "/repo"
    await persist(_run("r4"))

    assert _loaded(store).session_state == {"cwd": "/repo"}
    assert _loaded_ids(store) == ["r1", "r2", "r3", "r4"]


@pytest.mark.asyncio
async def test_persist_to_writes_back_a_session_state_restored_after_an_earlier_save(store: ConversationStore) -> None:
    """A state changed and saved with one record, then restored before the next, is stored restored."""
    _save(store, _run("r1"))
    session = _loaded(store)
    session.session_state = {"cwd": "/repo"}
    seed_session_state(store, session, session_state_snapshot(session.session_state))
    persist = persist_to(store, session)

    session.session_state["cwd"] = "/repo/sub"
    await persist(_run("r2"))
    assert _loaded(store).session_state == {"cwd": "/repo/sub"}
    session.session_state["cwd"] = "/repo"
    await persist(_run("r3"))

    assert _loaded(store).session_state == {"cwd": "/repo"}
    assert _loaded_ids(store) == ["r1", "r2", "r3"]


@pytest.mark.asyncio
async def test_persist_to_stores_a_record_and_its_state_together_or_neither(store: ConversationStore) -> None:
    """A state write that fails takes its record with it, so a paused run never stores without its state."""
    _save(store, _run("r1"))
    session = _loaded(store)
    session.session_state = {"cwd": "/repo"}
    seed_session_state(store, session, session_state_snapshot(session.session_state))
    persist = persist_to(store, session)
    _execute(
        store,
        "CREATE TRIGGER refuse_state BEFORE UPDATE OF session_state ON code_sessions "
        "WHEN json_extract(NEW.session_state, '$.cwd') = '/repo/sub' "
        "BEGIN SELECT RAISE(ABORT, 'state refused'); END",
    )
    session.session_state["cwd"] = "/repo/sub"

    with pytest.raises(sqlite3.IntegrityError, match="state refused"):
        await persist(_run("r2", RunStatus.paused))
    assert (_loaded_ids(store), _has_usage(store, "r2")) == (["r1"], False)
    assert _loaded(store).session_state == {"cwd": "/repo"}
    assert session.stored_session_state == {"cwd": "/repo"}

    _execute(store, "DROP TRIGGER refuse_state")
    await persist(_run("r2", RunStatus.paused))

    assert _loaded(store).session_state == {"cwd": "/repo/sub"}
    assert _loaded_ids(store) == ["r1", "r2"]


@pytest.mark.asyncio
async def test_persist_to_compares_the_state_with_what_the_session_loaded_or_last_wrote(
    store: ConversationStore,
) -> None:
    """A state the turn left alone keeps another writer's; one saved changed mid-turn and restored is stored restored."""
    _save(store, _run("r1"))
    seeded = _loaded(store)
    seeded.session_state = {"cwd": "/repo"}
    seed_session_state(store, seeded, session_state_snapshot(seeded.session_state))
    session = _loaded(store)
    persist = persist_to(store, session)
    _execute(
        store,
        """UPDATE code_sessions SET session_state = json('{"cwd": "/other"}')""",
    )

    await persist(_run("r2"))
    assert _loaded(store).session_state == {"cwd": "/other"}

    session.session_state["cwd"] = "/repo/sub"
    seed_session_state(store, session, session_state_snapshot(session.session_state))
    session.session_state["cwd"] = "/repo"
    await persist(_run("r3"))

    assert _loaded(store).session_state == {"cwd": "/repo"}


@pytest.mark.asyncio
async def test_the_save_that_creates_the_session_row_records_the_state_it_stored(store: ConversationStore) -> None:
    """A created row holds the session's state, so a terminal save that leaves it alone keeps another writer's."""
    session = _session()
    session.session_state["cwd"] = "/repo"

    store.save_run(session, _run("r1"), record_usage=False)

    assert session.stored_session_state == {"cwd": "/repo"}
    assert session.stored_session_state is not session.session_state
    _execute(
        store,
        """UPDATE code_sessions SET session_state = json('{"cwd": "/other"}')""",
    )
    await persist_to(store, session)(_run("r2"))
    assert _loaded(store).session_state == {"cwd": "/other"}


@pytest.mark.asyncio
async def test_a_run_save_keeps_the_state_in_its_column_and_its_usage_in_the_ledger(store: ConversationStore) -> None:
    """A row keeps its state in its own column, and a save recording usage writes the ledger, not ``session_data``."""
    session = _session()
    session.session_state["cwd"] = "/repo"
    store.save_run(session, _run("r1"), record_usage=False)
    assert _query(store, "SELECT session_state, session_data FROM code_sessions") == [('{"cwd": "/repo"}', None)]

    session.session_state["cwd"] = "/repo/sub"
    await persist_to(store, session)(_run("r2", metrics=_usage(10)))

    assert _query(store, "SELECT session_state, session_data FROM code_sessions") == [('{"cwd": "/repo/sub"}', None)]
    assert _usage_row(store, "r2")["metrics"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_saves_read_a_session_state_another_thread_keeps_changing(store: ConversationStore) -> None:
    """Saves copy ``session_state`` while another thread changes it in place, and each records what its row holds.

    The lane saves on its worker thread while tools change the live state on the event loop, and a sync tool changes
    it from its own thread, so a copy must read it in one step no other thread runs during: a copy that walks the
    dict in Python fails with "dictionary changed size during iteration". A tiny switch interval hands the
    interpreter to the changing thread every few bytecodes, so a walking copy fails on the first save.
    """
    live: dict[str, Any] = {f"key-{index}": [index] for index in range(2_000)}
    stop = threading.Event()

    def churn() -> None:
        # One change per pass, so the thread can hand the interpreter over with the extra key present.
        while not stop.is_set():
            if live.pop("churn", None) is None:
                live["churn"] = True

    sessions = [
        ConversationSession(session_id=f"s{index}", kind="agent", owner_id="code", session_state=live)
        for index in range(3)
    ]
    switch_interval = sys.getswitchinterval()
    changer = threading.Thread(target=churn)
    sys.setswitchinterval(1e-6)
    changer.start()
    try:
        for index, session in enumerate(sessions):
            # The first terminal save creates the row, and the second writes the state again once it changed.
            persist = persist_to(store, session)
            await persist(_run(f"r{index}"))
            assert session.stored_session_state == stored_session_state(store, session.session_id)
            await persist(_run(f"r{index}-again"))
            assert session.stored_session_state == stored_session_state(store, session.session_id)
    finally:
        stop.set()
        changer.join()
        sys.setswitchinterval(switch_interval)

    assert all(session.stored_session_state is not live for session in sessions)


def test_a_metadata_update_leaves_the_state_and_the_totals_alone(store: ConversationStore) -> None:
    """A metadata update writes only ``metadata``, so it moves neither the state nor the totals."""
    _persist(store, _session(), _run("r1", metrics=_usage(10)))
    session = _loaded(store)
    session.session_state = {"key": "value"}
    seed_session_state(store, session, session_state_snapshot(session.session_state))

    store.update_metadata(session, lambda _metadata: {"mindroom_matrix_history": {"version": 1}})

    reloaded = _loaded(store)
    assert (reloaded.metadata, reloaded.session_state) == (
        {"mindroom_matrix_history": {"version": 1}},
        {"key": "value"},
    )
    assert session.metadata == reloaded.metadata
    assert _totals(store)["input_tokens"] == 10


def test_a_metadata_update_reads_and_writes_under_the_write_lock(store: ConversationStore) -> None:
    """No writer lands between the metadata the update reads and the metadata it stores."""
    seed_session_row(store, _session())
    errors: list[str] = []

    def update(metadata: dict[str, Any] | None) -> dict[str, Any]:
        _write_now(store, """UPDATE code_sessions SET metadata = '{"raced": 1}'""", errors)
        return {**(metadata or {}), "k": "v"}

    store.update_metadata(_session(), update)

    assert errors == ["database is locked"]
    assert _query(store, "SELECT metadata FROM code_sessions") == [('{"k": "v"}',)]


@pytest.mark.parametrize("create", ["save_run", "persist_to", "save_independent_usage"])
def test_every_writer_of_a_session_row_stores_absent_metadata_as_json_null(
    store: ConversationStore,
    create: str,
) -> None:
    """Agno's JSON column stored no metadata as the text ``null``; no writer stores SQL NULL or an empty object."""
    session = ConversationSession(session_id="s1", kind="agent", owner_id="code")
    writes = {
        "save_run": lambda: store.save_run(session, _run("r1"), record_usage=False),
        "persist_to": lambda: _persist(store, session, _run("r1")),
        "save_independent_usage": lambda: store.save_independent_usage(
            session_id="s1",
            usage_id="u1",
            kind="compaction_summary",
            requester_id=None,
            usage=HelperUsage(run_id="u1", model="m", provider="P", metrics=RunMetrics(), created_at=1),
            initial_session=session,
        ),
    }

    writes[create]()

    assert _query(store, "SELECT metadata, typeof(metadata) FROM code_sessions") == [("null", "text")]


_ROW_WRITERS = ["save_run", "save_interrupted_record", "update_metadata", "save_independent_usage"]


def _write_row(store: ConversationStore, session: ConversationSession, writer: str) -> None:
    """Make one write of ``writer`` kind into ``session``, which creates its row when it has none."""
    if writer == "save_run":
        store.save_run(session, _run("r1"), record_usage=False)
    elif writer == "save_interrupted_record":
        store.save_interrupted_record(session, _run("r1", RunStatus.error))
    elif writer == "update_metadata":
        store.update_metadata(session, lambda _metadata: {"k": "v"})
    else:
        store.save_independent_usage(
            session_id=session.session_id,
            usage_id="u1",
            kind="compaction_summary",
            requester_id=None,
            usage=HelperUsage(run_id="u1", model="m", provider="P", metrics=RunMetrics(), created_at=1),
            initial_session=session,
        )


@pytest.mark.parametrize("writer", _ROW_WRITERS)
def test_a_write_that_creates_the_session_row_reports_it(store: ConversationStore, writer: str) -> None:
    """Each write that inserts a session's row tells the block watching that session, once it commits."""
    with watch_session_row(HistoryScope(kind="agent", scope_id="code"), "s1") as watch:
        _write_row(store, _session(), writer)

    assert watch.created


@pytest.mark.parametrize("writer", _ROW_WRITERS)
def test_a_write_to_an_existing_row_reports_nothing(store: ConversationStore, writer: str) -> None:
    """A write into a row that another write created first leaves the watch alone."""
    seed_session_row(store, _session())

    with watch_session_row(HistoryScope(kind="agent", scope_id="code"), "s1") as watch:
        _write_row(store, _session(), writer)

    assert not watch.created


def test_a_creating_write_that_rolls_back_reports_nothing(store: ConversationStore) -> None:
    """A write whose transaction fails after inserting the row leaves no row and reports none."""
    _execute(
        store,
        "CREATE TRIGGER fail_usage BEFORE INSERT ON code_sessions_usage BEGIN SELECT RAISE(ABORT, 'usage refused'); END",
    )

    with (
        watch_session_row(HistoryScope(kind="agent", scope_id="code"), "s1") as watch,
        pytest.raises(sqlite3.IntegrityError, match="usage refused"),
    ):
        _write_row(store, _session(), "save_independent_usage")

    assert not watch.created
    assert _query(store, "SELECT session_id FROM code_sessions") == []


def test_a_created_row_of_another_session_or_scope_reports_nothing(store: ConversationStore) -> None:
    """Only the watched session's row in the watched scope counts, not a row of the same id another scope owns."""
    with watch_session_row(HistoryScope(kind="agent", scope_id="code"), "s1") as watch:
        _write_row(store, _session("s2"), "save_run")
        _write_row(store, _session(owner="other"), "update_metadata")

    assert not watch.created


@pytest.mark.asyncio
async def test_a_terminal_save_in_the_lane_reports_the_row_it_created(store: ConversationStore) -> None:
    """A run's terminal save runs on the database's lane, and still reports to the block that awaited it."""
    session = _session()

    with watch_session_row(HistoryScope(kind="agent", scope_id="code"), "s1") as watch:
        await persist_to(store, session)(_run("r1"))

    assert watch.created


def test_clearing_metadata_stores_it_as_json_null(store: ConversationStore) -> None:
    """A metadata update that removes the whole mapping leaves the text a row created without it holds."""
    session = ConversationSession(session_id="s1", kind="agent", owner_id="code")
    store.update_metadata(session, lambda _metadata: {"k": "v"})
    assert _query(store, "SELECT metadata FROM code_sessions") == [('{"k": "v"}',)]

    store.update_metadata(session, lambda _metadata: None)

    assert _query(store, "SELECT metadata, typeof(metadata) FROM code_sessions") == [("null", "text")]
    assert _loaded(store).metadata is None


def test_unreadable_session_columns_load_as_empty_and_usage_leaves_their_bytes(store: ConversationStore) -> None:
    """One odd session row never fails a load; a usage save keeps its bytes but moves its update time."""
    seed_session_row(store, _session())
    _execute(store, "UPDATE code_sessions SET session_data = 'not json', metadata = '[1]', updated_at = 5")

    session = _loaded(store)
    assert (session.session_state, session.metadata) == ({}, None)

    _persist(store, _session(), _run("r1", metrics=_usage(3)))
    [(session_data, updated_at)] = _query(store, "SELECT session_data, updated_at FROM code_sessions")
    assert (session_data, updated_at > 5) == ("not json", True)
    assert _usage_row(store, "r1")["metrics"]["input_tokens"] == 3


@pytest.mark.parametrize(
    "totals",
    [{"input_tokens": "12"}, {"input_tokens": 12, "details": {"model": ["not an object"]}}],
)
def test_a_run_saves_with_its_usage_whatever_totals_an_older_release_left(
    store: ConversationStore,
    totals: dict[str, object],
) -> None:
    """Totals an older release stored that no longer read as counters never stop a run, or its usage, from saving."""
    seed_session_row(store, _session())
    _execute(store, f"UPDATE code_sessions SET session_data = '{json.dumps({'session_metrics': totals})}'")  # noqa: S608

    _persist(store, _session(), _run("r1", metrics=_usage(3)))

    assert _row(store, "r1") is not None
    assert _usage_row(store, "r1")["metrics"]["input_tokens"] == 3


# --- usage ------------------------------------------------------------------------------------------------------


def test_the_usage_snapshot_takes_its_timestamps_from_the_written_row(
    store: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot's creation time is the row's first one and its update time the write's, not the record's."""
    monkeypatch.setattr(store_module, "time", lambda: 1_700_000_500.0)
    _save(store, _run("r1", RunStatus.paused), record_usage=True)
    later = _run("r1")
    later.created_at = 5
    monkeypatch.setattr(store_module, "time", lambda: 1_700_000_900.0)

    _save(store, later, record_usage=True)

    row = _row(store, "r1")
    assert row is not None
    assert row[5:] == (1_700_000_000, 1_700_000_900)
    snapshot = _usage_row(store, "r1")
    assert (snapshot["created_at"], snapshot["updated_at"]) == (1_700_000_000, 1_700_000_900)


def test_a_run_saved_again_counts_once_in_the_session_totals(store: ConversationStore) -> None:
    """A run saved when it paused, again when it finished, and then linked to its reply counts its final usage once."""
    _persist(
        store,
        _session(),
        _run("r1", RunStatus.paused, metrics=_usage(10)),
        _run("r2", metrics=_usage(100, model="other-model")),
        _run("r1", metrics=_usage(25)),
    )
    store.patch_run_metadata("s1", "r1", {"matrix_response_event_id": "$reply"})

    totals = _totals(store)
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (125, 2, 127)
    assert [
        (entry["id"], entry["input_tokens"])
        for run_id in ("r1", "r2")
        for entry in _usage_row(store, run_id)["metrics"]["details"]["model"]
    ] == [("code-model", 25), ("other-model", 100)]


def test_team_member_runs_count_in_their_team_sessions_totals(team_store: ConversationStore) -> None:
    """Each member row and the team row add their own usage once."""
    _persist(
        team_store,
        _team(),
        _member("m1", "t1", metrics=_usage(7, model="helper-model")),
        _team_run("t1", metrics=_usage(30)),
    )

    assert _totals(team_store)["input_tokens"] == 37
    assert _usage_row(team_store, "m1")["parent_run_id"] == "t1"


def test_a_usage_write_failure_rolls_back_the_run(store: ConversationStore) -> None:
    """A run and its usage snapshot land together or not at all."""
    _save(store, _run("r0"))
    _execute(
        store,
        "CREATE TRIGGER fail_usage BEFORE INSERT ON code_sessions_usage BEGIN SELECT RAISE(ABORT, 'usage refused'); END",
    )

    with pytest.raises(sqlite3.IntegrityError, match="usage refused"):
        _persist(store, _session(), _run("r1"))

    assert _row(store, "r1") is None
    assert _message_rows(store, "r1") == []


def test_news_in_the_log_moves_the_sessions_update_time_and_rewrites_do_not(store: ConversationStore) -> None:
    """Update-time cursors see a finished run new to the log, one that finished after waiting, and usage; no rewrite."""

    def updated_at() -> int:
        [(value,)] = _query(store, "SELECT updated_at FROM code_sessions")
        _execute(store, "UPDATE code_sessions SET updated_at = 5")
        return value

    _save(store, _run("r1", RunStatus.paused))
    _execute(store, "UPDATE code_sessions SET updated_at = 5")
    _save(store, _run("r1", RunStatus.paused))
    store.patch_run_metadata("s1", "r1", {"matrix_response_event_id": "$reply"})
    assert updated_at() == 5

    _save(store, _run("r1"))
    assert updated_at() > 5
    _save(store, _run("r2", RunStatus.cancelled))
    assert updated_at() == 5
    store.save_interrupted_record(_session(), _run("r2", content="what was heard"))
    assert updated_at() > 5
    _save(store, _run("r3"))
    assert updated_at() > 5

    store.save_interrupted_record(_session(), _run("r1", content="what was heard"))
    delete_runs(store, ["r2"], session_id="s1")
    assert updated_at() == 5

    _persist(store, _session(), _run("r4"))
    assert updated_at() > 5
    # A paused run takes no place in the log yet; its recorded usage still moves the update time.
    _persist(store, _session(), _run("r5", RunStatus.paused, metrics=_usage(1)))
    assert updated_at() > 5


def test_independent_usage_is_recorded_beside_the_runs(store: ConversationStore) -> None:
    """Helper usage keeps its kind and requester in the ledger, beside the runs."""
    created_at = 1_700_000_500
    tokens = TokenCounts(input_tokens=40, output_tokens=4, total_tokens=44)
    metrics = {"input_tokens": 40, "output_tokens": 4, "total_tokens": 44}

    store.save_independent_usage(
        session_id="s1",
        usage_id="compaction:1",
        kind="compaction_summary",
        requester_id="@bob:example.test",
        usage=HelperUsage(
            run_id="1",
            model="summary-model",
            provider="P",
            metrics=RunMetrics(tokens=tokens),
            requests=(RequestUsage(created_at=created_at, metrics=MessageMetrics(tokens=tokens)),),
            created_at=created_at,
        ),
        initial_session=_session(),
    )

    assert _usage_row(store, "compaction:1") == {
        "run_id": "compaction:1",
        "parent_run_id": None,
        "team_id": None,
        "user_id": "@bob:example.test",
        "model_provider": "P",
        "model": "summary-model",
        "created_at": created_at,
        "metrics": metrics,
        "requests": [{"created_at": created_at, "metrics": metrics}],
        "kind": "compaction_summary",
    }
    assert _query(store, "SELECT session_data FROM code_sessions") == [(None,)]
    assert _loaded(store).runs == []


def test_independent_usage_needs_its_conversation_row(store: ConversationStore) -> None:
    """Without an initial session, helper usage needs a conversation row to belong to."""
    with pytest.raises(sqlite3.IntegrityError):
        store.save_independent_usage(
            session_id="s1",
            usage_id="routing:1",
            kind="routing",
            requester_id=None,
            usage=HelperUsage(run_id="1", model=None, provider=None, metrics=RunMetrics()),
        )


def test_list_updated_sessions_returns_the_sessions_updated_after_the_cursor(store: ConversationStore) -> None:
    """The database's sessions are listed oldest update first, strictly after the cursor."""
    for session_id, updated_at in (("a", 10), ("b", 30), ("c", 20)):
        seed_session_row(store, _session(session_id))
        _execute(store, "UPDATE code_sessions SET updated_at = ? WHERE session_id = ?", updated_at, session_id)

    assert store.list_updated_sessions(since=10) == [("c", 20), ("b", 30)]
