"""The conversation databases Agno 3.0.9 wrote convert to storage v2 and read and write with nothing lost or changed.

A converted run is compared with the run its Agno row decodes to, as typed records, so these tests pin what the rows
hold and not how MindRoom encodes a run it writes.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.storage import new_scope_session
from mindroom.llm.runs import TeamRunRecord
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, run_from_row
from mindroom.session_storage.types import ConversationSession
from tests.conftest import persist_runs
from tests.llm.agno_dict_helpers import stored_run_record
from tests.session_storage.goldens import GoldenStore, agno_loaded_session, golden_sessions, golden_stores, stored_rows

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.history.types import ScopeSummary
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore

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
# Replayable sessions whose Agno row holds no scope metadata, which replaying their runs reproduces.
_WITHOUT_METADATA = [
    (pair, name)
    for pair, name in zip(_REPLAYABLE, _REPLAYABLE_IDS, strict=True)
    if agno_loaded_session(*pair)["metadata"] is None
]


def _as_agno_reread[RunT: AnyRunRecord](run: RunT) -> RunT:
    """Return a run as Agno's reader loaded it: an empty tool list as none, and no audio formats."""
    reread = replace(
        run,
        tools=run.tools or None,
        messages=[
            replace(message, audio=[replace(audio, format=None) for audio in message.audio])
            if message.audio
            else message
            for message in run.messages
        ],
    )
    if isinstance(reread, TeamRunRecord):
        reread.member_responses = [_as_agno_reread(member) for member in reread.member_responses]
    return reread


def _summary_dict(summary: ScopeSummary | None) -> dict[str, str] | None:
    return None if summary is None else {"summary": summary.text, "updated_at": summary.updated_at.isoformat()}


def _load(conversation: ConversationStore, store: GoldenStore, session_id: str) -> ConversationSession:
    session = conversation.load_session(session_id, store.sessions[session_id])
    assert session is not None
    return session


def _query(db_file: Path, sql: str, *parameters: object) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(db_file)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def _folded_message_ids(db_file: Path) -> dict[str, set[str]]:
    """Return the ids of the messages each run's mid-turn folds took, by run, from an Agno-layout file's segments."""
    folded: dict[str, set[str]] = {}
    for row in _query(
        db_file,
        f'SELECT segment_run_id, segment FROM "{AgnoTableNames.of_database(db_file).summaries}" '  # noqa: S608
        "WHERE segment_run_id IS NOT NULL",
    ):
        messages = json.loads(row["segment"])["messages"]
        folded.setdefault(row["segment_run_id"], set()).update(message["id"] for message in messages)
    return folded


def _replay_view(run_data: dict[str, Any], kept: Callable[[str], bool]) -> AnyRunRecord:
    """Return the run Agno stored as ``run_data`` with only the messages whose ids ``kept`` accepts."""
    run = stored_run_record(run_data)
    return replace(run, messages=[message for message in run.messages if kept(message.id)])


@pytest.mark.parametrize(("store", "session_id"), _SESSIONS, ids=_SESSION_IDS)
def test_golden_sessions_load_as_agno_loaded_them(store: GoldenStore, session_id: str, tmp_path: Path) -> None:
    """Fields, run selection, order, summary, and every run match Agno's load, but for Agno's own reader losses."""
    session = _load(store.open(tmp_path), store, session_id)
    agno = agno_loaded_session(store, session_id)

    assert (
        session.session_id,
        session.owner_id,
        session.metadata,
        session.session_state,
        session.created_at,
    ) == (
        agno["session_id"],
        agno["team_id"] if session.kind == "team" else agno["agent_id"],
        agno["metadata"],
        agno["session_data"]["session_state"],
        agno["created_at"],
    )
    assert _summary_dict(session.summary) == agno["summary"]
    assert [run.run_id for run in session.runs] == [run["run_id"] for run in agno["runs"]]
    assert [_as_agno_reread(run) for run in session.runs] == [stored_run_record(run) for run in agno["runs"]]


@pytest.mark.parametrize(("store", "session_id"), _SESSIONS, ids=_SESSION_IDS)
def test_golden_runs_load_every_field_their_rows_hold(store: GoldenStore, session_id: str, tmp_path: Path) -> None:
    """A loaded run is the run its Agno row decodes to, minus only the messages a fold holds."""
    rows = {
        row["run_id"]: row["run_data"]
        for row in stored_rows(store.restore(tmp_path / "agno"), store.names.runs, session_id)
    }
    session = _load(store.open(tmp_path / "converted"), store, session_id)
    loaded_message_ids = {
        run["run_id"]: {message["id"] for message in run["messages"]}
        for run in agno_loaded_session(store, session_id)["runs"]
    }

    for run in session.runs:
        assert run == _replay_view(rows[run.run_id], loaded_message_ids[run.run_id].__contains__), run.run_id


@pytest.mark.parametrize("store", golden_stores(), ids=lambda store: store.label)
def test_golden_run_logs_list_and_get_as_stored(store: GoldenStore, tmp_path: Path) -> None:
    """The whole run log, and each run by id, read back as Agno stored it, minus the messages a fold holds."""
    agno = store.restore(tmp_path / "agno")
    folded = _folded_message_ids(agno)
    conversation = store.open(tmp_path / "converted")

    for session_id in store.sessions:
        rows = stored_rows(agno, store.names.runs, session_id)
        assert rows
        expected = [
            _replay_view(row["run_data"], lambda message_id, row=row: message_id not in folded.get(row["run_id"], ()))
            for row in rows
        ]
        assert conversation.list_runs(session_id) == expected
        for row, stored in zip(rows, expected, strict=True):
            assert conversation.get_run(row["run_id"]) == stored
    assert conversation.get_run("no-such-run") is None


def _log_rows(db_file: Path, names: AgnoTableNames, session_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return what a session's log and ledger rows and its session row hold, update times aside.

    Message rows come in replay order, the folded ones after; ``ord`` itself only orders the unfolded rows of a run,
    which a save numbers from 0, so it is left out.
    """
    runs = _query(
        db_file,
        f'SELECT * FROM "{names.log_runs}" WHERE session_id = ? ORDER BY seq',  # noqa: S608
        session_id,
    )
    return {
        "runs": [{key: value for key, value in row.items() if key != "updated_at"} for row in runs],
        "messages": _query(
            db_file,
            f'SELECT run_id, message_id, folded_by, data FROM "{names.log_messages}" WHERE run_id IN '  # noqa: S608
            f'(SELECT run_id FROM "{names.log_runs}" WHERE session_id = ?) '
            "ORDER BY run_id, folded_by IS NOT NULL, ord, message_id",
            session_id,
        ),
        "usage": _query(db_file, f'SELECT * FROM "{names.usage}" WHERE session_id = ? ORDER BY id', session_id),  # noqa: S608
        "session": _query(db_file, f'SELECT * FROM "{names.sessions}" WHERE session_id = ?', session_id),  # noqa: S608
    }


@pytest.mark.parametrize(("store", "session_id"), _SESSIONS, ids=_SESSION_IDS)
def test_saving_and_rewriting_every_loaded_golden_run_changes_nothing(
    store: GoldenStore,
    session_id: str,
    tmp_path: Path,
) -> None:
    """Saving each loaded run's copy and rewriting their messages unchanged keeps every row but its update time."""
    conversation = store.open(tmp_path)
    kind = store.sessions[session_id]
    session = _load(conversation, store, session_id)
    loaded = [run.to_dict() for run in session.runs]
    before = _log_rows(conversation.path, store.names, session_id)

    for run in list(session.runs):
        conversation.save_run(session, copy.deepcopy(run), record_usage=False)
    conversation.rewrite_run_messages(session_id, kind, lambda runs: runs, session=session)

    assert _log_rows(conversation.path, store.names, session_id) == before
    assert [run.to_dict() for run in session.runs] == loaded
    assert [run.to_dict() for run in _load(conversation, store, session_id).runs] == loaded


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


def _without_update_time(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "updated_at"}


@pytest.mark.parametrize(("store", "session_id"), _REPLAYABLE, ids=_REPLAYABLE_IDS)
def test_replaying_golden_runs_with_usage_reproduces_rows_ledger_and_totals(
    store: GoldenStore,
    session_id: str,
    tmp_path: Path,
) -> None:
    """Saving each stored run with its usage into a new database writes the runs, snapshots, and totals Agno wrote."""
    golden = store.restore(tmp_path / "golden")
    rows = stored_rows(golden, store.names.runs, session_id)
    [golden_session] = _query(golden, f'SELECT * FROM "{store.names.sessions}" WHERE session_id = ?', session_id)  # noqa: S608
    fresh = create_state_storage(store.names.storage_name, tmp_path / "fresh")
    # As the runtime builds the session of a conversation's first turn: it does not know the requester.
    session = ConversationSession(
        session_id=session_id,
        kind=golden_session["session_type"],
        owner_id=golden_session["agent_id"] or golden_session["team_id"],
        created_at=golden_session["created_at"],
    )

    persist_runs(fresh, session, *_replayed(rows))

    replayed = _query(
        fresh.path,
        f'SELECT run_id, kind, scope_key, parent_run_id, status, created_at FROM "{store.names.log_runs}" '  # noqa: S608
        "WHERE session_id = ? ORDER BY seq",
        session_id,
    )
    assert replayed == [
        {
            "run_id": row["run_id"],
            "kind": row["run_type"],
            "scope_key": f"{row['run_type']}:{row['agent_id'] if row['run_type'] == 'agent' else row['team_id']}",
            "parent_run_id": row["parent_run_id"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    assert fresh.list_runs(session_id) == _replayed(rows)
    assert sorted(
        (row["run_id"], _without_update_time(row["usage_data"]))
        for row in stored_rows(fresh.path, store.names.usage, session_id)
    ) == sorted(
        (row["run_id"], _without_update_time(row["usage_data"]))
        for row in stored_rows(golden, store.names.usage, session_id)
    )
    [fresh_session] = _query(fresh.path, f'SELECT * FROM "{store.names.sessions}" WHERE session_id = ?', session_id)  # noqa: S608
    assert fresh_session["user_id"] == golden_session["user_id"]
    assert _sorted_details(json.loads(fresh_session["session_data"])["session_metrics"]) == _sorted_details(
        json.loads(golden_session["session_data"])["session_metrics"],
    )


@pytest.mark.parametrize(
    ("store", "session_id"),
    [pair for pair, _ in _WITHOUT_METADATA],
    ids=[name for _, name in _WITHOUT_METADATA],
)
def test_a_first_turns_session_row_stores_no_metadata_as_agno_stored_it(
    store: GoldenStore,
    session_id: str,
    tmp_path: Path,
) -> None:
    """The row of a session built as the runtime builds a first turn's holds the JSON text Agno's JSON column wrote."""
    golden = store.restore(tmp_path / "golden")
    rows = stored_rows(golden, store.names.runs, session_id)
    [golden_row] = _query(golden, f'SELECT * FROM "{store.names.sessions}" WHERE session_id = ?', session_id)  # noqa: S608
    session = new_scope_session(
        session_id=session_id,
        scope_id=golden_row["team_id"] or golden_row["agent_id"],
        is_team=golden_row["session_type"] == "team",
    )
    fresh = create_state_storage(store.names.storage_name, tmp_path / "fresh")

    persist_runs(fresh, session, *_replayed(rows))

    assert golden_row["metadata"] == "null"
    sessions = f'"{store.names.sessions}"'
    assert _query(fresh.path, f"SELECT metadata FROM {sessions} WHERE session_id = ?", session_id) == [  # noqa: S608
        {"metadata": "null"},
    ]


def test_linking_a_delivered_reply_rewrites_only_that_run(tmp_path: Path) -> None:
    """Production links each delivered reply's run to its Matrix event id; nothing else about the run moves."""
    [store] = [store for store in golden_stores() if store.label == "agent_code"]
    conversation = store.open(tmp_path)
    session_id = "!room:example.org:$approved"
    session = _load(conversation, store, session_id)
    [run] = session.runs
    before = _log_rows(conversation.path, store.names, session_id)

    assert conversation.patch_run_metadata(
        session_id,
        run.run_id,
        {"matrix_response_event_id": "$reply"},
        session=session,
    )

    after = _log_rows(conversation.path, store.names, session_id)
    [(before_run, after_run)] = zip(before.pop("runs"), after.pop("runs"), strict=True)
    linked = {**(run.metadata or {}), "matrix_response_event_id": "$reply"}
    assert after_run == {**before_run, "metadata": json.dumps(linked)}
    assert after == before
    assert [linked_run.to_dict() for linked_run in session.runs] == [{**run.to_dict(), "metadata": linked}]


def test_a_resumed_team_run_keeps_its_empty_member_and_requirement_lists(tmp_path: Path) -> None:
    """Agno stored a continued team run with ``member_responses: []`` and ``requirements: []``; both survive."""
    [store] = [store for store in golden_stores() if store.label == "team_squad"]
    conversation = store.open(tmp_path)

    team_run = conversation.get_run("team-approved")

    assert isinstance(team_run, TeamRunRecord)
    assert (team_run.member_responses, team_run.requirements) == ([], [])


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


@pytest.mark.parametrize("store", golden_stores(), ids=lambda store: store.label)
def test_a_converted_golden_has_the_layout_a_new_database_gets(store: GoldenStore, tmp_path: Path) -> None:
    """Every column, key, index, and schema object of a converted database is what a new database is created with."""
    converted = store.open(tmp_path / "converted").path
    fresh = create_state_storage(store.names.storage_name, tmp_path / "fresh").path
    names = store.names
    tables = (
        names.sessions,
        names.usage,
        names.log_meta,
        names.log_runs,
        names.log_messages,
        names.log_summaries,
        names.log_removed,
    )

    assert _layout(converted, tables) == _layout(fresh, tables)
    assert _schema_objects(converted) == _schema_objects(fresh)
