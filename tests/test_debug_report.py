"""Tests for `mindroom debug-report`."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from mindroom.debug_report import collect_ids, postgres_query, read_journal, sqlite_query
from mindroom.event_journal.schema import POSTGRES_DIALECT, SQLITE_DIALECT, schema_statements
from tests.conftest import postgres_journal_schema_url

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.com"


def _report() -> dict[str, object]:
    return {
        "type": "io.mindroom.bug_report",
        "version": 1,
        "target": {"roomId": ROOM, "threadId": "$root", "eventId": "$reply"},
        "events": [
            {"event": {"event_id": "$root", "content": {"body": "hi"}}, "latestEdit": None},
            {"event": {"event_id": "~!room:example.com:m1", "content": {}}, "latestEdit": None},
            {
                "event": {
                    "event_id": "$reply",
                    "content": {"io.mindroom.ai_run": {"run_id": "run-1", "session_id": f"{ROOM}:$root"}},
                },
                "latestEdit": {
                    "event_id": "$edit",
                    "content": {"m.new_content": {"io.mindroom.ai_run": {"run_id": "run-2"}}},
                },
            },
        ],
    }


def test_collect_ids_reads_targets_events_edits_and_nested_ai_runs() -> None:
    """Targets, events, edits, and ai_run blocks nested in m.new_content all contribute ids."""
    ids = collect_ids(_report())
    assert ids.room_id == ROOM
    assert ids.thread_id == "$root"
    assert ids.event_ids == frozenset({"$root", "$reply", "$edit"})
    assert ids.run_ids == frozenset({"run-1", "run-2"})
    assert ids.session_ids == frozenset({f"{ROOM}:$root"})


def test_collect_ids_merges_flags_and_derives_unthreaded_session() -> None:
    """Flags merge in, local echoes are dropped, and an unthreaded room maps to a bare-room session."""
    ids = collect_ids(None, event_ids=["$a", "~local"], room_id=ROOM)
    assert ids.event_ids == frozenset({"$a"})
    assert ids.session_ids == frozenset({ROOM})
    assert not ids.is_empty()
    assert collect_ids(None).is_empty()


def _seed_journal(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        for statement in schema_statements(SQLITE_DIALECT):
            db.execute(statement)
        turn = "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (?, ?, ?, ?)"
        db.execute(turn, ("general", "$user", "$user", json.dumps({"correlation_id": "$user"})))
        db.execute(turn, ("general", "$unrelated", "$unrelated", "{}"))
        journal = (
            "INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender, "
            "origin_server_ts, source_json, membership_epoch, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        alice, bob = "@alice:example.com", "@bob:example.com"
        db.executemany(
            journal,
            [
                ("general@x", "$user", ROOM, "$root", "message", alice, 1, '{"a": 1}', 1, "settled"),
                ("general@x", "$late", ROOM, "$root", "message", alice, 2, "{}", 1, "pending"),
                ("general@x", "$other", "!other:example.com", "", "message", bob, 3, "{}", 1, "settled"),
            ],
        )
        db.commit()


def test_read_journal_matches_events_and_thread_and_decodes_json(tmp_path: Path) -> None:
    """Turn records match by event id, journal rows by event id or thread, and JSON columns come back decoded."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    ids = collect_ids(None, event_ids=["$user"], room_id=ROOM, thread_id="$root")
    with sqlite_query(path) as query:
        results = read_journal(query, ids, str(path))

    turns = results["turn_records"]
    assert turns.status == "ok"
    assert [item["index_event_id"] for item in turns.items] == ["$user"]
    assert turns.items[0]["record_json"] == {"correlation_id": "$user"}

    events = results["journal_events"].items
    assert [item["event_id"] for item in events] == ["$user", "$late"]
    assert events[0]["source_json"] == {"a": 1}
    assert results["delivery_outbox"].status == "ok"
    assert results["delivery_outbox"].items == []


def test_read_journal_without_thread_matches_event_ids_only(tmp_path: Path) -> None:
    """Without a thread, only the named events are read and the outbox is not queried."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    with sqlite_query(path) as query:
        results = read_journal(query, collect_ids(None, event_ids=["$late"], room_id=ROOM), str(path))
    assert [item["event_id"] for item in results["journal_events"].items] == ["$late"]
    assert results["delivery_outbox"].items == []


def test_sqlite_query_is_read_only(tmp_path: Path) -> None:
    """The reader opens the database read-only, so inspecting an install cannot change it."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    with sqlite_query(path) as query, pytest.raises(sqlite3.OperationalError):
        query("DELETE FROM turn_records", [])


def test_postgres_query_reads_the_journal_and_cannot_write(postgres_journal_url: str) -> None:
    """The PostgreSQL reader returns the same shape as SQLite and refuses writes on its autocommit connection."""
    import psycopg  # noqa: PLC0415 - psycopg ships with the optional postgres extra

    database_url = postgres_journal_schema_url(postgres_journal_url)
    with psycopg.connect(database_url, autocommit=True) as db:
        for statement in schema_statements(POSTGRES_DIALECT):
            db.execute(statement)
        db.execute(
            "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (%s, %s, %s, %s)",
            ("general", "$user", "$user", json.dumps({"correlation_id": "$user"})),
        )

    ids = collect_ids(None, event_ids=["$user"], room_id=ROOM, thread_id="$root")
    with postgres_query(database_url) as query:
        results = read_journal(query, ids, "postgres")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            query("DELETE FROM turn_records", [])

    assert results["turn_records"].items == [
        {
            "agent_name": "general",
            "index_event_id": "$user",
            "anchor_event_id": "$user",
            "record_json": {"correlation_id": "$user"},
        },
    ]
    assert results["journal_events"].items == []
    with psycopg.connect(database_url, autocommit=True) as db:
        assert db.execute("SELECT count(*) FROM turn_records").fetchone() == (1,)
