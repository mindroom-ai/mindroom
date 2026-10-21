"""The owned store reads and writes the conversation databases Agno 3.0.9 wrote, with nothing lost or changed."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.runs import RunRecord
from mindroom.session_storage.codec import run_from_row
from mindroom.session_storage.store import open_conversation_store
from mindroom.session_storage.types import ConversationSession
from tests.llm.agno_dict_helpers import expected_run_dict
from tests.session_storage.goldens import GoldenStore, agno_loaded_session, golden_sessions, golden_stores, stored_rows

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.history.types import ScopeSummary
    from mindroom.llm.runs import AnyRunRecord

_SESSIONS = golden_sessions()
_SESSION_IDS = [f"{store.label}:{session_id}" for store, session_id in _SESSIONS]
# Sessions whose every run was last saved with its usage, so replaying the rows reproduces the ledger and totals:
# MindRoom's interrupted-turn records replace a run's row without recording usage ($conversation), a cut removed a
# run whose usage stays in the ledger ($compacted), and Agno 2 kept no session totals (agno2_migrated).
_NOT_REPLAYABLE = {
    "agent_code:!room:example.org:$conversation",
    "agent_code:!room:example.org:$compacted",
    "agno2_migrated:session-1",
}
_REPLAYABLE = [pair for pair, name in zip(_SESSIONS, _SESSION_IDS, strict=True) if name not in _NOT_REPLAYABLE]
_REPLAYABLE_IDS = [name for name in _SESSION_IDS if name not in _NOT_REPLAYABLE]
_ROW_COLUMNS = ("run_id", "run_type", "agent_id", "team_id", "workflow_id", "user_id", "parent_run_id", "status")


def _as_agno_reread(run: dict[str, Any]) -> dict[str, Any]:
    """Return what Agno's reader writes back for a run dict: no empty tool list and no audio formats."""
    reread = {key: value for key, value in run.items() if not (key == "tools" and value == [])}
    reread["messages"] = [
        {**message, "audio": [{k: v for k, v in audio.items() if k != "format"} for audio in message["audio"]]}
        if "audio" in message
        else message
        for message in run["messages"]
    ]
    if "member_responses" in run:
        reread["member_responses"] = [_as_agno_reread(member) for member in run["member_responses"]]
    return reread


def _summary_dict(summary: ScopeSummary | None) -> dict[str, str] | None:
    return None if summary is None else {"summary": summary.text, "updated_at": summary.updated_at.isoformat()}


def _load(store: GoldenStore, db_file: Path, session_id: str) -> ConversationSession:
    session = open_conversation_store(db_file, store.names).load_session(session_id, store.sessions[session_id])
    assert session is not None
    return session


@pytest.mark.parametrize(("store", "session_id"), _SESSIONS, ids=_SESSION_IDS)
def test_golden_sessions_load_as_agno_loaded_them(store: GoldenStore, session_id: str, tmp_path: Path) -> None:
    """Fields, run selection, order, summary, and every run match Agno's load, but for Agno's own reader losses."""
    session = _load(store, store.restore(tmp_path), session_id)
    agno = agno_loaded_session(store, session_id)

    assert (
        session.session_id,
        session.owner_id,
        session.user_id,
        session.metadata,
        session.session_state,
        session.created_at,
        session.updated_at,
    ) == (
        agno["session_id"],
        agno["team_id"] if session.kind == "team" else agno["agent_id"],
        agno["user_id"],
        agno["metadata"],
        agno["session_data"]["session_state"],
        agno["created_at"],
        agno["updated_at"],
    )
    assert _summary_dict(session.summary) == agno["summary"]
    assert [run.run_id for run in session.runs] == [run["run_id"] for run in agno["runs"]]
    assert [_as_agno_reread(run.to_dict()) for run in session.runs] == [expected_run_dict(run) for run in agno["runs"]]


@pytest.mark.parametrize(("store", "session_id"), _SESSIONS, ids=_SESSION_IDS)
def test_golden_runs_load_with_every_stored_key(store: GoldenStore, session_id: str, tmp_path: Path) -> None:
    """A loaded run writes back exactly its stored row, keys in stored order, minus only messages a fold holds."""
    db_file = store.restore(tmp_path)
    session = _load(store, db_file, session_id)
    rows = {row["run_id"]: row["run_data"] for row in stored_rows(db_file, store.names.runs, session_id)}
    loaded_message_ids = {
        run["run_id"]: {message["id"] for message in run["messages"]}
        for run in agno_loaded_session(store, session_id)["runs"]
    }

    for run in session.runs:
        expected = expected_run_dict(rows[run.run_id])
        expected["messages"] = [
            message for message in expected["messages"] if message["id"] in loaded_message_ids[run.run_id]
        ]
        written = run.to_dict()
        assert written == expected, run.run_id
        assert list(written) == list(expected), run.run_id


@pytest.mark.parametrize("store", golden_stores(), ids=lambda store: store.label)
def test_golden_run_logs_list_and_get_as_stored(store: GoldenStore, tmp_path: Path) -> None:
    """The whole run log, and each run by id, read back exactly as stored."""
    conversation = store.open(tmp_path)

    for session_id in store.sessions:
        rows = stored_rows(conversation.path, store.names.runs, session_id)
        assert rows
        assert [run.to_dict() for run in conversation.list_runs(session_id)] == [
            expected_run_dict(row["run_data"]) for row in rows
        ]
        for row in rows:
            run = conversation.get_run(row["run_id"])
            assert run is not None
            assert run.to_dict() == expected_run_dict(row["run_data"])
    assert conversation.get_run("no-such-run") is None


def _session_row(db_file: Path, table: str, session_id: str) -> tuple[Any, ...]:
    with closing(sqlite3.connect(db_file)) as connection:
        return connection.execute(f'SELECT * FROM "{table}" WHERE session_id = ?', (session_id,)).fetchone()  # noqa: S608


def _without_update_time(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "updated_at"}


@pytest.mark.parametrize(("store", "session_id"), _SESSIONS, ids=_SESSION_IDS)
def test_rewriting_every_loaded_golden_run_changes_only_its_rows(
    store: GoldenStore,
    session_id: str,
    tmp_path: Path,
) -> None:
    """A conversation rewrite of unchanged copies keeps every place, the ledger, the totals, and the session row."""
    conversation = store.open(tmp_path)
    session = conversation.load_session(session_id, store.sessions[session_id])
    assert session is not None
    loaded = [run.to_dict() for run in session.runs]
    before = stored_rows(conversation.path, store.names.runs, session_id)
    usage_before = stored_rows(conversation.path, store.names.usage, session_id)
    session_before = _session_row(conversation.path, store.names.sessions, session_id)

    conversation.save_runs(session, [copy.deepcopy(run) for run in session.runs])
    reloaded = conversation.load_session(session_id, store.sessions[session_id])

    after = stored_rows(conversation.path, store.names.runs, session_id)
    rewritten = {run["run_id"]: run for run in loaded}
    assert [(row["run_id"], row["run_index"]) for row in after] == [(row["run_id"], row["run_index"]) for row in before]
    assert [_without_update_time(row) for row in after] == [
        {**_without_update_time(row), "run_data": rewritten.get(row["run_id"], row["run_data"])} for row in before
    ]
    assert stored_rows(conversation.path, store.names.usage, session_id) == usage_before
    assert _session_row(conversation.path, store.names.sessions, session_id) == session_before
    assert reloaded is not None
    assert [run.to_dict() for run in reloaded.runs] == [run.to_dict() for run in session.runs] == loaded


def _sorted_details(totals: dict[str, Any]) -> dict[str, Any]:
    details = {
        model_type: sorted(entries, key=lambda entry: (entry.get("provider", ""), entry.get("id", "")))
        for model_type, entries in totals.get("details", {}).items()
    }
    return {**totals, "details": details}


def _replayed(rows: list[dict[str, Any]]) -> list[AnyRunRecord]:
    runs = [run_from_row(row["run_type"], json.dumps(row["run_data"])) for row in rows]
    assert all(run is not None for run in runs)
    return [run for run in runs if run is not None]


@pytest.mark.parametrize(("store", "session_id"), _REPLAYABLE, ids=_REPLAYABLE_IDS)
def test_replaying_golden_runs_with_usage_reproduces_rows_ledger_and_totals(
    store: GoldenStore,
    session_id: str,
    tmp_path: Path,
) -> None:
    """Saving each stored run with its usage into a fresh store writes the rows, snapshots, and totals Agno wrote."""
    golden = store.restore(tmp_path / "golden")
    rows = stored_rows(golden, store.names.runs, session_id)
    with closing(sqlite3.connect(golden)) as connection:
        kind, owner, user_id, created_at, session_data = connection.execute(
            f'SELECT session_type, COALESCE(agent_id, team_id), user_id, created_at, session_data FROM "{store.names.sessions}" '  # noqa: S608
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    fresh = open_conversation_store(tmp_path / "fresh.db", store.names)
    session = ConversationSession(
        session_id=session_id,
        kind=kind,
        owner_id=owner,
        user_id=user_id,
        created_at=created_at,
    )

    outcomes = [fresh.save_run(session, run, record_usage=True) for run in _replayed(rows)]

    replayed = stored_rows(fresh.path, store.names.runs, session_id)
    assert outcomes == ["written"] * len(rows)
    assert [[row[column] for column in _ROW_COLUMNS] for row in replayed] == [
        [row[column] for column in _ROW_COLUMNS] for row in rows
    ]
    assert [row["created_at"] for row in replayed] == [row["created_at"] for row in rows]
    assert [row["run_data"] for row in replayed] == [expected_run_dict(row["run_data"]) for row in rows]
    assert sorted(
        (row["run_id"], _without_update_time(row["usage_data"]))
        for row in stored_rows(fresh.path, store.names.usage, session_id)
    ) == sorted(
        (row["run_id"], _without_update_time(row["usage_data"]))
        for row in stored_rows(golden, store.names.usage, session_id)
    )
    (fresh_session_data,) = _session_row(fresh.path, store.names.sessions, session_id)[6:7]
    assert _sorted_details(json.loads(fresh_session_data)["session_metrics"]) == _sorted_details(
        json.loads(session_data)["session_metrics"],
    )


def test_linking_a_delivered_reply_rewrites_only_that_run(tmp_path: Path) -> None:
    """Production re-saves each delivered reply's run with its Matrix event id; nothing else about the run moves."""
    [store] = [store for store in golden_stores() if store.label == "agent_code"]
    conversation = store.open(tmp_path)
    session_id = "!room:example.org:$approved"
    session = conversation.load_session(session_id, "agent")
    assert session is not None
    usage_before = stored_rows(conversation.path, store.names.usage, session_id)
    [before] = stored_rows(conversation.path, store.names.runs, session_id)
    linked = copy.copy(session.runs[0])
    linked.metadata = {**(linked.metadata or {}), "matrix_response_event_id": "$reply"}

    conversation.save_runs(session, [linked])

    [after] = stored_rows(conversation.path, store.names.runs, session_id)
    assert session.runs == [linked]
    assert after["run_index"] == before["run_index"]
    assert after["run_data"] == {
        **expected_run_dict(before["run_data"]),
        "metadata": {**before["run_data"]["metadata"], "matrix_response_event_id": "$reply"},
    }
    assert stored_rows(conversation.path, store.names.usage, session_id) == usage_before


def test_a_resumed_team_run_keeps_its_empty_member_and_requirement_lists(tmp_path: Path) -> None:
    """Agno stored a continued team run with ``member_responses: []`` and ``requirements: []``; both survive a rewrite."""
    [store] = [store for store in golden_stores() if store.label == "team_squad"]
    conversation = store.open(tmp_path)
    session_id = "!room:example.org:$squad-approved"
    session = conversation.load_session(session_id, "team")
    assert session is not None
    [team_run] = [run for run in session.runs if run.run_id == "team-approved"]

    conversation.save_runs(session, [copy.deepcopy(team_run)])

    [row] = [
        row for row in stored_rows(conversation.path, store.names.runs, session_id) if row["run_id"] == "team-approved"
    ]
    assert (row["run_data"]["member_responses"], row["run_data"]["requirements"]) == ([], [])


def _layout(db_file: Path, tables: tuple[str, ...]) -> dict[str, Any]:
    with closing(sqlite3.connect(db_file)) as connection:
        return {
            table: (
                connection.execute(f'PRAGMA table_info("{table}")').fetchall(),
                connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall(),
                sorted(
                    (name, [column for _, _, column in connection.execute(f'PRAGMA index_info("{name}")')])
                    for _, name, *_ in connection.execute(f'PRAGMA index_list("{table}")')
                ),
            )
            for table in tables
        }


def _schema_objects(db_file: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(db_file)) as connection:
        return connection.execute("SELECT type, name FROM sqlite_master ORDER BY type, name").fetchall()


def test_new_databases_get_the_layout_agno_created(tmp_path: Path) -> None:
    """A database the owned store creates has Agno 3's tables and indexes, and writing to Agno's adds none."""
    [store] = [store for store in golden_stores() if store.label == "agent_code"]
    golden = store.restore(tmp_path / "golden")
    objects = _schema_objects(golden)
    session = ConversationSession(session_id="s1", kind="agent", owner_id="code")
    tables = (
        store.names.sessions,
        store.names.runs,
        store.names.summaries,
        store.names.removed_runs,
        store.names.usage,
    )

    for db_file in (tmp_path / "fresh.db", golden):
        open_conversation_store(db_file, store.names).save_run(
            session,
            RunRecord(run_id="r1", agent_id="code"),
            record_usage=True,
        )

    assert _layout(tmp_path / "fresh.db", tables) == _layout(golden, tables)
    assert _schema_objects(golden) == objects
    # Agno's own bookkeeping table is the one object the owned store does not create.
    assert _schema_objects(tmp_path / "fresh.db") == [item for item in objects if "agno_schema_versions" not in item[1]]
