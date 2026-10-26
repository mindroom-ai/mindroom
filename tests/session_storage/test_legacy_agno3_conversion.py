"""The one-way conversion of a conversation database from the Agno layout to storage v2."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from mindroom import constants
from mindroom.constants import resolve_runtime_paths
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild, DelegationState, SubagentPersona
from mindroom.history.storage import has_queued_force_compaction
from mindroom.history.summary_rows import folded_tool_call_ids, history_event_ids, legacy_event_ids, retire
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage import legacy_agno3_conversion
from mindroom.session_storage.legacy_agno3_backup import BACKUP_SUFFIX, ConversionSpaceError
from mindroom.session_storage.legacy_agno3_conversion import (
    migrate_conversation_database,
    migrate_conversation_storage,
)
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_schema_sql
from mindroom.session_storage.schema import LOG_VERSION, SESSION_COLUMNS, TableNames, table_names
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from tests.llm.agno_dict_helpers import DROPPED_RUN_KEYS, DROPPED_TOOL_EXECUTION_KEYS, expected_run_dict
from tests.session_storage.agno_layout import write_agno_database
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.record import FIXTURES, GOLDEN_DUMPS, storage_name_of

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import ExitStack

    from mindroom.llm.runs import AnyRunRecord

_NAMES = TableNames("code")
_AGNO_PARITY = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity"
_SESSION = "s1"
_DUMPS = {
    **{f"goldens/{dump.stem}": dump for dump in GOLDEN_DUMPS},
    **{f"legacy/{dump.stem}": dump for dump in sorted((FIXTURES / "legacy").glob("*.sql"))},
}
_TAKE_WRITE_LOCK = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    connection.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as error:
    print(error)
else:
    print("locked")
"""
# A writer that changes the file beyond its cache, then dies before it commits, leaving a hot journal.
_DIE_MID_TRANSACTION = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA cache_size = 1")
connection.execute("BEGIN IMMEDIATE")
connection.execute("CREATE TABLE filler (data BLOB)")
for _ in range(64):
    connection.execute("INSERT INTO filler VALUES (randomblob(4096))")
os._exit(0)
"""


# Building Agno-layout databases.


def _restore(dump: Path | str, root: Path) -> tuple[Path, TableNames]:
    text = dump if isinstance(dump, str) else dump.read_text(encoding="utf-8")
    names = TableNames(storage_name_of(text))
    database = names.database_path(root)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(text)
    return database, names


def _legacy(name: str, root: Path) -> Path:
    database, _ = _restore(FIXTURES / "legacy" / f"{name}.sql", root)
    return database


def _agno_database(root: Path, *sessions: tuple[str, str, str], names: TableNames = _NAMES) -> Path:
    """Create an empty Agno-layout database with the sessions ``(session_id, kind, owner)``, by default one agent's."""
    database = names.database_path(root)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        for statement in agno_schema_sql(AgnoTableNames(names.storage_name)):
            connection.execute(statement)
        for session_id, kind, owner in sessions or ((_SESSION, "agent", "code"),):
            connection.execute(
                f'INSERT INTO "{names.sessions}" (session_id, session_type, {"team_id" if kind == "team" else "agent_id"}, '  # noqa: S608
                "session_data, metadata, created_at, updated_at) VALUES (?, ?, ?, '{}', 'null', 1, 1)",
                (session_id, kind, owner),
            )
    return database


def _run(run_id: str, *, status: RunStatus = RunStatus.completed, **fields: Any) -> RunRecord:  # noqa: ANN401
    return RunRecord(
        run_id=run_id,
        agent_id="code",
        session_id=_SESSION,
        status=status,
        created_at=100,
        messages=[Message(role="user", content=f"question {run_id}", id=f"{run_id}.u", created_at=100)],
        metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}"},
        **fields,
    )


def _insert_run(
    database: Path,
    run: AnyRunRecord | dict[str, Any],
    *,
    run_index: int | None,
    run_data: str | None = None,
    run_type: str | None = None,
    session_id: str = _SESSION,
    created_at: int = 100,
    updated_at: int | None = 100,
) -> None:
    data = run if isinstance(run, dict) else run.to_dict()
    kind = run_type or ("team" if "member_responses" in data else "agent")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            f'INSERT INTO "{AgnoTableNames.of_database(database).runs}" (run_id, session_id, run_type, agent_id, team_id, '  # noqa: S608
            "parent_run_id, status, run_index, run_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["run_id"],
                session_id,
                kind,
                data.get("agent_id"),
                data.get("team_id"),
                data.get("parent_run_id"),
                data["status"],
                run_index,
                run_data if run_data is not None else json.dumps(data),
                created_at,
                updated_at,
            ),
        )


def _insert_summary(
    database: Path,
    row_id: int,
    after_run_index: int,
    *,
    summary: str | None = "summary",
    legacy: int = 0,
    segment_run_id: str | None = None,
    segment: dict[str, Any] | None = None,
    raw_segment: str | None = None,
    event_ids: tuple[str, ...] = (),
    session_id: str = _SESSION,
    created_at: int = 200,
) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'INSERT INTO "code_sessions_summaries" (id, session_id, scope_key, after_run_index, summary, summary_model, '
            "event_ids, seen_event_ids, legacy, created_at, segment_run_id, segment) "
            "VALUES (?, ?, 'agent:code', ?, ?, 'model', ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                session_id,
                after_run_index,
                summary,
                json.dumps(event_ids),
                json.dumps(event_ids),
                legacy,
                created_at,
                segment_run_id,
                raw_segment if segment is None else json.dumps(segment),
            ),
        )


def _store_raw(database: Path, table: str, column: str, value: object, **row: object) -> None:
    """Store ``value`` in ``column`` of ``table``'s row ``row`` names, as worker code could, whatever its type."""
    [(key, match)] = row.items()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f'UPDATE "{table}" SET {column} = ? WHERE {key} = ?', (value, match))  # noqa: S608


def _segment(
    *messages: Message,
    metadata: dict[str, Any] | None = None,
    tools: tuple[ToolExecution, ...] = (),
    members: tuple[RunRecord, ...] = (),
) -> dict[str, Any]:
    return {
        "metadata": metadata or {},
        "messages": [message.to_dict() for message in messages],
        "tools": [tool.to_dict() for tool in tools],
        "member_responses": [member.to_dict() for member in members],
    }


# Reading what the conversion wrote.


def _converted(database: Path) -> ConversationStore:
    migrate_conversation_database(database, TableNames.of_database(database))
    return ConversationStore(database)


def _query(database: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _seqs(database: Path) -> dict[str, int]:
    return dict(_query(database, 'SELECT run_id, seq FROM "code_sessions_log_runs"'))


def _message_rows(database: Path, run_id: str) -> list[tuple[str, int | None]]:
    return _query(
        database,
        'SELECT message_id, folded_by FROM "code_sessions_log_messages" WHERE run_id = ? ORDER BY ord, message_id',
        (run_id,),
    )


def _tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return table_names(connection)


def _is_converted(database: Path) -> bool:
    return TableNames.of_database(database).log_meta in _tables(database)


def _dump(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        return "\n".join(connection.iterdump())


def _loaded_ids(store: ConversationStore, session_id: str = _SESSION) -> list[str]:
    session = store.load_session(session_id, "agent")
    assert session is not None
    return [run.run_id for run in session.runs]


def _skipped(logs: list[dict[str, Any]], event: str) -> list[object]:
    return [entry.get("path", entry.get("run_id")) for entry in logs if entry["event"] == event]


def _other_process_takes_write_lock(database: Path) -> str:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _TAKE_WRITE_LOCK, str(database)],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


# The rows each step converts.


def test_runs_convert_in_load_order_with_their_row_ids(tmp_path: Path) -> None:
    """Each run gets the next ``seq`` in the order a load read it, a row without a position last, with its row's ids."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("later-tie"), run_index=1, created_at=300)
    _insert_run(database, _run("unplaced"), run_index=None)
    _insert_run(database, _run("first"), run_index=0, updated_at=None)
    _insert_run(database, _run("earlier-tie"), run_index=1, created_at=200)
    renamed = _run("in-json").to_dict() | {"session_id": "elsewhere"}
    _insert_run(database, renamed, run_index=2, run_data=json.dumps(renamed | {"run_id": "in-json"}))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE \"code_sessions_runs\" SET run_id = 'row-id' WHERE run_id = 'in-json'")

    store = _converted(database)

    assert sorted(_seqs(database), key=_seqs(database).get) == [
        "first",
        "earlier-tie",
        "later-tie",
        "row-id",
        "unplaced",
    ]
    stored = store.get_run("row-id")
    assert stored is not None
    assert (stored.run_id, stored.session_id) == ("row-id", _SESSION)
    assert _query(database, "SELECT created_at, updated_at FROM \"code_sessions_log_runs\" WHERE run_id = 'first'") == [
        (100, 100),
    ]
    assert _query(database, 'SELECT next_seq FROM "code_sessions_log_meta"') == [(6,)]


def test_rows_without_a_session_row_are_left_out(tmp_path: Path) -> None:
    """Rows of a session without a session row, which a cascade would have deleted, do not stop the conversion."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("kept"), run_index=0)
    _insert_run(database, _run("ghost-run"), run_index=0, session_id="ghost")
    _insert_summary(database, 1, 0, session_id="ghost")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO \"code_sessions_removed_runs\" VALUES ('ghost-gone', 'ghost')")

    _converted(database)

    assert _seqs(database) == {"kept": 1}
    assert _query(database, 'SELECT count(*) FROM "code_sessions_log_summaries"') == [(0,)]
    assert _query(database, 'SELECT count(*) FROM "code_sessions_log_removed"') == [(0,)]


def test_runs_whose_parents_form_a_cycle_convert(tmp_path: Path) -> None:
    """Run rows naming each other as parents, which no writer stores, still convert."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("a", parent_run_id="b"), run_index=0)
    _insert_run(database, _run("b", parent_run_id="a"), run_index=1)

    _converted(database)

    assert set(_seqs(database)) == {"a", "b"}


def test_a_database_missing_an_agno_table_converts(tmp_path: Path) -> None:
    """A database lacking one of the Agno tables, which no writer leaves, converts as if that table were empty."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('DROP TABLE "code_sessions_removed_runs"')

    store = _converted(database)

    assert _loaded_ids(store) == ["r0"]


def test_members_of_members_join_their_team_run(tmp_path: Path) -> None:
    """A member's member is stored as a member of the top-level team run and loads with it."""
    database = _legacy("synthetic__members_of_members", tmp_path)

    store = _converted(database)

    assert dict(_query(database, 'SELECT run_id, parent_run_id FROM "code_sessions_log_runs"')) == {
        "t0": None,
        "m0": "t0",
        "mm0": "t0",
    }
    session = store.load_session("session", "team")
    assert session is not None
    assert {run.run_id for run in session.runs} == {"t0", "m0", "mm0"}


def test_a_paused_team_runs_embedded_members_become_its_member_rows(tmp_path: Path) -> None:
    """An embedded member replaces its member's own row, and one without a row gets a row right after its team run."""
    database = _legacy("synthetic__embedded_paused_member_advanced", tmp_path / "advanced")
    store = _converted(database)
    member = store.get_run("m0")
    team = store.get_run("t0")
    assert member is not None
    assert isinstance(team, TeamRunRecord)
    assert member.status is RunStatus.paused
    assert [run.run_id for run in team.member_responses] == ["m0"]

    database = _agno_database(tmp_path / "created", (_SESSION, "team", "squad"))
    tool = ToolExecution(tool_call_id="call", tool_name="dangerous", requires_confirmation=True)
    embedded = _run("member", status=RunStatus.paused, parent_run_id="team", tools=[tool])
    team_run = TeamRunRecord(
        run_id="team",
        team_id="squad",
        session_id=_SESSION,
        status=RunStatus.paused,
        created_at=100,
        requirements=[RunRequirement(tool_execution=tool, id="req", member_agent_id="code", member_run_id="member")],
        member_responses=[embedded],
    )
    _insert_run(database, team_run, run_index=0)
    _insert_run(database, _run("after"), run_index=1)
    store = _converted(database)
    assert sorted(_seqs(database), key=_seqs(database).get) == ["team", "member", "after"]
    resumed = store.get_run("team")
    assert isinstance(resumed, TeamRunRecord)
    assert [run.to_dict() for run in resumed.member_responses] == [embedded.to_dict()]


def test_a_fold_whose_run_has_no_row_gets_a_cancelled_row_after_its_summary(tmp_path: Path) -> None:
    """The fold's run is stored cancelled after its summary, with the fold's metadata, messages, and members folded."""
    database = _legacy("synthetic__orphan_segment", tmp_path)

    store = _converted(database)

    orphan = store.get_run("orphan")
    assert orphan is not None
    assert (orphan.status, orphan.messages, orphan.tools) == (RunStatus.cancelled, [], None)
    assert orphan.metadata == {
        constants.MATRIX_EVENT_ID_METADATA_KEY: "$orphan",
        constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$ctx-orphan"],
    }
    # The fold's row sorts before every run, so its run does too.
    assert sorted(_seqs(database), key=_seqs(database).get) == ["orphan", "orphan-member", "r0"]
    [(row_id,)] = _query(database, 'SELECT id FROM "code_sessions_log_summaries"')
    assert _message_rows(database, "orphan") == [("orphan.b0", row_id), ("orphan.t0", row_id)]
    assert _query(
        database,
        'SELECT parent_run_id, folded_by FROM "code_sessions_log_runs" WHERE run_id = ?',
        ("orphan-member",),
    ) == [
        ("orphan", row_id),
    ]
    assert folded_tool_call_ids(store, session_id="session", run_id="orphan") == {"orphan.c0"}
    assert _loaded_ids(store, "session") == ["orphan", "orphan-member", "r0"]


def test_a_fold_run_without_a_row_takes_its_latest_folds_place_and_keeps_its_members_rows(tmp_path: Path) -> None:
    """Of several folds of a run without a row, the latest gives its row's metadata, time, and place; members keep theirs."""
    database = _agno_database(tmp_path)
    member = _run("member", parent_run_id="gone", content="as the member stored it")
    _insert_run(database, member, run_index=0, created_at=50)
    _insert_run(database, _run("r0"), run_index=0)
    _insert_run(database, _run("r1"), run_index=1)
    copy = _run("member", parent_run_id="gone", content="as the fold saw it")
    first = {constants.MATRIX_EVENT_ID_METADATA_KEY: "$gone"}
    latest = {**first, constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$read-later"]}
    _insert_summary(database, 1, -1, segment_run_id="gone", segment=_segment(metadata=first, members=(copy,)))
    _insert_summary(database, 2, 0, segment_run_id="gone", segment=_segment(metadata=latest), created_at=300)

    store = _converted(database)

    gone = store.get_run("gone")
    assert gone is not None
    assert (gone.metadata, gone.created_at) == (latest, 300)
    assert sorted(_seqs(database), key=_seqs(database).get) == ["member", "r0", "gone", "r1"]
    stored_member = store.get_run("member")
    assert stored_member is not None
    assert stored_member.content == "as the member stored it"
    assert _query(database, "SELECT folded_by FROM \"code_sessions_log_runs\" WHERE run_id = 'member'") == [(1,)]


@pytest.mark.parametrize("field", ["agent_id", "user_id"])
def test_a_lone_surrogate_in_a_runs_text_field_converts_as_a_replacement_character(tmp_path: Path, field: str) -> None:
    """A run row's or fold member's text field worker code wrote with an escaped lone surrogate reads as U+FFFD."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0, run_data=json.dumps({**_run("r0").to_dict(), field: "x\ud800"}))
    segment = _segment(members=(_run("gone.m", parent_run_id="gone"),))
    segment["member_responses"][0][field] = "x\ud800"
    _insert_summary(database, 1, 0, segment_run_id="gone", segment=segment)

    store = _converted(database)

    for run_id in ("r0", "gone.m"):
        run = store.get_run(run_id)
        assert run is not None
        assert run.to_dict()[field] == "x\ufffd"


def test_a_run_whose_metadata_is_not_an_object_converts(tmp_path: Path) -> None:
    """Metadata worker code stored as something other than an object neither stops the conversion nor changes."""
    database = _agno_database(tmp_path)
    run = _run("odd").to_dict() | {"metadata": ["not", "an", "object"]}
    _insert_run(database, run, run_index=0)

    store = _converted(database)

    assert store.get_run("odd").metadata == ["not", "an", "object"]  # type: ignore[union-attr]


def test_fold_runs_without_a_row_sort_by_their_latest_fold(tmp_path: Path) -> None:
    """Runs without a row go by their latest fold's place, even when one's first fold comes before the other's."""
    database = _agno_database(tmp_path)
    for index in range(3):
        _insert_run(database, _run(f"r{index}"), run_index=index)
    _insert_summary(database, 1, -1, segment_run_id="a", segment=_segment())
    _insert_summary(database, 2, 0, segment_run_id="b", segment=_segment())
    _insert_summary(database, 3, 1, segment_run_id="a", segment=_segment())

    _converted(database)

    assert sorted(_seqs(database), key=_seqs(database).get) == ["r0", "b", "r1", "a", "r2"]
    assert _query(database, 'SELECT id, through FROM "code_sessions_log_summaries" ORDER BY id') == [
        (1, 0),
        (2, 1),
        (3, 3),
    ]


def test_rows_worker_code_wrote_in_odd_shapes_convert_alone(tmp_path: Path) -> None:
    """Segments, segment entries, and paused team rows of any shape convert as far as they read; other sessions convert."""
    database = _agno_database(tmp_path, ("odd", "agent", "code"), ("healthy", "agent", "code"))
    _insert_run(database, _run("fine"), run_index=0, session_id="healthy")
    _insert_run(database, _run("live", status=RunStatus.paused), run_index=0, session_id="odd")
    _insert_run(database, _run("live-member", parent_run_id="live"), run_index=1, session_id="odd")
    team = TeamRunRecord(
        run_id="team",
        team_id="squad",
        session_id="odd",
        status=RunStatus.paused,
        created_at=100,
        member_responses=[_run("emb-replaced"), _run("emb-created")],
    ).to_dict()
    for member in team["member_responses"]:
        member.pop("parent_run_id", None)
    _insert_run(database, team, run_index=2, session_id="odd")
    # The embedded copy replaces its member's own row, even one that does not decode.
    _insert_run(database, _run("emb-replaced"), run_index=3, run_data="{not json", session_id="odd")
    paused_team = {"run_id": "team-bad", "team_id": "squad", "status": "PAUSED", "member_responses": []}
    _insert_run(database, paused_team, run_index=4, run_data="{not json", session_id="odd")
    odd = {"session_id": "odd", "segment_run_id": "live"}
    _insert_summary(database, 1, -1, raw_segment="{not json", **odd)
    _insert_summary(database, 2, -1, raw_segment="[1, 2]", **odd)
    folded = Message(role="assistant", content="folded", id="live.f1", created_at=100).to_dict()
    entries = {
        "metadata": {},
        "messages": [5, {"id": "live.no-role", "content": "x"}, folded],
        "tools": [7, {"tool_call_id": 9}, {"tool_call_id": "ok-call", "tool_name": "probe"}],
        "member_responses": ["m", {"run_id": 5}, {"run_id": "live-member"}],
    }
    _insert_summary(database, 3, -1, segment=entries, **odd)
    members = {"member_responses": [{"run_id": 7}, {"run_id": "bad-member", "status": "BOGUS"}]}
    _insert_summary(database, 4, -1, segment=members, session_id="odd", segment_run_id="gone")

    store = _converted(database)

    assert set(_seqs(database)) == {"fine", "live", "live-member", "team", "emb-replaced", "emb-created", "gone"}
    assert set(_message_rows(database, "live")) == {("live.u", None), ("live.f1", 3), ("live:3:ok-call", 3)}
    assert _query(database, "SELECT folded_by FROM \"code_sessions_log_runs\" WHERE run_id = 'live-member'") == [(3,)]
    parents = dict(_query(database, 'SELECT run_id, parent_run_id FROM "code_sessions_log_runs"'))
    assert (parents["emb-replaced"], parents["emb-created"]) == ("team", "team")
    assert _loaded_ids(store, "healthy") == ["fine"]


_ODD_VALUES = {
    "nan": math.nan,
    "infinity": math.inf,
    "beyond_64_bits": 2**70,
    "text": "yesterday",
    "object": {"at": 1},
    "list": [1],
    "bool": True,
}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(("created_at", value) for value in _ODD_VALUES.values()),
        *(("run_id", value) for name, value in _ODD_VALUES.items() if name != "text"),
    ],
    ids=[
        *(f"created_at-{name}" for name in _ODD_VALUES),
        *(f"run_id-{name}" for name in _ODD_VALUES if name != "text"),
    ],
)
def test_members_with_unusable_values_convert_alone(tmp_path: Path, field: str, value: object) -> None:
    """A member a paused team run embeds, or a fold stores, with a run id or a time Agno never wrote converts alone.

    A member whose run id is not text is left out; one whose time is not a timestamp takes its team run row's or its
    fold's, and every other run converts.
    """
    database = _agno_database(tmp_path, (_SESSION, "team", "squad"))
    embedded = _run("embedded", status=RunStatus.paused, parent_run_id="team").to_dict() | {field: value}
    team = TeamRunRecord(run_id="team", team_id="squad", session_id=_SESSION, status=RunStatus.paused).to_dict()
    _insert_run(database, team | {"member_responses": [embedded]}, run_index=0, created_at=300)
    folded = _run("folded", parent_run_id="gone").to_dict() | {field: value}
    _insert_summary(database, 1, 0, segment_run_id="gone", segment={"metadata": {}, "member_responses": [folded]})

    store = _converted(database)

    seqs, created = _seqs(database), dict(_query(database, 'SELECT run_id, created_at FROM "code_sessions_log_runs"'))
    assert set(seqs) == {"team", "gone"} | ({"embedded", "folded"} if field == "created_at" else set())
    if field == "created_at":
        assert (created["embedded"], created["folded"]) == (300, 200)
        assert (store.get_run("embedded").created_at, store.get_run("folded").created_at) == (300, 200)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "calls",
    [math.nan, 5, "call", {"id": "call"}, [1], ["call"], [{"id": {"nested": 1}}], [{"id": ["call"]}]],
    ids=["nan", "number", "text", "object", "list-of-number", "list-of-text", "object-id", "list-id"],
)
def test_segment_messages_name_only_text_call_ids(tmp_path: Path, calls: object) -> None:
    """Tool calls and result ids of any shape worker code wrote name no call unless they are text."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("live", status=RunStatus.paused), run_index=0)
    odd = {"id": "live.odd", "role": "assistant", "content": "x", "tool_calls": calls, "tool_call_id": {"id": "c0"}}
    named = Message(role="tool", content="result", id="live.t0", tool_call_id="c0", created_at=100).to_dict()
    segment = {
        "metadata": {},
        "messages": [odd, named],
        "tools": [{"tool_call_id": "c0", "tool_name": "probe"}, {"tool_call_id": "call", "tool_name": "probe"}],
    }
    _insert_summary(database, 1, -1, segment_run_id="live", segment=segment)

    _converted(database)

    rows = _message_rows(database, "live")
    assert ("live:1:call", 1) in rows
    assert ("live:1:c0", 1) not in rows
    assert {("live.odd", 1), ("live.t0", 1)} <= set(rows)


_FUNCTION = {"name": "probe", "arguments": "{}"}
# Each odd ``tool_calls`` value, the calls its folded copy keeps, and the calls the run's own message keeps once the
# legacy repair drops each call without a function, which cannot replay.
_ODD_CALLS = {
    "nan": (math.nan, None, None),
    "number": (5, None, None),
    "text": ("call", None, None),
    "object": ({"id": "call"}, None, None),
    "list-of-non-objects": ([1, "call"], None, None),
    "object-id": ([{"id": {"nested": 1}}], [{"id": None}], None),
    "list-id": ([{"id": ["call"], "type": "function"}], [{"id": None, "type": "function"}], None),
    "function-object-id": (
        [{"id": {"nested": 1}, "type": "function", "function": _FUNCTION}],
        [{"id": None, "type": "function", "function": _FUNCTION}],
        [{"id": None, "type": "function", "function": _FUNCTION}],
    ),
    "function-list-id": (
        [{"id": ["call"], "type": "function", "function": _FUNCTION}],
        [{"id": None, "type": "function", "function": _FUNCTION}],
        [{"id": None, "type": "function", "function": _FUNCTION}],
    ),
}


@pytest.mark.parametrize("odd_id", [math.nan, 5, {"id": "c9"}, ["c9"]], ids=["nan", "number", "object", "list"])
@pytest.mark.parametrize(("odd", "usable", "kept"), list(_ODD_CALLS.values()), ids=list(_ODD_CALLS))
def test_converted_call_ids_are_text_or_none_so_folded_calls_read(
    tmp_path: Path,
    odd: object,
    usable: object,
    kept: object,
    odd_id: object,
) -> None:
    """Call ids worker code wrote in other shapes convert as text or none in run, folded, and member rows alike.

    So the run's folded calls read; tool calls keep only their objects, and well-formed messages are stored as written.
    The run's own messages are then repaired like any legacy call: a call without a function leaves its message, and
    one with a function stays, its id None, so a resume's tool-call window can hash it. The folded copy, a message the
    run's row does not hold, is stored as the fold took it.
    """
    database = _agno_database(tmp_path)
    odd_message = {"id": "live.odd", "role": "assistant", "content": "x", "tool_calls": odd, "tool_call_id": odd_id}
    live = _run("live", status=RunStatus.paused).to_dict()
    live["messages"].append(odd_message)
    _insert_run(database, live, run_index=0)
    tools = [{"tool_call_id": odd_id, "tool_name": "probe"}, {"tool_call_id": "m.c0", "tool_name": "probe"}]
    member = _run("member", parent_run_id="live").to_dict() | {"tools": tools}
    _insert_run(database, member, run_index=1)
    result = Message(role="tool", content="result", id="live.t0", tool_call_id="c0", created_at=100)
    segment = {
        "metadata": {},
        "messages": [{**odd_message, "id": "live.f0"}, result.to_dict()],
        "member_responses": [{"run_id": "member"}],
    }
    _insert_summary(database, 1, -1, segment_run_id="live", segment=segment)

    store = _converted(database)

    assert folded_tool_call_ids(store, session_id=_SESSION, run_id="live") == {"c0", "m.c0"}
    stored = dict(_query(database, "SELECT message_id, data FROM \"code_sessions_log_messages\" WHERE run_id = 'live'"))
    run_message, folded = (json.loads(stored[message_id]) for message_id in ("live.odd", "live.f0"))
    assert (run_message.get("tool_calls"), run_message.get("tool_call_id")) == (kept, None)
    assert (folded.get("tool_calls"), folded.get("tool_call_id")) == (usable, None)
    assert stored["live.t0"] == json.dumps(result.to_dict())
    [(member_tools,)] = _query(database, "SELECT tools FROM \"code_sessions_log_runs\" WHERE run_id = 'member'")
    assert [tool["tool_call_id"] for tool in json.loads(member_tools)] == [None, "m.c0"]


@pytest.mark.parametrize("position", ["later", b"\x01"], ids=["text", "blob"])
def test_a_summary_row_whose_position_is_not_a_number_covers_every_numbered_run_as_a_load_read_it(
    tmp_path: Path,
    position: object,
) -> None:
    """SQLite sorts text and blobs after every number, so such a row covered every numbered run, and never one without."""
    database = _agno_database(tmp_path)
    for index in range(2):
        _insert_run(database, _run(f"r{index}"), run_index=index)
    _insert_run(database, _run("unplaced"), run_index=None)
    _insert_summary(database, 1, 0)
    _insert_summary(database, 2, 0)
    _store_raw(database, "code_sessions_summaries", "after_run_index", position, id=2)

    store = _converted(database)

    assert _query(database, 'SELECT id, through FROM "code_sessions_log_summaries" ORDER BY id') == [(1, 1), (2, 2)]
    assert _loaded_ids(store) == ["unplaced"]


@pytest.mark.parametrize("position", ["later", b"\x01"], ids=["text", "blob"])
def test_a_run_whose_position_is_not_a_number_goes_last_as_a_load_read_it(tmp_path: Path, position: object) -> None:
    """SQLite sorts text and blobs after every number, so no summary row covered such a run; it converts last.

    It goes after a fold's run without a row too, and the database converts.
    """
    database = _agno_database(tmp_path)
    _insert_run(database, _run("odd"), run_index=0)
    _insert_run(database, _run("r1"), run_index=1)
    _store_raw(database, "code_sessions_runs", "run_index", position, run_id="odd")
    _insert_summary(database, 1, 1)
    _insert_summary(database, 2, 1, segment_run_id="gone", segment=_segment())

    store = _converted(database)

    assert sorted(_seqs(database), key=_seqs(database).get) == ["r1", "gone", "odd"]
    assert _query(database, 'SELECT id, through FROM "code_sessions_log_summaries" ORDER BY id') == [(1, 1), (2, 1)]
    assert _loaded_ids(store) == ["gone", "odd"]


def test_a_run_row_whose_id_is_not_text_is_left_out(tmp_path: Path) -> None:
    """No read could address a run by an id that is not text, so its row stays out, a paused team's with its embeds too.

    A member keeps its own row, as a member of a team row that does not decode does, and the session reads.
    """
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    member = _run("member", parent_run_id="team", status=RunStatus.paused)
    team = TeamRunRecord(run_id="team", team_id="squad", session_id=_SESSION, status=RunStatus.paused)
    embedded = _run("member", parent_run_id="team", status=RunStatus.completed)
    _insert_run(database, team.to_dict() | {"member_responses": [embedded.to_dict()]}, run_index=1)
    _insert_run(database, member, run_index=2)
    _store_raw(database, "code_sessions_runs", "run_id", b"team", run_id="team")

    store = _converted(database)

    assert set(_seqs(database)) == {"r0", "member"}
    assert _query(database, "SELECT status FROM \"code_sessions_log_runs\" WHERE run_id = 'member'") == [("PAUSED",)]
    assert [run.run_id for run in store.list_runs(_SESSION)] == ["r0", "member"]
    assert _loaded_ids(store) == ["r0"]


def test_a_summary_row_whose_scope_key_is_not_text_is_left_out(tmp_path: Path) -> None:
    """No read names a scope that is not text, so such a row stays out, a fold's without a run row too."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    _insert_summary(database, 1, 0)
    _insert_summary(
        database,
        2,
        0,
        segment_run_id="gone",
        segment=_segment(members=(_run("gone.m", parent_run_id="gone"),)),
    )
    for row_id in (1, 2):
        _store_raw(database, "code_sessions_summaries", "scope_key", b"agent:code", id=row_id)

    store = _converted(database)

    assert _query(database, 'SELECT count(*) FROM "code_sessions_log_summaries"') == [(0,)]
    assert set(_seqs(database)) == {"r0"}
    assert _loaded_ids(store) == ["r0"]


@pytest.mark.parametrize("fold_run_id", ["r1", "gone"], ids=["run-with-a-row", "run-without-a-row"])
def test_a_fold_whose_run_id_is_not_text_names_no_run(tmp_path: Path, fold_run_id: str) -> None:
    """A fold row whose run id is not text converts as a summary of no run, so a later retirement removes nothing."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    _insert_run(database, _run("r1"), run_index=1)
    _insert_summary(database, 1, 0)
    _insert_summary(database, 2, 1, segment_run_id=fold_run_id, segment=_segment())
    _store_raw(database, "code_sessions_summaries", "segment_run_id", fold_run_id.encode(), id=2)

    store = _converted(database)

    assert _query(database, 'SELECT id, through, fold_run_id FROM "code_sessions_log_summaries"') == [
        (1, 1, None),
        (2, 2, None),
    ]
    assert [run.run_id for run in store.list_runs(_SESSION)] == ["r0", "r1"]
    session = store.load_session(_SESSION, "agent")
    assert session is not None
    retire(store, session=session, scope_key="agent:code")
    assert [run.run_id for run in store.list_runs(_SESSION)] == ["r0", "r1"]


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("{not json", []),
        ('{"$e": "$e"}', []),
        ("5", []),
        ('[1, "$e", {"id": "$f"}, ["$g"]]', ["$e"]),
        ('["$e\\ud800"]', ["$e\ufffd"]),
    ],
    ids=["not-json", "object", "number", "mixed-list", "lone-surrogate"],
)
def test_summary_event_ids_keep_only_their_text_entries(tmp_path: Path, raw: str, kept: list[str]) -> None:
    """Event ids worker code wrote as anything but a JSON array of text keep only their text entries, so the scope reads.

    A redaction's and a seen-id read's event sets, the rows a cut reads, and a reply linked to a covered run all work.
    """
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    _insert_summary(database, 1, 0)
    for column in ("event_ids", "seen_event_ids"):
        _store_raw(database, "code_sessions_summaries", column, raw, id=1)

    store = _converted(database)

    assert _query(database, 'SELECT event_ids, seen_event_ids FROM "code_sessions_log_summaries"') == [
        (json.dumps(kept), json.dumps(kept)),
    ]
    for kind in ("all", "seen"):
        assert history_event_ids(store, session_id=_SESSION, scope_key="agent:code", kind=kind) == set(kept)
    assert legacy_event_ids(store, session_id=_SESSION, scope_key="agent:code") is None
    assert store.patch_run_metadata(_SESSION, "r0", {constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply"})
    assert "$reply" in history_event_ids(store, session_id=_SESSION, scope_key="agent:code", kind="all")


@pytest.mark.parametrize(
    "created_at",
    ["yesterday", math.inf, 1.5, 10**12],
    ids=["text", "infinity", "fraction", "after-year-9999"],
)
def test_a_summary_rows_unusable_creation_time_becomes_the_conversions(tmp_path: Path, created_at: object) -> None:
    """A summary row's time that is not whole seconds a datetime holds becomes the conversion's, so its session loads."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    _insert_summary(database, 1, 0)
    _store_raw(database, "code_sessions_summaries", "created_at", created_at, id=1)
    started = int(time.time())

    store = _converted(database)

    [(stored,)] = _query(database, 'SELECT created_at FROM "code_sessions_log_summaries"')
    assert started <= stored <= time.time()
    assert _loaded_ids(store) == []


def test_workflow_rows_and_rows_that_do_not_decode_are_left_out(tmp_path: Path) -> None:
    """Neither a workflow row nor a row that does not decode is converted; the latter is logged by id."""
    workflow = _legacy("synthetic__workflow_rows", tmp_path / "workflow")
    undecodable = _legacy("synthetic__undecodable_run_data", tmp_path / "undecodable")

    with capture_logs() as logs:
        _converted(workflow)
        store = _converted(undecodable)

    assert set(_seqs(workflow)) == {"r0"}
    assert set(_seqs(undecodable)) == {"r0"}
    assert [run.run_id for run in store.list_runs("session")] == ["r0"]
    assert _skipped(logs, "conversation_run_left_out") == [str(undecodable)]
    assert [entry["run_id"] for entry in logs if entry["event"] == "conversation_run_left_out"] == ["bad"]


def test_messages_convert_under_the_saves_rule(tmp_path: Path) -> None:
    """History copies are dropped, and system messages unless the run is paused."""
    database = _agno_database(tmp_path)
    system = Message(role="system", content="prompt", id="system", created_at=100)
    copy = Message(role="user", content="earlier", id="copy", from_history=True, created_at=100)
    for run_id, status, run_index in (("done", RunStatus.completed, 0), ("waiting", RunStatus.paused, 1)):
        run = _run(run_id, status=status)
        run.messages = [system, copy, *run.messages]
        run.messages = [
            Message.from_dict(message.to_dict() | {"id": f"{run_id}.{message.id}"}) for message in run.messages
        ]
        _insert_run(database, run, run_index=run_index)

    store = _converted(database)

    assert [message.id for message in store.get_run("done").messages] == ["done.done.u"]  # type: ignore[union-attr]
    assert [message.id for message in store.get_run("waiting").messages] == ["waiting.system", "waiting.waiting.u"]  # type: ignore[union-attr]


def test_converted_runs_drop_their_stored_tool_schemas(tmp_path: Path) -> None:
    """Older releases stored every tool schema in run metadata, a paused team run's embedded members included."""
    database = _agno_database(tmp_path, (_SESSION, "team", "squad"))
    tool = ToolExecution(tool_call_id="call", tool_name="shell", requires_confirmation=True)
    member = _run("m1", status=RunStatus.paused, parent_run_id="t1")
    member.metadata = {"tools_schema": [{}], "room_id": "!r"}
    team = TeamRunRecord(
        run_id="t1",
        team_id="squad",
        session_id=_SESSION,
        status=RunStatus.paused,
        created_at=100,
        metadata={"tools_schema": [{"name": "x"}], "room_id": "!r"},
        requirements=[RunRequirement(tool_execution=tool, id="req", member_agent_id="code", member_run_id="m1")],
        member_responses=[member],
    )
    _insert_run(database, team, run_index=0)

    store = _converted(database)

    paused, stored_member = store.get_run("t1"), store.get_run("m1")
    assert isinstance(paused, TeamRunRecord)
    assert paused.metadata == {"room_id": "!r"}
    assert [run.metadata for run in paused.member_responses] == [{"room_id": "!r"}]
    assert stored_member is not None
    assert stored_member.metadata == {"room_id": "!r"}


def test_converted_runs_keep_only_the_keys_records_model(tmp_path: Path) -> None:
    """A run Agno wrote converts without the keys the owned records drop, and keeps every key they read."""
    stored = json.loads((_AGNO_PARITY / "codecs.json").read_text(encoding="utf-8"))["paused_agent_run"]
    assert {"input", "session_state", "content_type"} <= set(stored)
    assert any(DROPPED_TOOL_EXECUTION_KEYS & set(tool) for tool in stored["tools"])
    database = _agno_database(tmp_path, (stored["session_id"], "agent", stored["agent_id"]))
    _insert_run(database, stored, run_index=0, session_id=stored["session_id"])

    store = _converted(database)

    [(record, tools, requirements)] = _query(
        database,
        'SELECT record, tools, requirements FROM "code_sessions_log_runs"',
    )
    assert DROPPED_RUN_KEYS.isdisjoint(json.loads(record))
    executions = [*json.loads(tools), *(item["tool_execution"] for item in json.loads(requirements))]
    assert all(DROPPED_TOOL_EXECUTION_KEYS.isdisjoint(execution) for execution in executions)
    converted = store.get_run(stored["run_id"])
    assert converted is not None
    expected = expected_run_dict(stored)
    # The run holds a history copy, which no converted run keeps.
    expected["messages"] = [message for message in expected["messages"] if not message["from_history"]]
    assert converted.to_dict() == expected


def test_a_repeated_message_id_gets_a_derived_one(tmp_path: Path) -> None:
    """A message id a run holds twice is derived from the run and the place, unique, and the same in every conversion."""
    for copy in ("first", "second"):
        database = _agno_database(tmp_path / copy)
        run = _run("r")
        run.messages = [
            Message(role="user", content="one", id="a", created_at=100),
            Message(role="assistant", content="two", id="a", created_at=100),
            Message(role="assistant", content="three", id="r:1", created_at=100),
        ]
        _insert_run(database, run, run_index=0)
        store = _converted(database)
        stored = store.get_run("r")
        assert stored is not None
        assert [(message.id, message.content) for message in stored.messages] == [
            ("a", "one"),
            ("r:1+", "two"),
            ("r:1", "three"),
        ]
    assert _dump(tmp_path / "first" / "sessions" / "code.db") == _dump(tmp_path / "second" / "sessions" / "code.db")


def _child(**changes: object) -> dict[str, Any]:
    persona = SubagentPersona(source_kind="inline", source_name="writer", system_prompt="Write.", tools=("shell",))
    child = DelegationChild(
        delegation_id="d1",
        parent_tool_call_id="call",
        caller_agent_name="code",
        child_agent_name="helper",
        task="task",
        session_id="child-session",
        run_id="child-run",
        model_name="model",
        depth=1,
        execution_identity={},
        persona=persona,
    )
    data = json.loads(json.dumps(asdict(child)))
    for key, value in changes.items():
        if key.startswith("persona."):
            data["persona"].pop(key.removeprefix("persona."))
        elif value is None:
            data.pop(key)
    return data


_LEGACY_CHILDREN = {
    "without_agent_mode": _child(agent_mode=None),
    "without_requested_model_name": _child(requested_model_name=None),
    "without_persona": _child(persona=None),
    "persona_without_skills": _child(**{"persona.skills": None}),
    "persona_without_knowledge": _child(**{"persona.knowledge": None}),
}


@pytest.mark.parametrize("place", ["children", "pending_tool_sources"])
@pytest.mark.parametrize("shape", sorted(_LEGACY_CHILDREN))
def test_delegation_snapshots_are_stored_whole(tmp_path: Path, shape: str, place: str) -> None:
    """A delegation snapshot an older release wrote is stored as today's reader reads it, with every field present."""
    child = _LEGACY_CHILDREN[shape]
    snapshot: dict[str, Any] = (
        {"children": [child]}
        if place == "children"
        else {"pending_tool_sources": {"call": {"child": child, "tool_call_id": "call"}}}
    )
    database = _agno_database(tmp_path)
    run = _run("parent", status=RunStatus.paused)
    run.metadata = {**(run.metadata or {}), DELEGATION_STATE_KEY: snapshot}
    _insert_run(database, run, run_index=0)

    store = _converted(database)

    stored = store.get_run("parent")
    assert stored is not None
    expected = json.loads(json.dumps(DelegationState.from_metadata(run.metadata).to_dict()))
    assert stored.metadata[DELEGATION_STATE_KEY] == expected  # type: ignore[index]
    [stored_child] = expected["children"] or [source["child"] for source in expected["pending_tool_sources"].values()]
    assert set(stored_child) == set(DelegationChild.__dataclass_fields__)
    if stored_child["persona"] is not None:
        assert set(stored_child["persona"]) == set(SubagentPersona.__dataclass_fields__)
    assert (
        DelegationState.from_metadata(stored.metadata).to_dict()
        == DelegationState.from_metadata(run.metadata).to_dict()
    )


def test_a_delegation_snapshot_the_reader_refuses_stays_as_stored(tmp_path: Path) -> None:
    """A snapshot today's reader cannot read is kept byte for byte and logged by run id."""
    database = _agno_database(tmp_path)
    run = _run("parent")
    run.metadata = {DELEGATION_STATE_KEY: {"children": [{"unknown": 1}]}}
    _insert_run(database, run, run_index=0)

    with capture_logs() as logs:
        store = _converted(database)

    assert store.get_run("parent").metadata == run.metadata  # type: ignore[union-attr]
    assert [entry["run_id"] for entry in logs if entry["event"] == "conversation_delegation_snapshot_kept"] == [
        "parent",
    ]


def test_summary_rows_keep_their_order_against_the_runs(tmp_path: Path) -> None:
    """``through`` is the largest ``seq`` sorted at or before a row's position, so coverage and every order stay."""
    database = _agno_database(tmp_path)
    for index in range(4):
        _insert_run(database, _run(f"r{index}"), run_index=index)
    _insert_run(database, _run("m1", parent_run_id="r1"), run_index=2, created_at=50)
    # A load read a run without a position first and never behind a summary row.
    _insert_run(database, _run("unplaced"), run_index=None)
    _insert_summary(database, 1, -1, legacy=1)
    _insert_summary(database, 2, 1)
    _insert_summary(database, 3, 1, summary=None)
    _insert_summary(database, 4, 2)

    store = _converted(database)

    seqs = _seqs(database)
    assert [seqs[run_id] for run_id in ("r0", "r1", "m1", "r2", "r3", "unplaced")] == [1, 2, 3, 4, 5, 6]
    assert _query(database, 'SELECT id, through FROM "code_sessions_log_summaries" ORDER BY id') == [
        (1, 0),
        (2, 2),
        (3, 2),
        (4, 4),
    ]
    assert _loaded_ids(store) == ["r3", "unplaced"]


def test_summary_ids_are_never_handed_out_again(tmp_path: Path) -> None:
    """The converted ids set the summary id sequence, so a summary appended later never takes a deleted one's id."""
    database = _agno_database(tmp_path)
    _insert_summary(database, 3, -1)
    _insert_summary(database, 7, -1)

    _converted(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('DELETE FROM "code_sessions_log_summaries" WHERE id = 7')
        connection.execute(
            'INSERT INTO "code_sessions_log_summaries" (session_id, scope_key, through, created_at) '
            "VALUES (?, 'agent:code', 0, 1)",
            (_SESSION,),
        )

    assert _query(database, 'SELECT id FROM "code_sessions_log_summaries" ORDER BY id') == [(3,), (8,)]


def test_a_fold_row_keeps_its_runs_provenance(tmp_path: Path) -> None:
    """A fold's row names its run and keeps what the run answered and read at the fold; other rows keep none."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("live", status=RunStatus.paused), run_index=1)
    metadata = {
        constants.MATRIX_EVENT_ID_METADATA_KEY: "$asked",
        constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$read"],
        constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply",
    }
    _insert_summary(database, 1, -1)
    _insert_summary(database, 2, -1, segment_run_id="live", segment=_segment(metadata=metadata))

    _converted(database)

    assert _query(
        database,
        'SELECT id, fold_run_id, fold_event_ids, fold_seen_event_ids FROM "code_sessions_log_summaries" ORDER BY id',
    ) == [(1, None, None, None), (2, "live", '["$asked"]', '["$read", "$reply"]')]


def test_a_segment_flags_what_its_fold_took(tmp_path: Path) -> None:
    """A folded message the run holds keeps its data and gains the flag, one it lacks is stored folded, and so on."""
    database = _agno_database(tmp_path)
    held = Message(role="assistant", content="as the run stored it", id="live.b0", created_at=100)
    live = _run("live", status=RunStatus.paused)
    live.messages = [*live.messages, held]
    _insert_run(database, _run("member", parent_run_id="live"), run_index=0)
    _insert_run(database, live, run_index=1)
    copy = Message(role="assistant", content="as the fold saw it", id="live.b0", created_at=100)
    missing = Message(role="tool", content="result", id="live.t0", tool_call_id="c0", created_at=100)
    _insert_summary(database, 1, -1, segment_run_id="live", segment=_segment(copy, missing, members=(_run("member"),)))
    _insert_summary(database, 2, -1, segment_run_id="live", segment=_segment(copy, members=(_run("member"),)))

    store = _converted(database)

    assert _message_rows(database, "live") == [("live.u", None), ("live.b0", 1), ("live.t0", 1)]
    assert _query(database, "SELECT data FROM \"code_sessions_log_messages\" WHERE message_id = 'live.b0'") == [
        (json.dumps(held.to_dict()),),
    ]
    assert _query(database, "SELECT folded_by FROM \"code_sessions_log_runs\" WHERE run_id = 'member'") == [(1,)]
    assert [message.id for message in store.get_run("live").messages] == ["live.u"]  # type: ignore[union-attr]
    assert folded_tool_call_ids(store, session_id=_SESSION, run_id="live") == {"c0"}


def test_a_folded_message_the_run_row_holds_is_stored_repaired(tmp_path: Path) -> None:
    """A segment message the run's row holds keeps the row's repaired copy; one the repair removed keeps the fold's."""
    database = _agno_database(tmp_path)
    bare = {"id": "call-0", "type": "function", "function": {"name": "list_rooms"}}
    call = Message(role="assistant", id="live.a0", tool_calls=[bare, {"id": "call_0"}], created_at=100)
    orphan = Message(role="tool", content="orphan", id="live.t0", tool_call_id="call_0", created_at=100)
    live = _run("live", status=RunStatus.paused)
    live.messages = [*live.messages, call, orphan]
    _insert_run(database, live, run_index=0)
    _insert_summary(database, 1, -1, segment_run_id="live", segment=_segment(call, orphan))

    _converted(database)

    stored = dict(
        _query(database, 'SELECT message_id, data FROM "code_sessions_log_messages" WHERE run_id = ?', ("live",)),
    )
    assert json.loads(stored["live.a0"])["tool_calls"] == [
        {**bare, "function": {"name": "list_rooms", "arguments": "{}"}},
    ]
    assert stored["live.t0"] == json.dumps(orphan.to_dict())
    assert {message_id for message_id, folded_by in _message_rows(database, "live") if folded_by} == {
        "live.a0",
        "live.t0",
    }


def test_a_segment_whose_run_is_not_converted_flags_nothing(tmp_path: Path) -> None:
    """A fold whose run's row does not decode, or belongs to another session, leaves no folded rows behind."""
    database = _agno_database(tmp_path, (_SESSION, "agent", "code"), ("other", "agent", "code"))
    _insert_run(database, _run("bad"), run_index=0, run_data="{not json")
    _insert_run(database, _run("elsewhere"), run_index=0, session_id="other")
    copy = Message(role="assistant", content="folded", id="folded", created_at=100)
    _insert_summary(database, 1, -1, segment_run_id="bad", segment=_segment(copy))
    _insert_summary(database, 2, -1, segment_run_id="elsewhere", segment=_segment(copy))

    store = _converted(database)

    assert _query(database, 'SELECT count(*) FROM "code_sessions_log_messages" WHERE folded_by IS NOT NULL') == [(0,)]
    assert store.get_run("bad") is None


def test_a_segment_tool_no_folded_message_names_stays_folded(tmp_path: Path) -> None:
    """A segment's tool execution that no folded message names is kept as a folded tool result of its call."""
    database = _legacy("synthetic__segment_tool_without_message", tmp_path)

    store = _converted(database)

    assert folded_tool_call_ids(store, session_id="session", run_id="live") == {"live.c0", "execution-only"}
    [(data,)] = _query(
        database,
        "SELECT data FROM \"code_sessions_log_messages\" WHERE folded_by IS NOT NULL AND data LIKE '%execution-only%'",
    )
    assert (json.loads(data)["role"], json.loads(data)["tool_name"]) == ("tool", "probe")


def _store_text_bytes(database: Path, sql: str, *parameters: object) -> None:
    """Run ``sql``, whose ``CAST(? AS TEXT)`` stores bytes as text without checking them, as worker code could."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(sql, parameters)


def test_a_run_row_whose_data_is_not_utf8_converts_with_replacement_characters(tmp_path: Path) -> None:
    """SQLite stores any bytes as text; a run's JSON that is not UTF-8 reads with U+FFFD, and the session converts."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0", content="answer r0"), run_index=0)
    _insert_run(database, _run("r1"), run_index=1)
    stored = json.dumps(_run("r0", content="answer r0").to_dict()).encode().replace(b'"answer r0"', b'"answer \x80"')
    _store_text_bytes(database, "UPDATE code_sessions_runs SET run_data = CAST(? AS TEXT) WHERE run_id = 'r0'", stored)

    store = _converted(database)

    run = store.get_run("r0")
    assert run is not None
    assert run.content == "answer \ufffd"
    assert _loaded_ids(store) == ["r0", "r1"]


@pytest.mark.parametrize(
    ("column", "stored", "read"),
    [
        ("metadata", b'{"note": "x\x80"}', lambda session: session.metadata == {"note": "x\ufffd"}),
        (
            "session_data",
            b'{"session_state": {"note": "x\xff"}}',
            lambda session: session.session_state == {"note": "x\ufffd"},
        ),
        ("agent_id", b"code\xfe", lambda session: session.owner_id == "code\ufffd"),
    ],
    ids=["metadata", "session-data", "owner"],
)
def test_session_text_that_is_not_utf8_is_stored_as_the_conversion_reads_it(
    tmp_path: Path,
    column: str,
    stored: bytes,
    read: Callable[[ConversationSession], bool],
) -> None:
    """A session column worker code stored as text that is not UTF-8 is rewritten with U+FFFD, so the session loads."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    _store_text_bytes(database, f"UPDATE code_sessions SET {column} = CAST(? AS TEXT)", stored)  # noqa: S608

    store = _converted(database)

    session = store.load_session(_SESSION, "agent")
    assert session is not None
    assert read(session)
    assert [run.run_id for run in session.runs] == ["r0"]


def test_a_session_whose_id_is_not_utf8_is_left_out(tmp_path: Path) -> None:
    """No read can name a session whose id is not UTF-8, so it stays out and every other session converts."""
    database = _agno_database(tmp_path, (_SESSION, "agent", "code"), ("odd", "agent", "code"))
    _insert_run(database, _run("r0"), run_index=0)
    _insert_run(database, _run("o0"), run_index=0, session_id="odd")
    _store_text_bytes(
        database,
        "UPDATE code_sessions SET session_id = CAST(? AS TEXT) WHERE session_id = 'odd'",
        b"odd\x80",
    )

    store = _converted(database)

    assert _query(database, 'SELECT session_id FROM "code_sessions"') == [(_SESSION,)]
    assert _loaded_ids(store) == ["r0"]
    assert set(_seqs(database)) == {"r0"}


def test_a_session_whose_text_holds_a_replacement_character_converts_unchanged(tmp_path: Path) -> None:
    """U+FFFD stored as UTF-8 is text like any other: the session keeps its id and metadata."""
    database = _agno_database(tmp_path, ("s\ufffd", "agent", "code"))
    _insert_run(database, _run("r0"), run_index=0, session_id="s\ufffd")
    _store_raw(database, "code_sessions", "metadata", '{"note": "\ufffd"}', session_id="s\ufffd")

    store = _converted(database)

    session = store.load_session("s\ufffd", "agent")
    assert session is not None
    assert (session.metadata, [run.run_id for run in session.runs]) == ({"note": "\ufffd"}, ["r0"])


def test_removed_runs_stay_removed(tmp_path: Path) -> None:
    """A run a cut removed before the conversion still writes nothing when a late save stores it."""
    database = _agno_database(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO \"code_sessions_removed_runs\" VALUES ('cut', ?)", (_SESSION,))

    store = _converted(database)

    session = store.load_session(_SESSION, "agent")
    assert session is not None
    assert store.save_run(session, _run("cut"), record_usage=False) == "removed"
    assert store.get_run("cut") is None


_PENDING_KEY = "mindroom_pending_compaction_scope_keys"


def _session_columns(database: Path, session_id: str) -> tuple[object, object, object]:
    [row] = _query(
        database,
        'SELECT session_data, metadata, updated_at FROM "code_sessions" WHERE session_id = ?',
        (session_id,),
    )
    return row


def test_a_queued_compaction_request_becomes_the_metadata_flag(tmp_path: Path) -> None:
    """Each scope a session state queued gets the metadata force flag, and the state and the rest of the row stay."""
    database = _agno_database(
        tmp_path,
        (_SESSION, "agent", "code"),
        ("team", "team", "squad"),
        ("idle", "agent", "code"),
    )
    other_scope = {"agent:other": {"force_compact_before_next_run": True}}
    _store_raw(
        database,
        "code_sessions",
        "session_data",
        json.dumps(
            {
                "session_state": {_PENDING_KEY: ["agent:code", "team:squad"], "tool": "kept"},
                "session_metrics": {"total_tokens": 3},
            },
        ),
        session_id=_SESSION,
    )
    _store_raw(
        database,
        "code_sessions",
        "metadata",
        json.dumps(
            {"other": "kept", constants.MINDROOM_COMPACTION_METADATA_KEY: {"version": 2, "states": other_scope}},
        ),
        session_id=_SESSION,
    )
    # An Agno 2 row stored its session data encoded twice.
    _store_raw(
        database,
        "code_sessions",
        "session_data",
        json.dumps(json.dumps({"session_state": {_PENDING_KEY: ["team:squad"]}})),
        session_id="team",
    )
    _store_raw(database, "code_sessions", "session_data", '{"session_state": {"tool": 1}}', session_id="idle")
    idle_before = _session_columns(database, "idle")

    store = _converted(database)

    session_data, metadata, updated_at = _session_columns(database, _SESSION)
    assert json.loads(session_data) == {"session_state": {"tool": "kept"}, "session_metrics": {"total_tokens": 3}}
    flagged = {"force_compact_before_next_run": True}
    assert json.loads(metadata) == {
        "other": "kept",
        constants.MINDROOM_COMPACTION_METADATA_KEY: {
            "version": 2,
            "states": {**other_scope, "agent:code": flagged, "team:squad": flagged},
        },
    }
    assert updated_at == 1
    assert has_queued_force_compaction(store, _SESSION, HistoryScope(kind="agent", scope_id="code"))
    assert has_queued_force_compaction(store, _SESSION, HistoryScope(kind="team", scope_id="squad"))
    team = store.load_session("team", "team")
    assert team is not None
    assert team.session_state == {}
    assert has_queued_force_compaction(store, "team", HistoryScope(kind="team", scope_id="squad"))
    assert _session_columns(database, "idle") == idle_before


def test_a_request_queued_before_the_summary_rows_existed_leaves_the_scopes_tombstones_to_their_step(
    tmp_path: Path,
) -> None:
    """The destructive compactor's tombstones in the scope's state drop their runs before the flag joins that state."""
    compaction = constants.MINDROOM_COMPACTION_METADATA_KEY
    database = write_agno_database(
        _NAMES.database_path(tmp_path),
        ConversationSession(
            session_id=_SESSION,
            kind="agent",
            owner_id="code",
            runs=[_run("gone"), _run("kept")],
            metadata={compaction: {"version": 2, "states": {"agent:code": {"compacted_run_ids": ["gone"]}}}},
            session_state={_PENDING_KEY: ["agent:code"]},
            created_at=1,
        ),
    )

    store = _converted(database)

    assert _loaded_ids(store) == ["kept"]
    session = store.load_session(_SESSION, "agent")
    assert session is not None
    assert session.session_state == {}
    assert session.metadata == {
        compaction: {"version": 2, "states": {"agent:code": {"force_compact_before_next_run": True}}},
    }


@pytest.mark.parametrize(
    "queued",
    [
        "agent:code",
        {"agent:code": True},
        None,
        [None, 3, "", "agent:", ":code", "workflow:code", ["agent:code"]],
    ],
)
def test_a_queued_request_that_names_no_scope_requests_nothing(tmp_path: Path, queued: object) -> None:
    """A run read only a list holding its own scope's key, so any other entry asks for nothing; the key still goes."""
    database = _agno_database(tmp_path)
    _store_raw(
        database,
        "code_sessions",
        "session_data",
        json.dumps({"session_state": {_PENDING_KEY: queued, "tool": "kept"}}),
        session_id=_SESSION,
    )
    _store_raw(database, "code_sessions", "metadata", '{"other":  "kept"}', session_id=_SESSION)

    store = _converted(database)

    session_data, metadata, _updated_at = _session_columns(database, _SESSION)
    assert json.loads(session_data) == {"session_state": {"tool": "kept"}}
    assert metadata == '{"other":  "kept"}'
    assert not has_queued_force_compaction(store, _SESSION, HistoryScope(kind="agent", scope_id="code"))


@pytest.mark.parametrize("session_data", ["not json", json.dumps({"session_state": [_PENDING_KEY]}), json.dumps([1])])
def test_session_data_that_holds_no_state_object_converts_as_stored(tmp_path: Path, session_data: str) -> None:
    """Worker code can write the session row, so session data a run could not read leaves the row as it was."""
    database = _agno_database(tmp_path)
    _store_raw(database, "code_sessions", "session_data", session_data, session_id=_SESSION)
    before = _session_columns(database, _SESSION)

    _converted(database)

    assert _session_columns(database, _SESSION) == before


def test_a_removed_run_whose_id_is_not_utf8_is_stored_as_the_conversion_reads_it(tmp_path: Path) -> None:
    """A removed run id worker code stored as text that is not UTF-8 is copied with U+FFFD, so its session still loads."""
    database = _agno_database(tmp_path)
    _insert_run(database, _run("r0"), run_index=0)
    _store_text_bytes(
        database,
        "INSERT INTO code_sessions_removed_runs VALUES (CAST(? AS TEXT), ?), (CAST(? AS TEXT), ?)",
        b"cut\x80",
        _SESSION,
        b"cut\x81",
        _SESSION,
    )

    store = _converted(database)

    assert _query(database, 'SELECT run_id FROM "code_sessions_log_removed"') == [("cut\ufffd",)]
    assert _loaded_ids(store) == ["r0"]


@pytest.mark.parametrize("dump", GOLDEN_DUMPS, ids=lambda dump: dump.stem)
def test_a_converted_database_holds_only_the_v2_layout(tmp_path: Path, dump: Path) -> None:
    """The Agno tables, the sessions table's other columns, and its unused index are gone; the meta row marks it."""
    database, names = _restore(dump, tmp_path)

    migrate_conversation_database(database, names)

    with closing(sqlite3.connect(database)) as connection:
        assert table_names(connection) == {
            names.sessions,
            names.usage,
            names.log_meta,
            names.log_runs,
            names.log_messages,
            names.log_summaries,
            names.log_removed,
            "sqlite_sequence",
        }
        columns = [row[0] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (names.sessions,))]
        indexes = {row[0] for row in connection.execute("SELECT name FROM pragma_index_list(?)", (names.sessions,))}
        meta = connection.execute(f'SELECT version, next_seq FROM "{names.log_meta}"').fetchall()  # noqa: S608
        (largest,) = connection.execute(f'SELECT COALESCE(MAX(seq), 0) FROM "{names.log_runs}"').fetchone()  # noqa: S608
    assert columns == list(SESSION_COLUMNS)
    assert {index for index in indexes if not index.startswith("sqlite_autoindex")} == {
        f"idx_{names.sessions}_session_type",
    }
    assert meta == [(LOG_VERSION, largest + 1)]


@pytest.mark.parametrize("name", sorted(_DUMPS))
def test_every_input_converts_once_and_the_same_way_twice(tmp_path: Path, name: str) -> None:
    """A converted database converts to itself, and two copies of one original convert to the same bytes of rows."""
    dumps = []
    for copy in ("first", "second"):
        database, names = _restore(_DUMPS[name], tmp_path / copy)
        original = database.read_bytes()
        backup = database.with_name(f"{database.name}{BACKUP_SUFFIX}")
        with deterministic():
            migrate_conversation_database(database, names)
            converted = _dump(database)
            migrate_conversation_database(database, names)
        assert _dump(database) == converted
        assert backup.read_bytes() == original
        dumps.append(converted)
    assert dumps[0] == dumps[1]


# The entry: its transaction, its backup, and the files it refuses.


def test_a_failed_conversion_leaves_the_database_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error after the runs converted leaves the original bytes and no marker; the next open converts it."""
    database, names = _restore(GOLDEN_DUMPS[0], tmp_path)
    original = database.read_bytes()

    def fail(*_: object) -> None:
        msg = "injected"
        raise RuntimeError(msg)

    with monkeypatch.context() as patched:
        patched.setattr(legacy_agno3_conversion, "_convert_summary", fail)
        with pytest.raises(RuntimeError, match="injected"):
            migrate_conversation_database(database, names)
    assert database.read_bytes() == original
    assert not _is_converted(database)
    backup = database.with_name(f"{database.name}{BACKUP_SUFFIX}")
    backup.write_bytes(b"stale")

    migrate_conversation_database(database, names)

    assert _is_converted(database)
    assert backup.read_bytes() == original


def test_a_failed_conversion_rolls_back_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while a cursor still reads rolls back before the close, which that cursor would leave unfinished.

    An unfinished close keeps the transaction and its journal open, though closing the backup's descriptor drops
    the file's locks; its rollback would then write the file without holding them.
    """
    database, names = _restore(GOLDEN_DUMPS[0], tmp_path)

    def read_then_fail(connection: sqlite3.Connection, *_: object) -> None:
        reading = connection.execute(f'SELECT session_id FROM "{names.sessions}"')  # noqa: S608
        reading.fetchone()
        msg = "injected"
        raise RuntimeError(msg)

    monkeypatch.setattr(legacy_agno3_conversion, "_convert_summary", read_then_fail)
    with pytest.raises(RuntimeError, match="injected") as interrupted:
        migrate_conversation_database(database, names)

    # The failure's traceback keeps the reading cursor alive, as it does for a caller that logs or re-raises it.
    assert interrupted.traceback
    assert not Path(f"{database}-journal").exists()
    assert _other_process_takes_write_lock(database) == "locked"


def test_a_converted_database_costs_one_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening a converted database reads only its marker, beside checking which file SQLite opened."""
    database, names = _restore(GOLDEN_DUMPS[0], tmp_path)
    migrate_conversation_database(database, names)
    statements: list[str] = []
    connect = legacy_agno3_conversion.connect_conversation_database

    def traced(path: Path, *, timeout: float) -> sqlite3.Connection:
        connection = connect(path, timeout=timeout)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(legacy_agno3_conversion, "connect_conversation_database", traced)
    migrate_conversation_database(database, names)

    # SQLite traces the pragma a table-valued function runs as a comment of its own.
    run = [statement for statement in statements if not statement.startswith("--")]
    assert len(run) == 2
    assert "pragma_database_list" in run[0]
    assert run[1].startswith("SELECT 1 FROM sqlite_master")


def test_the_marker_is_read_again_under_the_write_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A converter that read no marker before another one converted the file leaves it, and its backup, alone."""
    database, names = _restore(GOLDEN_DUMPS[0], tmp_path)
    original = database.read_bytes()
    leave_wal_mode = legacy_agno3_conversion._leave_wal_mode
    converts = []

    def other_opener_converts_first(connection: sqlite3.Connection, path: Path) -> None:
        monkeypatch.setattr(legacy_agno3_conversion, "_leave_wal_mode", leave_wal_mode)
        migrate_conversation_database(path, names)
        leave_wal_mode(connection, path)

    convert = legacy_agno3_conversion._convert
    monkeypatch.setattr(legacy_agno3_conversion, "_leave_wal_mode", other_opener_converts_first)
    monkeypatch.setattr(legacy_agno3_conversion, "_convert", lambda *args: converts.append(convert(*args)))

    migrate_conversation_database(database, names)

    assert len(converts) == 1
    assert database.with_name(f"{database.name}{BACKUP_SUFFIX}").read_bytes() == original


def test_no_other_process_writes_between_the_backup_and_the_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry's write lock spans the backup and the conversion, so another process cannot write between them."""
    database, names = _restore(GOLDEN_DUMPS[0], tmp_path)
    convert_runs = legacy_agno3_conversion._convert_runs
    seen: list[str] = []

    def convert_runs_after_probe(*args: Any) -> object:  # noqa: ANN401
        seen.append(_other_process_takes_write_lock(database))
        return convert_runs(*args)

    monkeypatch.setattr(legacy_agno3_conversion, "_convert_runs", convert_runs_after_probe)
    migrate_conversation_database(database, names)

    assert set(seen) == {"database is locked"}
    assert _other_process_takes_write_lock(database) == "locked"


@pytest.mark.parametrize("link", ["database", "dangling", "directory"])
def test_a_link_at_the_database_or_its_directory_is_refused(tmp_path: Path, link: str) -> None:
    """The conversion never opens a link's target, nor creates a dangling one's."""
    target, names = _restore(GOLDEN_DUMPS[0], tmp_path / "target")
    before = target.read_bytes()
    database = names.database_path(tmp_path / "store")
    database.parent.parent.mkdir(parents=True)
    if link == "directory":
        database.parent.symlink_to(target.parent, target_is_directory=True)
    else:
        database.parent.mkdir()
        database.symlink_to(target if link == "database" else tmp_path / "created.db")

    with pytest.raises(ValueError, match="link"):
        migrate_conversation_database(database, names)

    assert target.read_bytes() == before
    assert not (tmp_path / "created.db").exists()
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


def test_a_link_planted_after_the_check_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link swapped in between the check and the connect is caught by the file SQLite reports it opened."""
    target, names = _restore(GOLDEN_DUMPS[0], tmp_path / "target")
    before = target.read_bytes()
    database = names.database_path(tmp_path / "store")
    database.parent.mkdir(parents=True)
    connect = legacy_agno3_conversion.connect_conversation_database

    def swapped(path: Path, *, timeout: float) -> sqlite3.Connection:
        path.symlink_to(target)
        return connect(path, timeout=timeout)

    monkeypatch.setattr(legacy_agno3_conversion, "connect_conversation_database", swapped)
    with pytest.raises(ValueError, match="instead"):
        migrate_conversation_database(database, names)

    assert target.read_bytes() == before


def test_a_sessions_directory_swapped_for_a_link_after_the_check_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link swapped in for the sessions directory between the check and the connect gets no v2 tables written."""
    outside = tmp_path / "outside"
    outside.mkdir()
    database = _NAMES.database_path(tmp_path / "store")
    database.parent.mkdir(parents=True)
    connect = legacy_agno3_conversion.connect_conversation_database

    def swapped(path: Path, *, timeout: float) -> sqlite3.Connection:
        path.parent.rename(tmp_path / "moved")
        path.parent.symlink_to(outside, target_is_directory=True)
        return connect(path, timeout=timeout)

    monkeypatch.setattr(legacy_agno3_conversion, "connect_conversation_database", swapped)
    with pytest.raises(ValueError, match="instead"):
        migrate_conversation_database(database, _NAMES)

    # SQLite's open may have left an empty file at the link's target, never a database with tables.
    planted = outside / database.name
    assert not planted.exists() or planted.stat().st_size == 0


def test_a_sessions_directory_swapped_for_a_link_before_the_resolve_is_refused(tmp_path: Path) -> None:
    """A link swapped in for the sessions directory while the conversion resolves its expected path is refused too."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = _NAMES.database_path(tmp_path / "store").parent
    sessions.mkdir(parents=True)

    class SwappedOnFirstResolve(Path):
        def resolve(self, strict: bool = False) -> Path:
            if not sessions.is_symlink():
                sessions.rename(tmp_path / "moved")
                sessions.symlink_to(outside, target_is_directory=True)
            return super().resolve(strict=strict)

    database = SwappedOnFirstResolve(_NAMES.database_path(tmp_path / "store"))
    with pytest.raises(ValueError, match="instead"):
        migrate_conversation_database(database, _NAMES)

    assert sessions.is_symlink()
    planted = outside / database.name
    assert not planted.exists() or planted.stat().st_size == 0


def _wal_crash_copy(tmp_path: Path) -> tuple[Path, TableNames]:
    """Return a copy of a golden in write-ahead-log mode as a crash left it: a commit only in its log."""
    source, names = _restore(GOLDEN_DUMPS[0], tmp_path / "source")
    writer = sqlite3.connect(source, isolation_level=None)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(f'UPDATE "{names.sessions}" SET metadata = \'{{"only": "in the log"}}\'')  # noqa: S608
    database = names.database_path(tmp_path / "crashed")
    database.parent.mkdir(parents=True)
    for suffix in ("", "-wal"):
        shutil.copyfile(f"{source}{suffix}", f"{database}{suffix}")
    writer.close()
    return database, names


def test_a_write_ahead_log_database_is_checkpointed_and_converted(tmp_path: Path) -> None:
    """A commit only the log holds reaches both the converted file and its backup, and the file leaves the mode."""
    database, names = _wal_crash_copy(tmp_path)

    migrate_conversation_database(database, names)

    assert not Path(f"{database}-wal").exists()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert {row[0] for row in connection.execute(f'SELECT metadata FROM "{names.sessions}"')} == {  # noqa: S608
            '{"only": "in the log"}',
        }
    restored = tmp_path / "restored.db"
    shutil.copyfile(database.with_name(f"{database.name}{BACKUP_SUFFIX}"), restored)
    with closing(sqlite3.connect(restored)) as connection:
        assert {row[0] for row in connection.execute(f'SELECT metadata FROM "{names.sessions}"')} == {  # noqa: S608
            '{"only": "in the log"}',
        }


def test_a_write_ahead_log_another_connection_holds_stops_the_conversion(tmp_path: Path) -> None:
    """When the log cannot be checkpointed, the conversion stops with an error naming the database."""
    database, names = _wal_crash_copy(tmp_path)
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute("SELECT 1 FROM sqlite_master").fetchall()
    try:
        with pytest.raises(RuntimeError, match=re.escape(str(database))):
            migrate_conversation_database(database, names, timeout=0)
    finally:
        holder.close()
    assert not _is_converted(database)


def test_a_hot_journal_is_rolled_back_before_the_backup(tmp_path: Path) -> None:
    """A writer that died mid-transaction leaves changed pages and a hot journal; the backup holds the committed file."""
    database, names = _restore(GOLDEN_DUMPS[0], tmp_path)
    committed = database.read_bytes()
    subprocess.run([sys.executable, "-I", "-c", _DIE_MID_TRANSACTION, str(database)], check=True)
    assert Path(f"{database}-journal").exists()
    assert database.read_bytes() != committed

    migrate_conversation_database(database, names)

    assert database.with_name(f"{database.name}{BACKUP_SUFFIX}").read_bytes() == committed
    assert "filler" not in _tables(database)


def test_a_database_without_a_sessions_table_only_gets_the_v2_tables(tmp_path: Path) -> None:
    """A new or empty database gets the v2 tables without a backup; one holding another table is backed up first."""
    for name, prepare in (("new", None), ("empty", b""), ("other", "CREATE TABLE other (x)")):
        database = _NAMES.database_path(tmp_path / name)
        database.parent.mkdir(parents=True)
        if isinstance(prepare, bytes):
            database.write_bytes(prepare)
        elif prepare is not None:
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(prepare)

        migrate_conversation_database(database, _NAMES)

        assert {_NAMES.log_meta, _NAMES.log_runs, _NAMES.usage} <= _tables(database)
        assert database.with_name(f"{database.name}{BACKUP_SUFFIX}").exists() == (name == "other")


# The startup sweep.


def _runtime(tmp_path: Path) -> tuple[Any, Path]:
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    return paths, paths.storage_root


def _store_at(directory: Path) -> Path:
    """Create the Agno-layout database of the store directory ``directory``, with one session and one run."""
    names = TableNames(directory.name)
    database = _agno_database(directory, names=names)
    _insert_run(database, _run("r0"), run_index=0)
    return database


@pytest.mark.asyncio
async def test_the_startup_sweep_converts_every_store_and_skips_what_it_cannot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every store under the state root converts; a linked, unreadable, locked, or too large one is logged and skipped."""
    paths, root = _runtime(tmp_path)
    converted = [
        _store_at(root / relative) for relative in ("agents/code", "private_instances/scope/code", "teams/code")
    ]
    # Linked in under the name of the database it holds, so following the link would reach it.
    outside = _store_at(tmp_path / "outside/alias")
    (root / "agents/alias").symlink_to(outside.parent.parent, target_is_directory=True)
    linked = root / "agents/linked/sessions/linked.db"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    garbage = root / "agents/garbage/sessions/garbage.db"
    garbage.parent.mkdir(parents=True)
    garbage.write_bytes(b"not a database")
    (root / "agents/empty/sessions").mkdir(parents=True)
    locked, full = _store_at(root / "agents/locked"), _store_at(root / "agents/full")
    write_backup = legacy_agno3_conversion.write_conversion_backup

    def backup_without_space(connection: sqlite3.Connection, database: Path, descriptors: ExitStack) -> Path:
        if database == full:
            msg = f"Converting {database} to storage v2 needs more bytes"
            raise ConversionSpaceError(msg)
        return write_backup(connection, database, descriptors)

    monkeypatch.setattr(legacy_agno3_conversion, "write_conversion_backup", backup_without_space)
    before = outside.read_bytes()
    holder = sqlite3.connect(locked, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with capture_logs() as logs:
            await migrate_conversation_storage(paths)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert elapsed < 10
    assert all(_is_converted(database) for database in converted)
    assert outside.read_bytes() == before
    assert not (root / "agents/empty/sessions/empty.db").exists()
    assert sorted(_skipped(logs, "conversation_conversion_skipped_store")) == sorted(
        str(path) for path in (full, garbage, linked, locked)
    )
    assert garbage.read_bytes() == b"not a database"
    migrate_conversation_database(locked, TableNames.of_database(locked))
    assert _is_converted(locked)


@pytest.mark.asyncio
async def test_the_startup_sweep_does_not_follow_a_linked_root(tmp_path: Path) -> None:
    """An ``agents`` root worker code replaced with a link is not walked; the other roots convert."""
    paths, root = _runtime(tmp_path)
    healthy = _store_at(root / "teams/code")
    outside = _store_at(tmp_path / "outside/agents/code")
    (root / "agents").symlink_to(tmp_path / "outside/agents", target_is_directory=True)

    await migrate_conversation_storage(paths)

    assert _is_converted(healthy)
    assert not _is_converted(outside)


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_the_startup_sweep_skips_an_unlistable_private_instance(tmp_path: Path) -> None:
    """A private instance directory worker code made unlistable is logged and skipped, and startup goes on."""
    paths, root = _runtime(tmp_path)
    healthy = _store_at(root / "teams/code")
    hidden = _store_at(root / "private_instances/scope/code")
    scope = root / "private_instances/scope"
    scope.chmod(0)
    try:
        with capture_logs() as logs:
            await migrate_conversation_storage(paths)
    finally:
        scope.chmod(0o755)

    assert _is_converted(healthy)
    assert not _is_converted(hidden)
    assert _skipped(logs, "conversation_conversion_skipped_store") == [str(scope)]


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
async def test_the_startup_sweep_skips_a_store_it_cannot_read(tmp_path: Path) -> None:
    """A store worker code made unreadable is logged and skipped, and the others convert."""
    paths, root = _runtime(tmp_path)
    healthy = _store_at(root / "agents/code")
    unreadable = _store_at(root / "agents/blocked")
    unreadable.parent.chmod(0)
    try:
        with capture_logs() as logs:
            await migrate_conversation_storage(paths)
    finally:
        unreadable.parent.chmod(0o755)

    assert _is_converted(healthy)
    assert _skipped(logs, "conversation_conversion_skipped_store") == [str(unreadable)]
