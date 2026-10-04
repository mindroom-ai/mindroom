"""Tests for `mindroom debug-report`."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom import debug_report as debug_report_module
from mindroom.agent_storage import create_state_storage
from mindroom.debug_report import (
    DebugReportSources,
    build_debug_report,
    collect_ids,
    postgres_query,
    read_agno_runs,
    read_journal,
    read_jsonl_records,
    read_log_lines,
    sqlite_query,
)
from mindroom.event_journal.schema import POSTGRES_DIALECT, SQLITE_DIALECT, schema_statements
from tests.conftest import postgres_journal_schema_url, seed_session

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


def test_sqlite_query_accepts_a_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative database path resolves against the working directory instead of raising."""
    _seed_journal(tmp_path / "event_journal.db")
    monkeypatch.chdir(tmp_path)
    with sqlite_query(Path("event_journal.db")) as query:
        assert len(query("SELECT * FROM turn_records", [])) == 2


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


def _seed_agno(storage_root: Path) -> None:
    storage = create_state_storage(
        "general",
        storage_root / "agents" / "general",
        subdir="sessions",
        session_table="general_sessions",
    )
    try:
        for session_id, run_id in ((f"{ROOM}:$root", "run-1"), ("!other:example.com", "run-other")):
            seed_session(
                storage,
                AgentSession(
                    session_id=session_id,
                    agent_id="general",
                    user_id="@alice:example.com",
                    runs=[
                        RunOutput(
                            run_id=run_id,
                            agent_id="general",
                            session_id=session_id,
                            user_id="@alice:example.com",
                            created_at=1_723_837_600,
                            status=RunStatus.completed,
                            metadata={"matrix_event_id": "$user"},
                        ),
                    ],
                    created_at=1_723_837_600,
                    updated_at=1_723_837_600,
                ),
            )
    finally:
        storage.close()


def test_read_agno_runs_matches_session_and_decodes_run_data(tmp_path: Path) -> None:
    """Runs match by session id, and run_data comes back as a nested object."""
    _seed_agno(tmp_path)
    ids = collect_ids(None, room_id=ROOM, thread_id="$root")
    result = read_agno_runs(tmp_path, ids)
    assert result.status == "ok"
    assert [item["run_id"] for item in result.items] == ["run-1"]
    assert result.items[0]["run_data"]["metadata"] == {"matrix_event_id": "$user"}
    assert result.items[0]["table"] == "general_sessions_runs"


def test_read_agno_runs_reports_missing_databases(tmp_path: Path) -> None:
    """A session root without databases is reported as missing."""
    result = read_agno_runs(tmp_path, collect_ids(None, room_id=ROOM))
    assert result.status == "missing"


def _seed_files(storage_root: Path) -> None:
    tracking = storage_root / "tracking"
    tracking.mkdir(parents=True, exist_ok=True)
    (tracking / "tool_calls.jsonl.1").write_text(
        json.dumps({"tool_name": "old", "correlation_id": "$user"}) + "\n",
        encoding="utf-8",
    )
    (tracking / "tool_calls.jsonl").write_text(
        json.dumps({"tool_name": "shell", "correlation_id": "$user", "result": "ok"})
        + "\n"
        + json.dumps({"tool_name": "other", "correlation_id": "$elsewhere"})
        + "\n"
        + "not json $user\n",
        encoding="utf-8",
    )
    llm = storage_root / "logs" / "llm_requests"
    llm.mkdir(parents=True, exist_ok=True)
    (llm / "llm-requests-2026-10-03.jsonl").write_text(
        json.dumps({"request_log_id": "r1", "session_id": f"{ROOM}:$root", "full_prompt": "p"})
        + "\n"
        + json.dumps({"record": "response", "request_log_id": "r1", "correlation_id": "$user"})
        + "\n"
        + json.dumps({"request_log_id": "r2", "session_id": ROOM})
        + "\n",
        encoding="utf-8",
    )
    (storage_root / "logs" / "mindroom_20261003_120000.log").write_text(
        "2026 [info] Dispatching event_id=$user\n"
        f"2026 [info] room heartbeat room_id={ROOM}\n"
        "2026 [info] run started run_id=run-1 " + "x" * 5000 + "\n",
        encoding="utf-8",
    )


def _sources(storage_root: Path, *, journal_postgres_url: str | None = None) -> DebugReportSources:
    return DebugReportSources(
        storage_root=storage_root,
        session_root=storage_root,
        journal_sqlite_path=storage_root / "tracking" / "event_journal.db",
        journal_postgres_url=journal_postgres_url,
        llm_request_log_dir=storage_root / "logs" / "llm_requests",
    )


def test_jsonl_and_log_readers_match_structured_fields_and_bound_output(tmp_path: Path) -> None:
    """Records match by correlation, reply target, or session; log lines by event or run id, capped per line."""
    _seed_files(tmp_path)
    ids = dataclasses.replace(
        collect_ids(None, event_ids=["$user"], room_id=ROOM, thread_id="$root"),
        run_ids=frozenset({"run-1"}),
    )

    tracking = tmp_path / "tracking"
    tools = read_jsonl_records(
        [tracking / "tool_calls.jsonl.1", tracking / "tool_calls.jsonl"],
        ids,
        location=tracking / "tool_calls.jsonl",
    )
    assert [item["tool_name"] for item in tools.items] == ["old", "shell"]

    llm_dir = tmp_path / "logs" / "llm_requests"
    llm = read_jsonl_records(sorted(llm_dir.glob("llm-requests-*.jsonl")), ids, location=llm_dir)
    assert [item.get("record", "request") for item in llm.items] == ["request", "response"]

    logs = read_log_lines(sorted((tmp_path / "logs").glob("mindroom_*.log")), ids, location=tmp_path / "logs")
    assert [item["line"] for item in logs.items] == [1, 3]
    assert logs.truncated == 1
    assert len(logs.items[1]["text"]) == debug_report_module.MAX_LOG_LINE_CHARS


def test_jsonl_reader_caps_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Records beyond the cap are counted as dropped instead of growing the report without bound."""
    monkeypatch.setattr(debug_report_module, "MAX_JSONL_RECORDS", 1)
    _seed_files(tmp_path)
    tracking = tmp_path / "tracking"
    result = read_jsonl_records(
        [tracking / "tool_calls.jsonl.1", tracking / "tool_calls.jsonl"],
        collect_ids(None, event_ids=["$user"]),
        location=tracking / "tool_calls.jsonl",
    )
    assert len(result.items) == 1
    assert result.dropped == 1


def test_build_debug_report_combines_every_source(tmp_path: Path) -> None:
    """One document carries the journal, Agno, tool call, LLM request, and log sources, and is JSON-serializable."""
    _seed_journal(tmp_path / "tracking" / "event_journal.db")
    _seed_agno(tmp_path)
    _seed_files(tmp_path)
    document = build_debug_report(_sources(tmp_path), collect_ids(_report()), generated_at="2026-10-03T12:00:00+00:00")
    assert document["type"] == "io.mindroom.debug_report"
    assert document["identifiers"]["threadId"] == "$root"
    assert set(document["sources"]) == {
        "turn_records",
        "journal_events",
        "delivery_outbox",
        "agno_runs",
        "tool_calls",
        "llm_requests",
        "log_lines",
    }
    assert document["sources"]["agno_runs"]["items"][0]["run_id"] == "run-1"
    json.dumps(document)


def test_build_debug_report_marks_missing_sources(tmp_path: Path) -> None:
    """An install with no data at all reports every source as missing instead of failing."""
    document = build_debug_report(_sources(tmp_path), collect_ids(None, event_ids=["$x"]), generated_at="now")
    assert {name: source["status"] for name, source in document["sources"].items()} == dict.fromkeys(
        document["sources"],
        "missing",
    )


def test_build_debug_report_isolates_a_failing_agno_database(tmp_path: Path) -> None:
    """A session database that cannot be read marks only agno_runs as an error; other sources still report."""
    _seed_journal(tmp_path / "tracking" / "event_journal.db")
    _seed_files(tmp_path)
    broken = tmp_path / "agents" / "x" / "sessions" / "x.db"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a sqlite database" * 100)

    document = build_debug_report(_sources(tmp_path), collect_ids(_report(), event_ids=["$user"]), generated_at="now")

    sources = document["sources"]
    assert sources["agno_runs"]["status"] == "error"
    assert sources["agno_runs"]["error"]
    healthy = {name: source["status"] for name, source in sources.items() if name != "agno_runs"}
    assert healthy == dict.fromkeys(healthy, "ok")
    assert [item["tool_name"] for item in sources["tool_calls"]["items"]] == ["old", "shell"]
    json.dumps(document)


def test_build_debug_report_marks_every_journal_source_when_the_journal_cannot_be_read(tmp_path: Path) -> None:
    """A journal missing its tables (an older install) marks all three journal sources as errors."""
    journal = tmp_path / "tracking" / "event_journal.db"
    journal.parent.mkdir(parents=True)
    with closing(sqlite3.connect(journal)) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    _seed_files(tmp_path)

    document = build_debug_report(_sources(tmp_path), collect_ids(_report()), generated_at="now")

    sources = document["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert sources[name]["status"] == "error"
        assert "no such table" in sources[name]["error"]
    assert sources["tool_calls"]["status"] == "ok"


def test_build_debug_report_marks_journal_sources_when_postgres_is_unreachable(tmp_path: Path) -> None:
    """A PostgreSQL journal that refuses the connection marks all three journal sources as errors."""
    pytest.importorskip("psycopg")
    _seed_files(tmp_path)

    unreachable = "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    document = build_debug_report(
        _sources(tmp_path, journal_postgres_url=unreachable),
        collect_ids(_report()),
        generated_at="now",
    )

    sources = document["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert sources[name]["status"] == "error"
        assert sources[name]["error"]
        assert sources[name]["paths"] == ["postgres"]
    assert sources["tool_calls"]["status"] == "ok"
