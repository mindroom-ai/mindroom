"""The conversation log's rules, session fields, usage, and concurrency guarantees of the owned store."""

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

from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts
from mindroom.session_storage import store as store_module
from mindroom.session_storage.schema import TableNames, table_names
from mindroom.session_storage.store import (
    ConversationStore,
    migrate_database,
    persist_to,
    runs_without,
)
from mindroom.session_storage.types import ConversationSession, session_state_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import SessionKind

_NAMES = TableNames("code")


def _store(tmp_path: Path, *, prompt_roles: frozenset[str] = frozenset({"system"})) -> ConversationStore:
    return ConversationStore(tmp_path / "code.db", _NAMES, prompt_roles=prompt_roles)


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    """One ordinary conversation store that leaves system messages out of stored runs."""
    return _store(tmp_path)


def _session(session_id: str = "s1", *, kind: SessionKind = "agent", owner: str = "code") -> ConversationSession:
    return ConversationSession(
        session_id=session_id,
        kind=kind,
        owner_id=owner,
        created_at=1_700_000_000,
    )


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
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        session_id="s1",
        parent_run_id=parent,
        status=status,
        content=content,
        messages=[
            Message(role="system", content="prompt", id=f"{run_id}-system", created_at=1),
            Message(role="user", content=run_id, id=f"{run_id}-user", created_at=1),
        ],
        metrics=metrics,
        created_at=1_700_000_000,
    )


def _team_run(run_id: str, status: RunStatus = RunStatus.completed, **fields: Any) -> TeamRunRecord:  # noqa: ANN401
    return TeamRunRecord(
        run_id=run_id,
        team_id="eng",
        session_id="s1",
        status=status,
        created_at=1_700_000_000,
        **fields,
    )


def _seed(store: ConversationStore, runs: list[AnyRunRecord], session: ConversationSession | None = None) -> None:
    for run in runs:
        store.save_run(session or _session(), run, record_usage=True)


def _query(store: ConversationStore, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(store.path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _execute(store: ConversationStore, sql: str, *parameters: object) -> None:
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(sql, parameters)


def _run_rows(store: ConversationStore) -> list[tuple[str, int]]:
    return [
        (run_id, run_index)
        for run_id, run_index in _query(store, "SELECT run_id, run_index FROM code_sessions_runs ORDER BY run_index")
    ]


def _stored(store: ConversationStore, run_id: str) -> tuple[int, str, dict[str, Any]]:
    [(run_index, status, run_data)] = _query(
        store,
        "SELECT run_index, status, run_data FROM code_sessions_runs WHERE run_id = ?",
        run_id,
    )
    return run_index, status, json.loads(run_data)


def _has_row(store: ConversationStore, run_id: str) -> bool:
    return bool(_query(store, "SELECT 1 FROM code_sessions_runs WHERE run_id = ?", run_id))


def _usage_row(store: ConversationStore, run_id: str) -> dict[str, Any]:
    [(usage_data,)] = _query(store, "SELECT usage_data FROM code_sessions_usage WHERE run_id = ?", run_id)
    return json.loads(usage_data)


def _has_usage(store: ConversationStore, run_id: str) -> bool:
    return bool(_query(store, "SELECT 1 FROM code_sessions_usage WHERE run_id = ?", run_id))


def _totals(store: ConversationStore, session_id: str = "s1") -> dict[str, Any]:
    [(session_data,)] = _query(store, "SELECT session_data FROM code_sessions WHERE session_id = ?", session_id)
    return json.loads(session_data).get("session_metrics", {})


def _summary_after(
    store: ConversationStore,
    run_id: str,
    scope_key: str = "agent:code",
    summary: str = "summary",
) -> None:
    """Position one summary row of ``scope_key`` right after ``run_id``, as compaction would."""
    _execute(
        store,
        "INSERT INTO code_sessions_summaries (session_id, scope_key, after_run_index, summary, created_at) "
        "SELECT 's1', ?, run_index, ?, 1700000100 FROM code_sessions_runs WHERE run_id = ?",
        scope_key,
        summary,
        run_id,
    )


def _remove(store: ConversationStore, run_id: str) -> None:
    _execute(store, "INSERT INTO code_sessions_removed_runs (run_id, session_id) VALUES (?, 's1')", run_id)


def _loaded(store: ConversationStore, kind: SessionKind = "agent") -> ConversationSession:
    session = store.load_session("s1", kind)
    assert session is not None
    return session


def _loaded_ids(store: ConversationStore, kind: SessionKind = "agent") -> list[str]:
    return [run.run_id for run in _loaded(store, kind).runs]


# --- the run log ------------------------------------------------------------------------------------------------


def test_saved_runs_load_in_write_order_without_prompt_messages(store: ConversationStore) -> None:
    """Runs are rows indexed in write order; prompt-role messages never reach the row, and the caller's run is untouched."""
    runs = [_run("r1"), _run("r2"), _run("r3")]

    _seed(store, runs)

    assert _run_rows(store) == [("r1", 0), ("r2", 1), ("r3", 2)]
    assert [(run.run_id, [message.role for message in run.messages]) for run in _loaded(store).runs] == [
        ("r1", ["user"]),
        ("r2", ["user"]),
        ("r3", ["user"]),
    ]
    assert [message.role for message in runs[0].messages] == ["system", "user"]


def test_a_paused_run_keeps_its_prompt_messages_until_it_finishes(store: ConversationStore) -> None:
    """A paused run keeps its system message for its continuation; the finished run drops it."""
    _seed(store, [_run("r1", RunStatus.paused)])
    assert [message.role for message in _loaded(store).runs[0].messages] == ["system", "user"]

    _seed(store, [_run("r1", RunStatus.completed)])

    assert [message.role for message in _loaded(store).runs[0].messages] == ["user"]


def test_loaded_runs_drop_their_stored_tool_schemas(store: ConversationStore) -> None:
    """Older releases stored every tool schema in run metadata, including in embedded member runs."""
    member = {"run_id": "m1", "agent_id": "code", "messages": [], "metadata": {"tools_schema": [{}], "room_id": "!r"}}
    team = {
        "run_id": "t1",
        "team_id": "eng",
        "status": "PAUSED",
        "messages": [],
        "metadata": {"tools_schema": [{"name": "x"}], "room_id": "!r"},
        "member_responses": [member],
    }
    store.ensure_session_row(_session(kind="team", owner="eng"))
    _execute(
        store,
        "INSERT INTO code_sessions_runs (run_id, session_id, run_type, team_id, status, run_index, run_data, created_at) "
        "VALUES ('t1', 's1', 'team', 'eng', 'PAUSED', 0, ?, 1)",
        json.dumps(team),
    )

    [loaded] = _loaded(store, "team").runs

    assert isinstance(loaded, TeamRunRecord)
    assert loaded.metadata == {"room_id": "!r"}
    assert loaded.member_responses[0].metadata == {"room_id": "!r"}
    stored_get = store.get_run("t1")
    assert stored_get is not None
    assert stored_get.metadata == {"room_id": "!r"}


def test_runs_appended_after_deleting_leading_runs_sort_after_the_survivors(store: ConversationStore) -> None:
    """A new run never reuses a position below a surviving run."""
    _seed(store, [_run("r0"), _run("r1"), _run("r2")])
    store.delete_run_subtrees(["r0", "r1"])

    _seed(store, [_run("r3")])

    assert _run_rows(store) == [("r2", 2), ("r3", 3)]
    assert _loaded_ids(store) == ["r2", "r3"]


def test_new_runs_sort_after_every_summary_row(store: ConversationStore) -> None:
    """Deleting the newest runs must not let a new run reuse an index a summary row sorts after."""
    _seed(store, [_run(f"r{index}") for index in range(3)])
    _summary_after(store, "r2")
    store.delete_run_subtrees(["r1", "r2"])

    _seed(store, [_run("r3")])

    assert _stored(store, "r3")[0] == 3
    assert _loaded_ids(store) == ["r3"]


@pytest.mark.parametrize(
    "stored_status",
    [RunStatus.paused, RunStatus.cancelled, RunStatus.error],
    ids=["approval", "cli-checkpoint", "interrupted-record"],
)
def test_a_non_visible_run_saved_visible_moves_to_the_end(store: ConversationStore, stored_status: RunStatus) -> None:
    """A continuation finishing after newer turns and a compaction replays after the summary."""
    _seed(store, [_run("r0", stored_status), *(_run(f"r{index}") for index in range(1, 4))])
    _summary_after(store, "r3")

    _seed(store, [_run("r0", content="approved")])

    session = _loaded(store)
    assert _stored(store, "r0")[0] == 4
    assert [run.run_id for run in session.runs] == ["r0"]
    assert session.summary is not None
    assert session.summary.text == "summary"


def test_a_continuation_without_a_summary_replays_after_the_turns_it_waited_for(store: ConversationStore) -> None:
    """The move holds without compaction too: the resumed run sorts after the turns its continuation saw."""
    _seed(store, [_run("r0", RunStatus.paused), _run("r1")])

    _seed(store, [_run("r0")])

    assert _loaded_ids(store) == ["r1", "r0"]


def test_a_save_keeping_visibility_never_moves_a_run(store: ConversationStore) -> None:
    """Re-saving a visible run, or a paused run that stays paused, keeps its place."""
    _seed(store, [_run("r0", RunStatus.paused), _run("r1"), _run("r2")])

    _seed(store, [_run("r1", content="linked"), _run("r0", RunStatus.paused, content="still waiting")])

    assert _stored(store, "r0")[0] == 0
    assert _stored(store, "r1")[0] == 1


def test_a_folded_run_is_frozen(store: ConversationStore) -> None:
    """A stale copy of a run behind the latest summary row changes neither its row, its usage, nor the totals."""
    _seed(store, [_run("p0", RunStatus.paused), _run("r1", content="original", metrics=_usage(10)), _run("r2")])
    _summary_after(store, "r2")
    before, usage_before, totals_before = _stored(store, "r1"), _usage_row(store, "r1"), _totals(store)

    outcomes = [
        store.save_run(_session(), _run("r1", RunStatus.paused, content="stale paused"), record_usage=True),
        store.save_run(_session(), _run("r1", content="stale", metrics=_usage(99)), record_usage=True),
        store.save_run(_session(), _run("p0", RunStatus.paused, content="still waiting"), record_usage=True),
    ]

    assert outcomes == ["frozen", "frozen", "written"]
    assert _stored(store, "r1") == before
    assert _usage_row(store, "r1") == usage_before
    assert _totals(store) == totals_before
    assert _stored(store, "p0")[2]["content"] == "still waiting"
    assert _loaded_ids(store) == ["p0"]


def test_a_member_of_a_folded_team_run_is_frozen(store: ConversationStore) -> None:
    """A folded team run's member rows, stored or seen for the first time, are as final as the team run."""
    team = _session(kind="team", owner="eng")
    _seed(store, [_run("m0", parent="t0", agent_id="helper", content="original"), _team_run("t0")], team)
    _summary_after(store, "t0", "team:eng")

    outcomes = [
        store.save_run(team, _run("m0", parent="t0", agent_id="helper", content="stale"), record_usage=True),
        store.save_run(team, _run("m-new", parent="t0", agent_id="helper"), record_usage=True),
    ]

    assert outcomes == ["frozen", "frozen"]
    assert _stored(store, "m0")[2]["content"] == "original"
    assert not _has_row(store, "m-new")


def test_a_removed_run_is_never_written_again_but_its_usage_counts(store: ConversationStore) -> None:
    """A run a cut deleted, and a member row under it, stay gone; the provider calls behind a late save still count."""
    _seed(store, [_run("r0", metrics=_usage(10))])
    _remove(store, "gone")

    outcomes = [
        store.save_run(_session(), _run("gone", metrics=_usage(5)), record_usage=True),
        store.save_run(_session(), _run("gone-member", parent="gone", agent_id="helper"), record_usage=False),
    ]

    assert outcomes == ["removed", "removed"]
    assert _loaded_ids(store) == ["r0"]
    assert not _has_row(store, "gone")
    assert not _has_row(store, "gone-member")
    assert _usage_row(store, "gone")["metrics"]["input_tokens"] == 5
    assert _totals(store)["input_tokens"] == 15


def test_save_runs_swaps_in_only_the_runs_it_wrote(store: ConversationStore) -> None:
    """A folded copy stays out of the session and a removed run leaves it, as stored."""
    _seed(store, [_run(f"r{index}") for index in range(3)])
    _summary_after(store, "r0")
    _remove(store, "r2")
    _execute(store, "DELETE FROM code_sessions_runs WHERE run_id = 'r2'")
    session = _loaded(store)
    stale, written, removed = (_run(run_id, content=run_id) for run_id in ("r0", "r1", "r2"))
    session.runs = [*session.runs, copy.deepcopy(removed)]

    store.save_runs(session, [stale, written, removed, _run("r3", content="new")])

    assert [(run.run_id, run.content) for run in session.runs] == [("r1", "r1"), ("r3", "new")]
    assert _stored(store, "r0")[2].get("content") is None
    assert _stored(store, "r3")[0] == 2
    assert not _has_usage(store, "r3")


def test_save_runs_refuses_a_run_object_loaded_from_the_session(store: ConversationStore) -> None:
    """Only a copy may be edited and written back, so a refused save leaves the session's copy as stored."""
    _seed(store, [_run("r1")])
    session = _loaded(store)
    session.runs[0].content = "edited in place"

    with pytest.raises(ValueError, match="edit a copy"):
        store.save_runs(session, [session.runs[0]])
    assert _stored(store, "r1")[2].get("content") is None


def test_a_failed_rewrite_writes_nothing_and_leaves_the_session_as_loaded(
    store: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite is one transaction; when it fails, neither the rows nor ``session.runs`` claim the change."""
    _seed(store, [_run("r1"), _run("r2")])
    session = _loaded(store)
    loaded = list(session.runs)
    original_write = store_module._write_run_row

    def fail_on_second(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        if args[2]["run_id"] == "r2":
            msg = "disk full"
            raise sqlite3.OperationalError(msg)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(store_module, "_write_run_row", fail_on_second)

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        store.save_runs(session, [_run("r1", content="new"), _run("r2", content="new")])

    assert session.runs == loaded
    assert _stored(store, "r1")[2].get("content") is None


def test_a_usage_write_failure_rolls_back_the_run(store: ConversationStore) -> None:
    """A run and its usage snapshot land together or not at all."""
    _seed(store, [_run("r0")])
    _execute(
        store,
        "CREATE TRIGGER fail_usage BEFORE INSERT ON code_sessions_usage BEGIN SELECT RAISE(ABORT, 'usage refused'); END",
    )

    with pytest.raises(sqlite3.IntegrityError, match="usage refused"):
        store.save_run(_session(), _run("r1"), record_usage=True)

    assert not _has_row(store, "r1")


def test_deleting_a_team_run_takes_its_member_rows_along(store: ConversationStore) -> None:
    """Member runs are rows below the team run; deleting it, within its session, deletes them too."""
    team = _session(kind="team", owner="eng")
    other = _session("s2", kind="team", owner="eng")
    _seed(
        store,
        [
            _team_run("team1"),
            _run("m1", parent="team1"),
            _run("m1-child", parent="m1"),
            _team_run("team2"),
        ],
        team,
    )
    _seed(store, [_run("elsewhere", parent="team1")], other)

    assert store.delete_run_subtrees(["team1"], session_id="s1") == {"team1", "m1", "m1-child"}
    assert _loaded_ids(store, "team") == ["team2"]
    assert _has_row(store, "elsewhere")
    assert store.delete_run_subtrees(["team2", ""]) == {"team2"}
    assert _loaded(store, "team").runs == []


def test_a_paused_team_run_behind_the_summary_keeps_writable_members_and_moves_on_completion(
    store: ConversationStore,
) -> None:
    """Only visible folded runs freeze; a paused team approval's members still save and the team run then moves."""
    team = _session(kind="team", owner="eng")
    _seed(
        store,
        [
            _run("m0", parent="t0", agent_id="helper", content="before"),
            _team_run("t0", RunStatus.paused),
            _team_run("t1"),
        ],
        team,
    )
    _summary_after(store, "t1", "team:eng")

    _seed(store, [_run("m0", parent="t0", agent_id="helper", content="after"), _team_run("t0")], team)

    assert _stored(store, "m0")[2]["content"] == "after"
    assert _stored(store, "t0")[0] == 3
    assert _loaded_ids(store, "team") == ["m0", "t0"]


# --- the loaded session -----------------------------------------------------------------------------------------


def _as_stored(session: ConversationSession) -> list[dict[str, Any]]:
    return [run.to_dict() for run in session.runs]


@pytest.mark.asyncio
async def test_a_persisted_run_joins_the_session_as_its_row_holds_it(store: ConversationStore) -> None:
    """The session's copy is decoded from the row, never the record handed over, so prompt messages stay out of it."""
    _seed(store, [_run("r1")])
    session = _loaded(store)
    record = _run("r2")

    await persist_to(store, session)(record)

    assert _as_stored(session) == _as_stored(_loaded(store))
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
    """A paused run saved visible goes after the turns it waited for; a run whose row keeps its place keeps it."""
    _seed(store, [_run("r0", RunStatus.paused), _run("r1")])
    session = _loaded(store)

    await persist_to(store, session)(_run("r0", saved_status, content="resumed"))

    assert [run.run_id for run in session.runs] == expected
    assert _as_stored(session) == _as_stored(_loaded(store))


@pytest.mark.asyncio
async def test_a_resumed_team_run_keeps_its_member_runs_in_the_session(store: ConversationStore) -> None:
    """A member keeps its place and its copy when its team run's save moves the team run after a later turn."""
    team = _session(kind="team", owner="eng")
    _seed(
        store,
        [
            _run("m0", RunStatus.paused, parent="t0", agent_id="helper"),
            _team_run("t0", RunStatus.paused),
            _team_run("t1"),
        ],
        team,
    )
    session = _loaded(store, "team")
    persist = persist_to(store, session)

    await persist(_run("m0", parent="t0", agent_id="helper", content="checked"))
    await persist(_team_run("t0", content="answered"))

    assert [run.run_id for run in session.runs] == ["m0", "t1", "t0"]
    assert _as_stored(session) == _as_stored(_loaded(store, "team"))


@pytest.mark.asyncio
async def test_a_removed_save_takes_the_run_and_its_members_out_of_the_session(store: ConversationStore) -> None:
    """A run a cut removed while it waited leaves the session with its member runs, as it left the log."""
    team = _session(kind="team", owner="eng")
    _seed(
        store,
        [
            _run("m0", RunStatus.paused, parent="t0", agent_id="helper"),
            _team_run("t0", RunStatus.paused),
            _team_run("t1"),
        ],
        team,
    )
    session = _loaded(store, "team")
    store.delete_run_subtrees(["t0"])
    _remove(store, "t0")

    await persist_to(store, session)(_team_run("t0", RunStatus.cancelled))

    assert [run.run_id for run in session.runs] == ["t1"]
    assert _as_stored(session) == _as_stored(_loaded(store, "team"))


@pytest.mark.asyncio
async def test_a_frozen_save_leaves_the_session_s_copy_as_stored(store: ConversationStore) -> None:
    """A run folded behind a summary since the session loaded refuses the save; the session keeps the stored copy."""
    _seed(store, [_run("r1", content="original")])
    session = _loaded(store)
    _summary_after(store, "r1")

    await persist_to(store, session)(_run("r1", content="stale"))

    [kept] = session.runs
    stored = store.get_run("r1")
    assert stored is not None
    assert kept.to_dict() == stored.to_dict()
    assert kept.content == "original"


@pytest.mark.asyncio
async def test_a_cancelled_persist_still_leaves_the_session_as_stored(
    store: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cancelled while its save runs finds the session holding what the save wrote."""
    _seed(store, [_run("r1")])
    session = _loaded(store)
    writing, release = threading.Event(), threading.Event()
    original_write = store_module._write_run_row

    def held_write(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        writing.set()
        release.wait(10)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(store_module, "_write_run_row", held_write)
    save = asyncio.create_task(persist_to(store, session)(_run("r2")))
    assert await asyncio.to_thread(writing.wait, 10)
    save.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await save
    assert [run.run_id for run in session.runs] == ["r1", "r2"]
    assert _as_stored(session) == _as_stored(_loaded(store))


def test_a_rewrite_that_makes_a_run_visible_moves_the_session_s_copy_last(store: ConversationStore) -> None:
    """A conversation rewrite finishing a cancelled run moves it after later turns, in the session as in the log."""
    _seed(store, [_run("r0", RunStatus.cancelled), _run("r1")])
    session = _loaded(store)

    store.save_runs(session, [_run("r0", content="interrupted reply")])

    assert [run.run_id for run in session.runs] == ["r1", "r0"]
    assert _as_stored(session) == _as_stored(_loaded(store))


def test_a_delete_takes_the_runs_and_their_members_out_of_the_session(store: ConversationStore) -> None:
    """A delete handed the loaded session takes the deleted runs out of it with their member runs, as out of the log."""
    team = _session(kind="team", owner="eng")
    _seed(store, [_run("m0", parent="t0", agent_id="helper"), _team_run("t0"), _team_run("t1")], team)
    session = _loaded(store, "team")

    assert store.delete_run_subtrees(["t0"], session_id="s1", session=session) == {"t0", "m0"}

    assert [run.run_id for run in session.runs] == ["t1"]
    assert _as_stored(session) == _as_stored(_loaded(store, "team"))


def test_runs_without_removes_every_descendant() -> None:
    """Grandchildren leave with their root even when stored before it."""
    runs: list[AnyRunRecord] = [
        RunRecord(run_id="unrelated"),
        RunRecord(run_id="child", parent_run_id="root"),
        RunRecord(run_id="grandchild", parent_run_id="child"),
        RunRecord(run_id="root"),
    ]

    assert [run.run_id for run in runs_without(runs, ["root"])] == ["unrelated"]


# --- loads ------------------------------------------------------------------------------------------------------


def test_a_store_without_tables_reads_as_empty(tmp_path: Path) -> None:
    """A database no one wrote yet reads as empty instead of failing."""
    store = _store(tmp_path)

    assert store.load_session("s1", "agent") is None
    assert not store.has_session("s1", "agent")
    assert store.list_runs("s1") == []
    assert store.get_run("r1") is None
    assert store.list_updated_sessions("agent", "code", since=0) == []


def test_load_returns_none_for_a_missing_session_or_one_of_another_kind(store: ConversationStore) -> None:
    """An agent load never returns a team session, nor a team load an agent one; ``has_session`` agrees."""
    _seed(store, [_team_run("t1")], _session(kind="team", owner="eng"))

    assert store.load_session("s1", "agent") is None
    assert store.load_session("missing", "team") is None
    assert _loaded_ids(store, "team") == ["t1"]
    assert not store.has_session("s1", "agent")
    assert not store.has_session("missing", "team")
    assert store.has_session("s1", "team")


def test_runs_of_another_type_never_load(store: ConversationStore) -> None:
    """Agno's workflow runs are rows no conversation reads."""
    _seed(store, [_run("r1")])
    _execute(
        store,
        "INSERT INTO code_sessions_runs (run_id, session_id, run_type, status, run_index, run_data, created_at) "
        "VALUES ('w1', 's1', 'workflow', 'COMPLETED', 1, '{\"run_id\": \"w1\"}', 1)",
    )

    assert _loaded_ids(store) == ["r1"]
    assert [run.run_id for run in store.list_runs("s1")] == ["r1"]
    assert store.get_run("w1") is None


def test_load_follows_member_rows_to_any_depth_and_drops_orphans(store: ConversationStore) -> None:
    """A paused team run behind the summary loads with its members' members; a member without a stored parent does not."""
    team = _session(kind="team", owner="eng")
    _seed(
        store,
        [
            _run("m0", parent="t0", agent_id="helper"),
            _run("m0-child", parent="m0", agent_id="helper"),
            _team_run("t0", RunStatus.paused),
            _run("orphan", parent="missing", agent_id="helper"),
            _team_run("t1"),
        ],
        team,
    )
    _summary_after(store, "t1", "team:eng")

    assert _loaded_ids(store, "team") == ["m0", "m0-child", "t0"]


def test_cancelled_and_errored_runs_behind_the_summary_still_load(store: ConversationStore) -> None:
    """Every non-visible run loads wherever it sits."""
    _seed(store, [_run("c0", RunStatus.cancelled), _run("e1", RunStatus.error), _run("r2"), _run("r3")])
    _summary_after(store, "r2")

    assert _loaded_ids(store) == ["c0", "e1", "r3"]


def test_load_takes_the_summary_from_the_owner_scopes_latest_row(store: ConversationStore) -> None:
    """The replayed summary is the owner scope's latest row; another scope's rows and older rows do not count."""
    _seed(store, [_run("r0"), _run("r1")])
    _summary_after(store, "r0", summary="first")
    _summary_after(store, "r0", "agent:other", summary="another scope")
    first = _loaded(store).summary
    assert first is not None
    assert first.text == "first"

    _summary_after(store, "r1", summary=None)

    session = _loaded(store)
    assert session.summary is None
    assert [run.run_id for run in session.runs] == []


def test_a_run_folded_mid_turn_loads_without_the_messages_its_segment_holds(store: ConversationStore) -> None:
    """The segment holds those messages; a by-id read still returns the row as stored."""
    _seed(store, [_run("r0")])
    segment = {"messages": [{"id": "r0-user", "role": "user", "content": "r0", "created_at": 1}]}
    _execute(
        store,
        "INSERT INTO code_sessions_summaries (session_id, scope_key, after_run_index, summary, created_at, segment_run_id, "
        "segment) VALUES ('s1', 'agent:code', -1, 'folded', 1, 'r0', ?)",
        json.dumps(segment),
    )

    [loaded] = _loaded(store).runs

    assert loaded.messages == []
    stored = store.get_run("r0")
    assert stored is not None
    assert [message.id for message in stored.messages] == ["r0-user"]


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Callable[[str], None]]]:
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
    """A commit cannot land between the reads of one load, so runs and summary always agree."""
    _seed(store, [_run("r0"), _run("r1")])
    errors: list[str] = []
    selects = 0

    def write_between_reads(statement: str) -> None:
        nonlocal selects
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            selects += 1
            if selects == 3:
                _write_now(
                    store,
                    "INSERT INTO code_sessions_summaries (session_id, scope_key, after_run_index, summary, created_at) "
                    "VALUES ('s1', 'agent:code', 0, 'raced', 1700000000)",
                    errors,
                )

    traced.append(write_between_reads)
    session = _loaded(store)

    assert errors == ["database is locked"]
    assert [run.run_id for run in session.runs] == ["r0", "r1"]


def test_a_concurrent_writer_cannot_land_between_the_index_read_and_the_insert(
    store: ConversationStore,
    traced: list[Callable[[str], None]],
) -> None:
    """A run save holds the write lock from its rule reads through its insert, so two saves never share an index."""
    _seed(store, [_run("r0")])
    errors: list[str] = []

    def write_during_index_read(statement: str) -> None:
        if "SELECT MAX(COALESCE" in statement:
            _write_now(
                store,
                "INSERT INTO code_sessions_runs (run_id, session_id, run_type, agent_id, status, run_index, run_data, "
                "created_at) VALUES ('raced', 's1', 'agent', 'code', 'COMPLETED', 1, '{}', 1700000000)",
                errors,
            )

    traced.append(write_during_index_read)
    _seed(store, [_run("r1")])

    assert errors == ["database is locked"]
    assert _run_rows(store) == [("r0", 0), ("r1", 1)]


def test_first_writes_to_a_fresh_database_from_many_owners_all_land(tmp_path: Path) -> None:
    """Owners opening one new database at once each create its tables or find them; none fails."""
    start = threading.Barrier(8)

    def first_write(index: int) -> str:
        store = _store(tmp_path)
        start.wait()
        return store.save_run(_session(f"s{index}"), _run(f"r{index}"), record_usage=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(first_write, range(8)))

    assert outcomes == ["written"] * 8
    assert sorted(_run_rows(_store(tmp_path))) == sorted((f"r{index}", 0) for index in range(8))


def test_load_finds_member_rows_without_scanning_the_session_per_run(
    store: ConversationStore,
    traced: list[Callable[[str], None]],
) -> None:
    """The descendant step searches a materialized child set, so a load stays linear in the session's rows."""
    _seed(store, [_run("r0"), _run("r1")])
    statements: list[str] = []
    traced.append(lambda statement: statements.append(statement) if "WITH RECURSIVE" in statement else None)

    _loaded(store)

    [statement] = statements
    plan = [row[3] for row in _query(store, f"EXPLAIN QUERY PLAN {statement}")]
    step = plan[plan.index("RECURSIVE STEP") + 1 :]
    # The runs table is read once, to materialize the child set; each step only probes that set's index.
    assert "SEARCH children USING AUTOMATIC COVERING INDEX (parent_run_id=?)" in step


# --- migrations -------------------------------------------------------------------------------------------------


def _database_with_values(tmp_path: Path) -> Path:
    path = tmp_path / "migrating.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE legacy (value INTEGER)")
        connection.executemany("INSERT INTO legacy VALUES (?)", [(1,), (2,)])
    return path


def _not_yet_copied(connection: sqlite3.Connection) -> bool:
    return "copied" not in table_names(connection)


def test_a_failed_migration_changes_nothing_and_releases_the_write_lock_at_once(tmp_path: Path) -> None:
    """A failure while a cursor is still reading rolls back by itself, not when the dropped cursor lets the close finish."""
    path = _database_with_values(tmp_path)

    def copy_then_fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE copied (value INTEGER)")
        reading = connection.execute("SELECT value FROM legacy")
        (value,) = reading.fetchone()
        connection.execute("INSERT INTO copied VALUES (?)", (value,))
        msg = "interrupted copy"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="interrupted copy") as interrupted:
        migrate_database(path, needed=_not_yet_copied, apply=copy_then_fail)

    # The failure's traceback keeps the reading cursor alive, as it does for a caller that logs or re-raises it.
    assert interrupted.traceback
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")
        assert _not_yet_copied(other)
        other.rollback()


def test_a_migration_another_opener_finished_while_this_one_waited_does_not_run_again(tmp_path: Path) -> None:
    """The migration is asked for again once the write lock is held, so racing openers migrate the database once."""
    path = _database_with_values(tmp_path)
    answers = iter([True, False])

    migrate_database(path, needed=lambda _connection: next(answers), apply=lambda _connection: pytest.fail("ran twice"))

    assert next(answers, None) is None


def test_a_migration_that_is_not_due_opens_no_write_transaction(tmp_path: Path) -> None:
    """A migrated database costs one read, so it never waits for another writer's lock."""
    path = _database_with_values(tmp_path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        migrate_database(
            path,
            needed=lambda _connection: False,
            apply=lambda _connection: pytest.fail("not due"),
            timeout=0,
        )
    finally:
        holder.rollback()
        holder.close()


# --- session rows -----------------------------------------------------------------------------------------------


def test_session_field_writes_leave_the_other_field_and_the_totals_alone(store: ConversationStore) -> None:
    """A state save writes only ``session_state``, a metadata update only ``metadata``, and neither moves the totals."""
    _seed(store, [_run("r1", metrics=_usage(10))])
    session = _loaded(store)
    session.session_state = {"mindroom_pending_compaction_scope_keys": ["agent:code"]}

    store.save_session_state(session, session_state_snapshot(session.session_state))
    reloaded = _loaded(store)
    assert (reloaded.metadata, reloaded.session_state) == (None, session.session_state)

    store.update_metadata(session, lambda _metadata: {"mindroom_matrix_history": {"version": 1}})
    reloaded = _loaded(store)
    assert (reloaded.metadata, reloaded.session_state) == (session.metadata, session.session_state)
    assert session.metadata == {"mindroom_matrix_history": {"version": 1}}
    assert _totals(store)["input_tokens"] == 10


@pytest.mark.parametrize(
    "create",
    ["ensure_session_row", "save_run", "save_runs", "save_session_state", "save_independent_usage"],
)
def test_every_writer_of_a_session_row_stores_absent_metadata_as_json_null(
    store: ConversationStore,
    create: str,
) -> None:
    """Agno's JSON column stored no metadata as the text ``null``; no writer stores SQL NULL or an empty object."""
    session = ConversationSession(session_id="s1", kind="agent", owner_id="code")
    writes = {
        "ensure_session_row": lambda: store.ensure_session_row(session),
        "save_run": lambda: store.save_run(session, _run("r1"), record_usage=True),
        "save_runs": lambda: store.save_runs(session, [_run("r1")]),
        "save_session_state": lambda: store.save_session_state(session, {}),
        "save_independent_usage": lambda: store.save_independent_usage(
            session_id="s1",
            usage_id="u1",
            kind="compaction_summary",
            requester_id=None,
            run={"model_provider": "P", "model": "m", "created_at": 1, "metrics": {}, "messages": []},
            initial_session=session,
        ),
    }

    writes[create]()

    assert _query(store, "SELECT metadata, typeof(metadata) FROM code_sessions") == [("null", "text")]


def test_clearing_metadata_stores_it_as_json_null(store: ConversationStore) -> None:
    """A metadata update that removes every key, or the whole mapping, leaves the same text a row created without it holds."""
    session = ConversationSession(session_id="s1", kind="agent", owner_id="code")
    store.update_metadata(session, lambda _metadata: {"k": "v"})
    assert _query(store, "SELECT metadata FROM code_sessions") == [('{"k": "v"}',)]

    store.update_metadata(session, lambda _metadata: None)

    assert _query(store, "SELECT metadata, typeof(metadata) FROM code_sessions") == [("null", "text")]
    assert _loaded(store).metadata is None


def test_ensure_session_row_creates_a_missing_row_from_the_session(store: ConversationStore) -> None:
    """A new conversation's row is created from the session."""
    session = _session()
    session.session_state = {"key": "value"}

    store.ensure_session_row(session)

    loaded = _loaded(store)
    assert (loaded.owner_id, loaded.session_state, loaded.created_at) == (
        "code",
        {"key": "value"},
        1_700_000_000,
    )


def _requested_run(run_id: str, requester_id: str | None) -> RunRecord:
    run = _run(run_id)
    run.user_id = requester_id
    return run


def _row_requester(store: ConversationStore) -> str | None:
    [(user_id,)] = _query(store, "SELECT user_id FROM code_sessions")
    return user_id


@pytest.mark.parametrize("save", ["save_run", "save_runs"])
def test_a_saved_run_gives_a_session_row_created_without_a_requester_the_runs(
    store: ConversationStore,
    save: str,
) -> None:
    """A conversation's row stores its first run's requester, as Agno's session did, whoever created the row."""
    session = _session()
    run = _requested_run("r1", "@alice:example.test")

    if save == "save_run":
        store.save_run(session, run, record_usage=True)
    else:
        store.save_runs(session, [run])

    assert _row_requester(store) == "@alice:example.test"


@pytest.mark.asyncio
async def test_the_terminal_save_of_a_turn_stores_its_requester_on_the_row_a_fold_created(
    store: ConversationStore,
) -> None:
    """The row a first turn's fold or usage write created has no requester; the turn's own save names it."""
    session = _session()
    store.ensure_session_row(session)
    assert _row_requester(store) is None

    await persist_to(store, session)(_requested_run("r1", "@alice:example.test"))

    assert _row_requester(store) == "@alice:example.test"


def test_a_session_row_keeps_the_first_requester_a_run_named(store: ConversationStore) -> None:
    """A later run of another requester, or of none, leaves the row's requester alone."""
    session = _session()

    store.save_run(session, _requested_run("r0", None), record_usage=True)
    assert _row_requester(store) is None
    store.save_run(session, _requested_run("r1", "@alice:example.test"), record_usage=True)
    store.save_run(session, _requested_run("r2", "@bob:example.test"), record_usage=True)
    store.save_run(session, _requested_run("r3", None), record_usage=True)

    assert _row_requester(store) == "@alice:example.test"


def test_a_frozen_or_removed_run_does_not_name_the_requester(store: ConversationStore) -> None:
    """Only a run the store wrote names the row's requester."""
    _seed(store, [_run("r0"), _run("r1")], _session())
    _summary_after(store, "r1")
    _remove(store, "gone")

    outcomes = [
        store.save_run(_session(), _requested_run("r0", "@alice:example.test"), record_usage=True),
        store.save_run(_session(), _requested_run("gone", "@alice:example.test"), record_usage=True),
    ]

    assert outcomes == ["frozen", "removed"]
    assert _row_requester(store) is None


@pytest.mark.asyncio
async def test_persist_to_saves_the_record_with_its_usage_and_the_session_state_only_when_it_changed(
    store: ConversationStore,
) -> None:
    """The run callback appends the record with its usage, and writes ``session_state`` back only after tools changed it."""
    _seed(store, [_run("r1", metrics=_usage(10))])
    session = _loaded(store)
    persist = persist_to(store, session)

    await persist(_run("r2", metrics=_usage(5)))

    assert _loaded_ids(store) == ["r1", "r2"]
    assert _totals(store)["input_tokens"] == 15
    # Another writer's state stays while this session's tools left theirs alone.
    _execute(
        store,
        """UPDATE code_sessions SET session_data = json_set(session_data, '$.session_state', json('{"other": 1}'))""",
    )
    await persist(_run("r3"))
    assert _loaded(store).session_state == {"other": 1}

    session.session_state["mindroom_pending_compaction_scope_keys"] = ["agent:code"]
    await persist(_run("r4"))

    assert _loaded(store).session_state == {"mindroom_pending_compaction_scope_keys": ["agent:code"]}
    assert _loaded_ids(store) == ["r1", "r2", "r3", "r4"]


@pytest.mark.asyncio
async def test_persist_to_writes_back_a_session_state_restored_after_an_earlier_save(store: ConversationStore) -> None:
    """A state changed and saved with one record, then restored before the next, is stored restored.

    One callback saves every member and leader record of a turn, so a member that restores what an earlier member
    changed must not leave the earlier change stored.
    """
    _seed(store, [_run("r1")])
    session = _loaded(store)
    session.session_state = {"cwd": "/repo"}
    store.save_session_state(session, session_state_snapshot(session.session_state))
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
    """A state write that fails takes its record with it, so a paused run never stores without the state it ran with."""
    _seed(store, [_run("r1")])
    session = _loaded(store)
    session.session_state = {"cwd": "/repo"}
    store.save_session_state(session, session_state_snapshot(session.session_state))
    persist = persist_to(store, session)
    _execute(
        store,
        "CREATE TRIGGER refuse_state BEFORE UPDATE OF session_data ON code_sessions "
        "WHEN json_extract(NEW.session_data, '$.session_state.cwd') = '/repo/sub' "
        "BEGIN SELECT RAISE(ABORT, 'state refused'); END",
    )
    session.session_state["cwd"] = "/repo/sub"

    with pytest.raises(sqlite3.IntegrityError, match="state refused"):
        await persist(_run("r2", RunStatus.paused))
    assert _loaded(store).session_state == {"cwd": "/repo"}
    assert _loaded_ids(store) == ["r1"]
    assert not _has_usage(store, "r2")
    assert session.stored_session_state == {"cwd": "/repo"}

    _execute(store, "DROP TRIGGER refuse_state")
    await persist(_run("r2", RunStatus.paused))

    assert _loaded(store).session_state == {"cwd": "/repo/sub"}
    assert _loaded_ids(store) == ["r1", "r2"]


@pytest.mark.parametrize("row_exists", [False, True], ids=["new-row", "stored-row"])
def test_save_runs_with_a_state_and_no_runs_still_writes_the_state(
    store: ConversationStore,
    *,
    row_exists: bool,
) -> None:
    """A state handed to a save is stored whether or not runs came with it, so the call never drops it silently."""
    session = _session()
    if row_exists:
        store.ensure_session_row(session)
    state = {"cwd": "/repo"}

    store.save_runs(session, [], state=state)

    fields = store.read_session_fields("s1")
    assert fields is not None
    assert fields.session_state == state
    assert session.stored_session_state == state
    assert _loaded_ids(store) == []


def test_save_runs_with_no_runs_and_no_state_writes_nothing(store: ConversationStore) -> None:
    """An empty rewrite is a no-op that does not even create the session's row."""
    store.save_runs(_session(), [])

    assert store.read_session_fields("s1") is None


@pytest.mark.asyncio
async def test_persist_to_compares_the_state_with_what_the_session_loaded_or_last_wrote(
    store: ConversationStore,
) -> None:
    """The terminal save compares with the state the store holds for the session: as loaded, or as a write left it.

    A state the turn left alone keeps another writer's, and one a state save stored changed mid-turn and the turn
    then restored is stored restored.
    """
    _seed(store, [_run("r1")])
    seeded = _loaded(store)
    seeded.session_state = {"cwd": "/repo"}
    store.save_session_state(seeded, session_state_snapshot(seeded.session_state))
    session = _loaded(store)
    persist = persist_to(store, session)
    _execute(
        store,
        """UPDATE code_sessions SET session_data = json_set(session_data, '$.session_state', json('{"cwd": "/other"}'))""",
    )

    await persist(_run("r2"))
    assert _loaded(store).session_state == {"cwd": "/other"}

    session.session_state["cwd"] = "/repo/sub"
    store.save_session_state(session, session_state_snapshot(session.session_state))
    session.session_state["cwd"] = "/repo"
    await persist(_run("r3"))

    assert _loaded(store).session_state == {"cwd": "/repo"}


@pytest.mark.asyncio
async def test_the_save_that_creates_the_session_row_records_the_state_it_stored(store: ConversationStore) -> None:
    """A created row holds the session's state, so a terminal save that leaves it alone keeps another writer's."""
    session = _session()
    session.session_state["cwd"] = "/repo"
    persist = persist_to(store, session)

    store.save_run(session, _run("r1"), record_usage=True)

    assert session.stored_session_state == {"cwd": "/repo"}
    assert session.stored_session_state is not session.session_state
    _execute(
        store,
        """UPDATE code_sessions SET session_data = json_set(session_data, '$.session_state', json('{"cwd": "/other"}'))""",
    )
    await persist(_run("r2"))
    assert _loaded(store).session_state == {"cwd": "/other"}


def _row_state(store: ConversationStore, session_id: str) -> dict[str, Any]:
    fields = store.read_session_fields(session_id)
    assert fields is not None
    return fields.session_state


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
            # The terminal save creates the row, and the state save writes the state again.
            await persist_to(store, session)(_run(f"r{index}"))
            assert session.stored_session_state == _row_state(store, session.session_id)
            store.save_session_state(session, session_state_snapshot(session.session_state))
            assert session.stored_session_state == _row_state(store, session.session_id)
    finally:
        stop.set()
        changer.join()
        sys.setswitchinterval(switch_interval)

    assert all(session.stored_session_state is not live for session in sessions)


def test_double_encoded_session_columns_load_and_are_rewritten_once(store: ConversationStore) -> None:
    """Agno 2 columns encoded twice load, and the field writes store them encoded once."""
    store.ensure_session_row(_session())
    _execute(
        store,
        "UPDATE code_sessions SET session_data = ?, metadata = ? WHERE session_id = 's1'",
        json.dumps(json.dumps({"session_state": {"a": 1}})),
        json.dumps(json.dumps({"k": "v"})),
    )
    session = _loaded(store)
    assert (session.session_state, session.metadata) == ({"a": 1}, {"k": "v"})

    store.save_session_state(session, session_state_snapshot(session.session_state))
    store.update_metadata(session, lambda metadata: {**(metadata or {}), "k2": "v2"})

    assert _query(store, "SELECT session_data, metadata FROM code_sessions") == [
        ('{"session_state": {"a": 1}}', '{"k": "v", "k2": "v2"}'),
    ]


def test_unreadable_session_columns_load_as_empty(store: ConversationStore) -> None:
    """One odd session row never fails a load, and a usage save leaves its bytes alone but moves its update time."""
    store.ensure_session_row(_session())
    _execute(
        store,
        "UPDATE code_sessions SET session_data = 'not json', metadata = '[1]', updated_at = 5 WHERE session_id = 's1'",
    )

    session = _loaded(store)
    assert (session.session_state, session.metadata) == ({}, None)

    _seed(store, [_run("r1", metrics=_usage(3))])
    [(session_data, updated_at)] = _query(store, "SELECT session_data, updated_at FROM code_sessions")
    assert (session_data, updated_at > 5) == ("not json", True)
    assert _usage_row(store, "r1")["metrics"]["input_tokens"] == 3


def test_json_null_session_data_counts_usage_like_an_empty_row(store: ConversationStore) -> None:
    """Agno wrote ``null`` for a session without data; its runs count in the totals and move its update time."""
    store.ensure_session_row(_session())
    _execute(store, "UPDATE code_sessions SET session_data = 'null', updated_at = 5 WHERE session_id = 's1'")

    store.save_run(_session(), _run("r1", RunStatus.paused, metrics=_usage(3)), record_usage=True)
    [(updated_at,)] = _query(store, "SELECT updated_at FROM code_sessions")
    assert updated_at > 5
    assert _totals(store)["input_tokens"] == 3

    store.save_run(_session(), _run("r1", metrics=_usage(7)), record_usage=True)
    assert _totals(store)["input_tokens"] == 7


# --- usage ------------------------------------------------------------------------------------------------------


def test_a_run_saved_again_counts_once_in_the_session_totals(store: ConversationStore) -> None:
    """A run saved when it paused, again when it finished, and again to link its reply counts its final usage once."""
    store.save_run(_session(), _run("r1", RunStatus.paused, metrics=_usage(10)), record_usage=True)
    store.save_run(_session(), _run("r2", metrics=_usage(100, model="other-model")), record_usage=True)
    store.save_run(_session(), _run("r1", metrics=_usage(25)), record_usage=True)
    session = _loaded(store)
    linked = copy.deepcopy(session.runs[-1])
    linked.metadata = {"matrix_response_event_id": "$reply"}
    store.save_runs(session, [linked])

    totals = _totals(store)
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (125, 2, 127)
    assert sorted((entry["id"], entry["input_tokens"]) for entry in totals["details"]["model"]) == [
        ("code-model", 25),
        ("other-model", 100),
    ]
    assert _usage_row(store, "r1")["metrics"]["input_tokens"] == 25


def test_team_member_runs_count_in_their_team_sessions_totals(store: ConversationStore) -> None:
    """Each member row and the team row add their own usage once."""
    team = _session(kind="team", owner="eng")
    _seed(
        store,
        [
            _run("m1", parent="t1", agent_id="helper", metrics=_usage(7, model="helper-model")),
            _team_run("t1", metrics=_usage(30)),
        ],
        team,
    )

    totals = _totals(store)
    assert totals["input_tokens"] == 37
    assert _usage_row(store, "m1")["parent_run_id"] == "t1"


def test_usage_saves_move_the_sessions_update_time_and_rewrites_do_not(store: ConversationStore) -> None:
    """Update-time cursors see a session whose run recorded usage, not one whose runs were only rewritten."""
    _seed(store, [_run("r1")])
    _execute(store, "UPDATE code_sessions SET updated_at = 5")
    session = _loaded(store)
    store.save_runs(session, [_run("r1", content="rewritten")])
    assert _query(store, "SELECT updated_at FROM code_sessions") == [(5,)]

    store.save_run(session, _run("r2"), record_usage=False)
    assert _query(store, "SELECT updated_at FROM code_sessions") == [(5,)]

    store.save_run(session, _run("r3"), record_usage=True)
    [(updated_at,)] = _query(store, "SELECT updated_at FROM code_sessions")
    assert updated_at > 5


def test_independent_usage_is_recorded_beside_the_runs_and_not_in_the_totals(store: ConversationStore) -> None:
    """Helper usage keeps its kind and requester, takes no ownership from the payload, and adds nothing to the totals."""
    created_at = 1_700_000_500
    metrics = {"input_tokens": 40, "output_tokens": 4, "total_tokens": 44}

    store.save_independent_usage(
        session_id="s1",
        usage_id="compaction:1",
        kind="compaction_summary",
        requester_id="@bob:example.test",
        run={
            "model_provider": "P",
            "model": "summary-model",
            "created_at": created_at,
            "metrics": metrics,
            "messages": [{"role": "assistant", "created_at": created_at, "metrics": metrics}],
            "metadata": {"requester_id": "@mallory:example.test"},
            "parent_run_id": "ignored",
        },
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
    assert _totals(store) == {}
    assert _loaded(store).runs == []


def test_independent_usage_needs_its_conversation_row(store: ConversationStore) -> None:
    """Without an initial session, helper usage needs a conversation row to belong to."""
    with pytest.raises(sqlite3.IntegrityError):
        store.save_independent_usage(
            session_id="s1",
            usage_id="routing:1",
            kind="routing",
            requester_id=None,
            run={"metrics": {}},
        )


def test_list_updated_sessions_returns_an_owners_sessions_updated_after_the_cursor(store: ConversationStore) -> None:
    """Sessions are listed per kind and owner, oldest update first, strictly after the cursor."""
    for session_id, owner, updated_at in (("a", "code", 10), ("b", "code", 30), ("c", "code", 20), ("d", "other", 40)):
        store.ensure_session_row(_session(session_id, owner=owner))
        _execute(store, "UPDATE code_sessions SET updated_at = ? WHERE session_id = ?", updated_at, session_id)
    store.ensure_session_row(_session("t", kind="team", owner="code"))

    assert store.list_updated_sessions("agent", "code", since=10) == [("c", 20), ("b", 30)]
    assert [session_id for session_id, _ in store.list_updated_sessions("team", "code", since=0)] == ["t"]
