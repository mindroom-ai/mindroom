"""Tests for `mindroom debug-report`."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from typer.testing import CliRunner

from mindroom import debug_report as debug_report_module
from mindroom import llm_request_logging
from mindroom.agent_storage import create_state_storage
from mindroom.cli.debug_report import _resolve_sources
from mindroom.cli.main import app
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.debug_report import (
    DebugReportSources,
    _postgres_query,
    _read_agno_runs,
    _read_journal,
    _read_jsonl_records,
    _read_log_lines,
    _sqlite_query,
    build_debug_report,
    collect_ids,
)
from mindroom.event_journal.schema import POSTGRES_DIALECT, SQLITE_DIALECT, schema_statements
from mindroom.event_journal_open import event_journal_sqlite_path
from mindroom.history.session_context import _team_scope_state_root
from mindroom.session_ids import create_session_id
from mindroom.tool_system import tool_calls
from mindroom.tool_system.worker_routing import (
    agent_state_root_path,
    agent_workspace_root_path,
    private_instance_scope_root_path,
)
from mindroom.usage_storage import SYSTEM_USAGE_STORAGE_NAME
from tests.conftest import postgres_journal_schema_url, seed_session, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOM = "!room:example.com"

runner = CliRunner()


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


_TURN_INSERT = "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (?, ?, ?, ?)"
_JOURNAL_INSERT = (
    "INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender, "
    "origin_server_ts, source_json, membership_epoch, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_OUTBOX_INSERT = (
    "INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch, "
    "thread_id, transaction_id, payload_json, result_json, created_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def test_runtime_names_and_paths_copied_by_the_reader_match_the_runtime(tmp_path: Path) -> None:
    """The reader stays stdlib-only, so its copies of runtime names and file layouts are pinned to the originals."""
    assert debug_report_module._AI_RUN_KEY == AI_RUN_METADATA_KEY
    assert collect_ids(None, room_id=ROOM, thread_id="$root").session_ids == {create_session_id(ROOM, "$root")}
    assert collect_ids(None, room_id=ROOM).session_ids == {create_session_id(ROOM, None)}
    assert debug_report_module._TOOL_CALL_ROTATIONS == tool_calls._TOOL_CALL_LOG_BACKUPS

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "absent.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    sources = _resolve_sources(runtime_paths)
    # model_loading.py passes `runtime_paths.storage_root / "logs" / "llm_requests"` to the request-log writer
    # as an inline expression, so that literal is what is pinned here.
    assert sources.llm_request_log_dir == runtime_paths.storage_root / "logs" / "llm_requests"

    tool_call_log = tool_calls._tool_call_log_path(runtime_paths)
    request_log = llm_request_logging._daily_log_path(None, sources.llm_request_log_dir, datetime.now().astimezone())
    for log in (tool_call_log, request_log):
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"correlation_id": "$user"}) + "\n", encoding="utf-8")
    document = build_debug_report(sources, collect_ids(None, event_ids=["$user"]), generated_at="now")
    assert document["sources"]["tool_calls"]["paths"] == [str(tool_call_log)]
    assert document["sources"]["llm_requests"]["paths"] == [str(request_log)]
    # No journal exists yet, so the missing source names where the reader looked.
    assert document["sources"]["turn_records"]["paths"] == [str(event_journal_sqlite_path(runtime_paths.storage_root))]


def _seed_journal(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        for statement in schema_statements(SQLITE_DIALECT):
            db.execute(statement)
        db.execute(_TURN_INSERT, ("general", "$user", "$user", json.dumps({"correlation_id": "$user"})))
        db.execute(_TURN_INSERT, ("general", "$unrelated", "$unrelated", "{}"))
        alice, bob = "@alice:example.com", "@bob:example.com"
        db.executemany(
            _JOURNAL_INSERT,
            [
                ("general@x", "$user", ROOM, "$root", "message", alice, 1, '{"a": 1}', 1, "settled"),
                ("general@x", "$late", ROOM, "$root", "message", alice, 2, "{}", 1, "pending"),
                ("general@x", "$other", "!other:example.com", "", "message", bob, 3, "{}", 1, "settled"),
            ],
        )
        payload = json.dumps({"msgtype": "m.text", "body": "answer"})
        db.executemany(
            _OUTBOX_INSERT,
            [
                ("general@x", "d1", "final", "m.room.message", ROOM, 1, "$root", "t1", payload, '{"ok": true}', 1),
                ("general@x", "d2", "final", "m.room.message", ROOM, 1, "$elsewhere", "t2", payload, None, 2),
            ],
        )
        db.commit()


def test_read_journal_matches_events_and_thread_and_decodes_json(tmp_path: Path) -> None:
    """Turn records match by event id, journal rows by event id or thread, and JSON columns come back decoded."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    ids = collect_ids(None, event_ids=["$user"], room_id=ROOM, thread_id="$root")
    with _sqlite_query(path) as query:
        results = _read_journal(query, ids, str(path))

    turns = results["turn_records"]
    assert turns.status == "ok"
    assert [item["index_event_id"] for item in turns.items] == ["$user"]
    assert turns.items[0]["record_json"] == {"correlation_id": "$user"}

    events = results["journal_events"].items
    assert [item["event_id"] for item in events] == ["$user", "$late"]
    assert events[0]["source_json"] == {"a": 1}
    outbox = results["delivery_outbox"]
    assert outbox.status == "ok"
    assert [item["delivery_id"] for item in outbox.items] == ["d1"]
    assert outbox.items[0]["payload_json"] == {"msgtype": "m.text", "body": "answer"}
    assert outbox.items[0]["result_json"] == {"ok": True}


def test_read_journal_matches_a_turn_record_by_its_anchor_alone(tmp_path: Path) -> None:
    """A turn record whose index event differs from the reported event still matches through its anchor."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(_TURN_INSERT, ("general", "$index", "$anchor", "{}"))
        db.commit()
    with _sqlite_query(path) as query:
        results = _read_journal(query, collect_ids(None, event_ids=["$anchor"]), str(path))
    assert [item["index_event_id"] for item in results["turn_records"].items] == ["$index"]


def test_read_journal_returns_the_turn_records_of_every_event_in_the_thread(tmp_path: Path) -> None:
    """Naming only the room and thread still returns the turn record of a later event the thread admitted."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(_TURN_INSERT, ("general", "$late", "$late", "{}"))
        db.commit()
    ids = collect_ids(None, room_id=ROOM, thread_id="$root")
    assert ids.event_ids == frozenset({"$root"})
    with _sqlite_query(path) as query:
        results = _read_journal(query, ids, str(path))
    assert [item["event_id"] for item in results["journal_events"].items] == ["$user", "$late"]
    assert sorted(item["index_event_id"] for item in results["turn_records"].items) == ["$late", "$user"]


def test_read_journal_without_thread_matches_event_ids_only(tmp_path: Path) -> None:
    """Without a thread, only the named events are read and the outbox is not queried."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    with _sqlite_query(path) as query:
        results = _read_journal(query, collect_ids(None, event_ids=["$late"], room_id=ROOM), str(path))
    assert [item["event_id"] for item in results["journal_events"].items] == ["$late"]
    assert results["delivery_outbox"].items == []


def test_sqlite_query_is_read_only(tmp_path: Path) -> None:
    """The reader opens the database read-only, so it cannot change the stored data."""
    path = tmp_path / "event_journal.db"
    _seed_journal(path)
    with _sqlite_query(path) as query, pytest.raises(sqlite3.OperationalError):
        query("DELETE FROM turn_records", [])


def test_sqlite_query_accepts_a_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative database path resolves against the working directory instead of raising."""
    _seed_journal(tmp_path / "event_journal.db")
    monkeypatch.chdir(tmp_path)
    with _sqlite_query(Path("event_journal.db")) as query:
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
    with _postgres_query(database_url) as query:
        results = _read_journal(query, ids, "postgres")
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


def _seed_runs(state_root: Path, session_id: str, runs: Sequence[tuple[str, int]]) -> None:
    """Persist one session holding `(run_id, created_at)` runs in the Agno database under `state_root`."""
    storage = create_state_storage(
        state_root.name,
        state_root,
        subdir="sessions",
        session_table=f"{state_root.name}_sessions",
    )
    try:
        seed_session(
            storage,
            AgentSession(
                session_id=session_id,
                agent_id=state_root.name,
                user_id="@alice:example.com",
                runs=[
                    RunOutput(
                        run_id=run_id,
                        agent_id=state_root.name,
                        session_id=session_id,
                        user_id="@alice:example.com",
                        created_at=created_at,
                        status=RunStatus.completed,
                        metadata={"matrix_event_id": "$user"},
                    )
                    for run_id, created_at in runs
                ],
                created_at=1_723_837_600,
                updated_at=1_723_837_600,
            ),
        )
    finally:
        storage.close()


def _seed_agno(storage_root: Path) -> None:
    _seed_runs(storage_root / "agents" / "general", f"{ROOM}:$root", [("run-1", 1_723_837_600)])
    _seed_runs(storage_root / "agents" / "general", "!other:example.com", [("run-other", 1_723_837_600)])


def test_read_agno_runs_matches_session_and_decodes_run_data(tmp_path: Path) -> None:
    """Runs match by session id, and run_data comes back as a nested object."""
    _seed_agno(tmp_path)
    ids = collect_ids(None, room_id=ROOM, thread_id="$root")
    result = _read_agno_runs(tmp_path, ids)
    assert result.status == "ok"
    assert [item["run_id"] for item in result.items] == ["run-1"]
    assert result.items[0]["run_data"]["metadata"] == {"matrix_event_id": "$user"}
    assert result.items[0]["table"] == "general_sessions_runs"


def test_read_agno_runs_matches_a_run_id_alone(tmp_path: Path) -> None:
    """Without a room there is no session to match, and a run is still found by its run ID."""
    _seed_agno(tmp_path)
    ids = dataclasses.replace(collect_ids(None, event_ids=["$x"]), run_ids=frozenset({"run-other"}))
    assert ids.session_ids == frozenset()
    result = _read_agno_runs(tmp_path, ids)
    assert [item["run_id"] for item in result.items] == ["run-other"]


def test_read_agno_runs_keeps_the_newest_runs_across_databases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beyond the cap the oldest runs by created_at are dropped and counted, even within one table inserted out of order.

    The kept runs come oldest first.
    """
    monkeypatch.setattr(debug_report_module, "_MAX_AGNO_RUNS", 2)
    _seed_runs(tmp_path / "agents" / "general", ROOM, [("run-300", 300), ("run-100", 100), ("run-200", 200)])
    _seed_runs(tmp_path / "teams" / "crew", ROOM, [("run-250", 250)])

    result = _read_agno_runs(tmp_path, collect_ids(None, room_id=ROOM))

    assert result.status == "ok"
    assert [item["run_id"] for item in result.items] == ["run-250", "run-300"]
    assert result.dropped == 2
    assert {item["table"] for item in result.items} == {"general_sessions_runs", "crew_sessions_runs"}


def test_read_agno_runs_reads_only_the_runtime_session_database_layouts(tmp_path: Path) -> None:
    """Agent, team, private-instance, and system databases are read; a sessions/ folder in a workspace is not.

    The state roots come from the runtime's own helpers, so the reader's copied layouts are pinned to them.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    root = runtime_paths.storage_root
    state_roots = [
        agent_state_root_path(root, "general"),
        _team_scope_state_root(storage_name="crew", runtime_paths=runtime_paths),
        private_instance_scope_root_path(root, "worker-1") / "helper",
        root / SYSTEM_USAGE_STORAGE_NAME,
    ]
    for state_root in [*state_roots, agent_workspace_root_path(root, "general")]:
        _seed_runs(state_root, f"{ROOM}:$root", [(f"run-{state_root.name}", 1_723_837_600)])

    result = _read_agno_runs(root, collect_ids(None, room_id=ROOM, thread_id="$root"))

    assert result.paths == sorted(str(state_root / "sessions" / f"{state_root.name}.db") for state_root in state_roots)
    assert sorted(item["run_id"] for item in result.items) == ["run-crew", "run-general", "run-helper", "run-system"]


def test_read_agno_runs_reports_missing_databases(tmp_path: Path) -> None:
    """A session root without databases is reported as missing."""
    result = _read_agno_runs(tmp_path, collect_ids(None, room_id=ROOM))
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
    tools = _read_jsonl_records(
        [tracking / "tool_calls.jsonl.1", tracking / "tool_calls.jsonl"],
        ids,
        location=tracking / "tool_calls.jsonl",
    )
    assert [item["tool_name"] for item in tools.items] == ["old", "shell"]

    llm_dir = tmp_path / "logs" / "llm_requests"
    llm = _read_jsonl_records(sorted(llm_dir.glob("llm-requests-*.jsonl")), ids, location=llm_dir)
    assert [item.get("record", "request") for item in llm.items] == ["request", "response"]

    logs = _read_log_lines(sorted((tmp_path / "logs").glob("mindroom_*.log")), ids, location=tmp_path / "logs")
    assert [item["line"] for item in logs.items] == [1, 3]
    assert logs.truncated == 1
    assert len(logs.items[1]["text"]) == debug_report_module._MAX_LOG_LINE_CHARS


def test_jsonl_reader_matches_a_record_by_its_reply_target_alone(tmp_path: Path) -> None:
    """A record without a matching correlation or session still matches through reply_to_event_id."""
    log = tmp_path / "tool_calls.jsonl"
    log.write_text(
        json.dumps({"tool_name": "replied", "correlation_id": "$other", "reply_to_event_id": "$user"})
        + "\n"
        + json.dumps({"tool_name": "unrelated", "correlation_id": "$other", "reply_to_event_id": "$other"})
        + "\n",
        encoding="utf-8",
    )
    result = _read_jsonl_records([log], collect_ids(None, event_ids=["$user"]), location=log)
    assert [item["tool_name"] for item in result.items] == ["replied"]


def test_jsonl_reader_caps_records_keeping_the_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beyond the cap the oldest records are dropped and counted: the newest file's record is the one kept."""
    monkeypatch.setattr(debug_report_module, "_MAX_JSONL_RECORDS", 1)
    _seed_files(tmp_path)
    tracking = tmp_path / "tracking"
    result = _read_jsonl_records(
        [tracking / "tool_calls.jsonl.1", tracking / "tool_calls.jsonl"],
        collect_ids(None, event_ids=["$user"]),
        location=tracking / "tool_calls.jsonl",
    )
    assert [item["tool_name"] for item in result.items] == ["shell"]
    assert result.dropped == 1


def test_log_reader_caps_lines_keeping_the_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Older thread lines beyond the cap are dropped so the reported turn's line, written last, is kept."""
    monkeypatch.setattr(debug_report_module, "_MAX_LOG_LINES", 2)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "mindroom_20261002_120000.log").write_text(
        "older thread_id=$root " + "x" * 5000 + "\n" + "older thread_id=$root\n",
        encoding="utf-8",
    )
    (logs / "mindroom_20261003_120000.log").write_text(
        "older thread_id=$root\nreported event_id=$reported thread_id=$root\n",
        encoding="utf-8",
    )

    result = _read_log_lines(
        sorted(logs.glob("mindroom_*.log")),
        collect_ids(None, event_ids=["$reported"], room_id=ROOM, thread_id="$root"),
        location=logs,
    )

    assert [(item["file"], item["line"]) for item in result.items] == [
        ("mindroom_20261003_120000.log", 1),
        ("mindroom_20261003_120000.log", 2),
    ]
    assert result.items[-1]["text"] == "reported event_id=$reported thread_id=$root"
    assert result.dropped == 2
    # The truncated line was dropped, so no kept line is truncated.
    assert result.truncated == 0


def test_build_debug_report_marks_missing_sources(tmp_path: Path) -> None:
    """An install with no data at all reports every source as missing instead of failing."""
    document = build_debug_report(_sources(tmp_path), collect_ids(None, event_ids=["$x"]), generated_at="now")
    assert {name: source["status"] for name, source in document["sources"].items()} == dict.fromkeys(
        document["sources"],
        "missing",
    )


def _write_corrupt_database(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a sqlite database" * 100)


def test_read_agno_runs_keeps_healthy_databases_when_one_fails(tmp_path: Path) -> None:
    """A corrupt session database is named in `error` while the runs of a healthy one are still returned."""
    _seed_agno(tmp_path)
    broken = tmp_path / "agents" / "x" / "sessions" / "x.db"
    _write_corrupt_database(broken)

    result = _read_agno_runs(tmp_path, collect_ids(None, room_id=ROOM, thread_id="$root"))

    assert result.status == "ok"
    assert [item["run_id"] for item in result.items] == ["run-1"]
    assert result.error is not None
    assert result.error.startswith(f"{broken}: DatabaseError")
    assert "general.db" not in result.error


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


def test_build_debug_report_marks_journal_sources_with_the_given_journal_error(tmp_path: Path) -> None:
    """A journal that could not be located is an error on all three sources, and the SQLite file is not read."""
    _seed_journal(tmp_path / "tracking" / "event_journal.db")
    _seed_files(tmp_path)
    sources = dataclasses.replace(_sources(tmp_path), journal_error="no database URL")

    document = build_debug_report(sources, collect_ids(None, event_ids=["$user"]), generated_at="now")

    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert document["sources"][name]["status"] == "error"
        assert document["sources"][name]["error"] == "no database URL"
        assert document["sources"][name]["items"] == []
    assert document["sources"]["tool_calls"]["status"] == "ok"


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


def test_build_debug_report_marks_journal_sources_when_psycopg_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the postgres extra the journal sources name the missing dependency and the others are still read."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    _seed_files(tmp_path)

    document = build_debug_report(
        _sources(tmp_path, journal_postgres_url="postgresql://nobody@127.0.0.1:1/none"),
        collect_ids(_report(), event_ids=["$user"]),
        generated_at="now",
    )

    sources = document["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert sources[name]["status"] == "error"
        assert "psycopg" in sources[name]["error"]
        assert "mindroom[postgres]" in sources[name]["error"]
        assert sources[name]["paths"] == ["postgres"]
    assert sources["tool_calls"]["status"] == "ok"
    assert [item["tool_name"] for item in sources["tool_calls"]["items"]] == ["old", "shell"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_build_debug_report_survives_an_unreadable_tracking_directory(tmp_path: Path) -> None:
    """An unreadable parent directory makes `is_file()` raise; the affected sources error and the rest still report."""
    _seed_journal(tmp_path / "tracking" / "event_journal.db")
    _seed_files(tmp_path)
    tracking = tmp_path / "tracking"
    tracking.chmod(0o000)
    try:
        document = build_debug_report(_sources(tmp_path), collect_ids(_report()), generated_at="now")
    finally:
        tracking.chmod(0o700)

    sources = document["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox", "tool_calls"):
        assert sources[name]["status"] == "error"
        assert "PermissionError" in sources[name]["error"]
    assert sources["llm_requests"]["status"] == "ok"
    assert sources["log_lines"]["status"] == "ok"


def _write_config(path: Path) -> None:
    path.write_text(
        "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-5-5\n"
        "agents:\n  general:\n    display_name: General Agent\n    model: default\n"
        "router:\n  model: default\n"
        "matrix_space:\n  enabled: false\n"
        "authorization:\n  global_users: []\n",
        encoding="utf-8",
    )


def test_cli_writes_backend_report_for_a_chat_bug_report(tmp_path: Path) -> None:
    """A bug report file yields a JSON document with every source and a per-source summary on stderr."""
    config = tmp_path / "config.yaml"
    _write_config(config)
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    _seed_agno(storage)
    _seed_files(storage)
    report = tmp_path / "bug-report.json"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    output = tmp_path / "backend.json"

    result = runner.invoke(app, ["debug-report", str(report), "-c", str(config), "-s", str(storage), "-o", str(output)])

    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["identifiers"]["roomId"] == ROOM
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
    assert "agno_runs: ok" in result.output


def test_cli_shows_the_error_of_a_source_that_could_not_be_read(tmp_path: Path) -> None:
    """A failing source is summarized with its error on stderr while the other sources are still collected."""
    config = tmp_path / "config.yaml"
    _write_config(config)
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    _write_corrupt_database(storage / "agents" / "x" / "sessions" / "x.db")
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-r", ROOM, "-c", str(config), "-s", str(storage), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["sources"]["agno_runs"]["status"] == "error"
    assert [item["index_event_id"] for item in document["sources"]["turn_records"]["items"]] == ["$user"]
    assert f"agno_runs: error, 0 items ({document['sources']['agno_runs']['error']})" in result.output
    assert "turn_records: ok, 1 items" in result.output


def test_cli_shows_the_error_of_a_source_that_is_ok_with_a_partial_failure(tmp_path: Path) -> None:
    """One unreadable session database leaves agno_runs ok, and the stderr summary still names what failed."""
    config = tmp_path / "config.yaml"
    _write_config(config)
    storage = tmp_path / "storage"
    _seed_agno(storage)
    _write_corrupt_database(storage / "agents" / "x" / "sessions" / "x.db")
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-r", ROOM, "-t", "$root", "-c", str(config), "-s", str(storage), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    agno_runs = json.loads(output.read_text(encoding="utf-8"))["sources"]["agno_runs"]
    assert agno_runs["status"] == "ok"
    assert [item["run_id"] for item in agno_runs["items"]] == ["run-1"]
    assert f"agno_runs: ok, 1 items ({agno_runs['error']})" in result.output


def test_cli_reports_journal_errors_when_the_postgres_url_is_not_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A postgres journal whose DSN resolves from nowhere is an error, never a silent read of the SQLite file."""
    monkeypatch.delenv("MINDROOM_EVENT_CACHE_DATABASE_URL", raising=False)
    config = tmp_path / "config.yaml"
    _write_config(config)
    config.write_text(config.read_text(encoding="utf-8") + "event_journal:\n  backend: postgres\n", encoding="utf-8")
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    _seed_files(storage)
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-c", str(config), "-s", str(storage), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    sources = json.loads(output.read_text(encoding="utf-8"))["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert sources[name]["status"] == "error"
        assert sources[name]["items"] == []
        assert "MINDROOM_EVENT_CACHE_DATABASE_URL" in sources[name]["error"]
    assert sources["tool_calls"]["status"] == "ok"
    assert "Warning:" not in result.output
    assert "turn_records: error, 0 items (PostgreSQL event journal requires" in result.output


def _write_request_log(directory: Path, request_log_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "llm-requests-2026-10-03.jsonl").write_text(
        json.dumps({"request_log_id": request_log_id, "correlation_id": "$user"}) + "\n",
        encoding="utf-8",
    )


def test_cli_reads_a_relative_request_log_dir_from_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative debug.llm_request_log_dir is read from the working directory, where the runtime writes it."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    _write_config(config)
    config.write_text(
        config.read_text(encoding="utf-8") + "debug:\n  llm_request_log_dir: request-logs\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "workdir"
    _write_request_log(workdir / "request-logs", "in-working-directory")
    _write_request_log(config_dir / "request-logs", "beside-config")
    monkeypatch.chdir(workdir)
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-c", str(config), "-s", str(tmp_path / "storage"), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    llm_requests = json.loads(output.read_text(encoding="utf-8"))["sources"]["llm_requests"]
    assert [item["request_log_id"] for item in llm_requests["items"]] == ["in-working-directory"]


def test_cli_reads_the_config_without_migrating_it(tmp_path: Path) -> None:
    """A config the runtime would migrate on load (retired authorization.global_users) is left byte-for-byte as is."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    _write_config(config)
    original = config.read_bytes()
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-c", str(config), "-s", str(storage), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert config.read_bytes() == original
    assert [path.name for path in config_dir.iterdir()] == ["config.yaml"]
    turn_records = json.loads(output.read_text(encoding="utf-8"))["sources"]["turn_records"]
    assert [item["index_event_id"] for item in turn_records["items"]] == ["$user"]


def test_cli_reads_settings_from_a_config_the_runtime_would_reject(tmp_path: Path) -> None:
    """An invalid but parseable config still supplies the request-log directory, and the report is written."""
    config = tmp_path / "config.yaml"
    request_logs = tmp_path / "request-logs"
    config.write_text(
        "agents:\n  general:\n    model: no-such-model\nnot_a_config_section: 1\n"
        f"debug:\n  llm_request_log_dir: {request_logs}\n",
        encoding="utf-8",
    )
    _write_request_log(request_logs, "from-invalid-config")
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-c", str(config), "-s", str(tmp_path / "storage"), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    llm_requests = json.loads(output.read_text(encoding="utf-8"))["sources"]["llm_requests"]
    assert [item["request_log_id"] for item in llm_requests["items"]] == ["from-invalid-config"]
    assert "Warning" not in result.output


def test_cli_reports_journal_errors_when_the_event_journal_section_is_invalid(tmp_path: Path) -> None:
    """An event_journal section the runtime would reject is a journal error, never a guess at the SQLite file."""
    config = tmp_path / "config.yaml"
    config.write_text("event_journal:\n  backend: mysql\n", encoding="utf-8")
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    _seed_files(storage)
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-c", str(config), "-s", str(storage), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    sources = json.loads(output.read_text(encoding="utf-8"))["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert sources[name]["status"] == "error"
        assert "EventJournalConfig" in sources[name]["error"]
        assert sources[name]["items"] == []
    assert sources["tool_calls"]["status"] == "ok"


def test_cli_fails_when_the_given_config_does_not_exist(tmp_path: Path) -> None:
    """A --config path that does not exist is an error instead of a silent fall back to default locations."""
    missing = tmp_path / "missing.yaml"
    result = runner.invoke(app, ["debug-report", "-e", "$user", "-c", str(missing), "-s", str(tmp_path)])
    assert result.exit_code == 1
    assert f"config file not found: {missing}" in result.output


def test_cli_notes_default_locations_when_the_default_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --config and without a config at the default path, the default journal is read and stderr says so."""
    missing = tmp_path / "absent.yaml"
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(missing))
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    output = tmp_path / "backend.json"

    result = runner.invoke(app, ["debug-report", "-e", "$user", "-s", str(storage), "-o", str(output)])

    assert result.exit_code == 0, result.output
    assert f"Note: no config at {missing}; using default storage locations." in result.output
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["type"] == "io.mindroom.debug_report"
    assert document["sources"]["turn_records"]["status"] == "ok"
    assert [item["index_event_id"] for item in document["sources"]["turn_records"]["items"]] == ["$user"]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("agents: [unclosed\n", id="unparseable"),
        pytest.param("- not\n- a mapping\n", id="not-a-mapping"),
    ],
)
def test_cli_reports_journal_errors_when_the_config_exists_but_cannot_be_read(tmp_path: Path, content: str) -> None:
    """A config that exists but is unreadable may select PostgreSQL, so the SQLite file is never read in its place."""
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")
    _seed_files(storage)
    output = tmp_path / "backend.json"

    result = runner.invoke(
        app,
        ["debug-report", "-e", "$user", "-c", str(config), "-s", str(storage), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    sources = json.loads(output.read_text(encoding="utf-8"))["sources"]
    for name in ("turn_records", "journal_events", "delivery_outbox"):
        assert sources[name]["status"] == "error"
        assert sources[name]["items"] == []
        assert sources[name]["error"].startswith("config could not be read: ")
    # The request-log directory stays at its default, so the sources that do not depend on the config are still read.
    assert sources["llm_requests"]["status"] == "ok"
    assert [item["request_log_id"] for item in sources["llm_requests"]["items"]] == ["r1"]
    assert sources["tool_calls"]["status"] == "ok"
    assert "turn_records: error, 0 items (config could not be read: " in result.output


def test_cli_prints_the_document_to_stdout_without_an_output_file(tmp_path: Path) -> None:
    """Without --output the JSON goes to stdout."""
    config = tmp_path / "config.yaml"
    _write_config(config)
    storage = tmp_path / "storage"
    _seed_journal(storage / "tracking" / "event_journal.db")

    result = runner.invoke(app, ["debug-report", "-e", "$user", "-c", str(config), "-s", str(storage)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["type"] == "io.mindroom.debug_report"


@pytest.mark.parametrize("to_file", [True, False], ids=["output-file", "stdout"])
def test_cli_writes_valid_json_for_a_lone_surrogate_in_stored_json(tmp_path: Path, *, to_file: bool) -> None:
    """A lone-surrogate escape the journal stored survives as the same escape, in an output file and on stdout."""
    config = tmp_path / "config.yaml"
    _write_config(config)
    storage = tmp_path / "storage"
    journal = storage / "tracking" / "event_journal.db"
    _seed_journal(journal)
    with closing(sqlite3.connect(journal)) as db:
        source = '{"body": "broken \\ud83d emoji"}'
        db.execute(_JOURNAL_INSERT, ("general@x", "$garbled", ROOM, "", "message", "@a:x", 4, source, 1, "settled"))
        db.commit()
    output = tmp_path / "backend.json"
    args = ["debug-report", "-e", "$garbled", "-c", str(config), "-s", str(storage)]

    result = runner.invoke(app, [*args, "-o", str(output)] if to_file else args)

    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8") if to_file else result.stdout
    assert "\\ud83d" in text
    events = json.loads(text)["sources"]["journal_events"]["items"]
    assert [event["source_json"] for event in events] == [{"body": "broken \ud83d emoji"}]


def test_build_debug_report_keeps_values_the_json_decoder_refuses(tmp_path: Path) -> None:
    """A 5,000-digit integer or deep nesting keeps a column's raw text or skips a JSONL line instead of aborting."""
    huge = "{" + '"n": ' + "1" * 5000 + "}"
    deep = "[" * 100_000 + "]" * 100_000
    journal = tmp_path / "tracking" / "event_journal.db"
    _seed_journal(journal)
    with closing(sqlite3.connect(journal)) as db:
        db.execute(_TURN_INSERT, ("general", "$huge", "$huge", huge))
        db.execute(_TURN_INSERT, ("general", "$deep", "$deep", deep))
        db.commit()
    _seed_files(tmp_path)
    with (tmp_path / "tracking" / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"tool_name": "huge", "correlation_id": "$user", "n": ' + "1" * 5000 + "}\n")
        handle.write('{"tool_name": "deep", "correlation_id": "$user", "x": ' + deep + "}\n")

    document = build_debug_report(
        _sources(tmp_path),
        collect_ids(None, event_ids=["$user", "$huge", "$deep"]),
        generated_at="now",
    )

    sources = document["sources"]
    turns = {item["index_event_id"]: item["record_json"] for item in sources["turn_records"]["items"]}
    assert turns == {"$deep": deep, "$huge": huge, "$user": {"correlation_id": "$user"}}
    assert sources["tool_calls"]["status"] == "ok"
    assert [item["tool_name"] for item in sources["tool_calls"]["items"]] == ["old", "shell"]


@pytest.mark.parametrize("args", [[], ["--thread", "~local"]], ids=["nothing", "local-echo-thread"])
def test_cli_requires_an_identifier(tmp_path: Path, args: list[str]) -> None:
    """With nothing to look up, such as only a local echo that never reached the server, the command fails."""
    result = runner.invoke(app, ["debug-report", *args, "-s", str(tmp_path)])
    assert result.exit_code == 1
    assert "at least one of --event, --room, --thread" in result.output


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\xff\xfe\x00\x80 not text", id="binary"),
        pytest.param(b'{"type": "io.mindroom.bug_report", "n": ' + b"1" * 5000 + b"}", id="huge-integer"),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deep-nesting"),
    ],
)
def test_cli_rejects_an_unreadable_bug_report(tmp_path: Path, content: bytes) -> None:
    """A file that is not UTF-8 text, or JSON the decoder refuses, is refused with a message instead of a traceback."""
    report = tmp_path / "bug-report.json"
    report.write_bytes(content)
    result = runner.invoke(app, ["debug-report", str(report), "-s", str(tmp_path)])
    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_cli_rejects_a_file_that_is_not_a_bug_report(tmp_path: Path) -> None:
    """A JSON file of another type is refused instead of producing an empty report."""
    other = tmp_path / "other.json"
    other.write_text('{"type": "something"}', encoding="utf-8")
    result = runner.invoke(app, ["debug-report", str(other), "-s", str(tmp_path)])
    assert result.exit_code == 1
    assert "not a MindRoom Chat bug report" in result.output
