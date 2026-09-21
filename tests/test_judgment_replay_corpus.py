"""Synthetic evidence tests; no real conversation bodies."""

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.judgment_replay.corpus import extract_corpus, read_records, verify_manifest


def _log(root: Path, rows: list[dict]) -> None:
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mindroom_test.log").write_text("\n".join(json.dumps(row) for row in rows))


def _notice(turn: str, timestamp: str, thread: str = "thread") -> dict:
    return {
        "event": "queued_message_notice_injected",
        "response_turn_id": turn,
        "timestamp": timestamp,
        "room_id": "room",
        "thread_id": thread,
        "reply_to_event_id": "trigger",
        "session_id": "session",
        "agent_id": "agent",
    }


def test_exact_events_and_persisted_marker_do_not_create_turns(tmp_path: Path) -> None:
    """Only exact log events seed truth, and historical notices are not fresh boundaries."""
    source = tmp_path / "source"
    _log(
        source,
        [
            _notice("owner", "2026-09-01T01:00:00Z"),
            {"event": "quoted queued_message_notice_injected", "response_turn_id": "forged"},
        ],
    )
    requests = source / "logs/llm_requests"
    requests.mkdir()
    common = {
        "timestamp": "2026-09-01T01:00:01Z",
        "room_id": "room",
        "thread_id": "thread",
        "reply_to_event_id": "trigger",
        "agent_id": "agent",
        "session_id": "session",
    }
    rows = [
        {
            **common,
            "messages": [
                {
                    "role": "user",
                    "provider_data": {
                        "mindroom_queued_message_notice": True,
                        "mindroom_queued_message_notice_response_turn_id": "owner",
                    },
                },
            ],
        },
        {
            **common,
            "reply_to_event_id": "later",
            "messages": [
                {
                    "role": "user",
                    "provider_data": {
                        "mindroom_queued_message_notice": "persisted",
                        "mindroom_queued_message_notice_response_turn_id": "owner",
                    },
                },
            ],
        },
    ]
    (requests / "day.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    evidence = tmp_path / "evidence"
    report = extract_corpus(source, evidence)
    assert report["notice_events"] == 1
    assert report["notice_turns"] == 1
    assert report["coverage"]["provider_requests"] == 1
    assert report["provider_boundary_observations"] == 1
    cases = [record for _, record in read_records(evidence / "cases.jsonl")]
    assert cases[0]["observed_provider_boundaries"] == 1
    assert cases[0]["pending_membership_known"] is False
    assert cases[0]["tier"] == "partial"
    assert "pending_membership_unobservable" in cases[0]["exclusions"]
    assert verify_manifest(evidence)["files"] == 2
    assert (evidence.stat().st_mode & 0o077) == 0
    assert all((p.stat().st_mode & 0o077) == 0 for p in evidence.rglob("*") if p.is_file())


def test_symlinks_refused_and_frozen_integrity_checked(tmp_path: Path) -> None:
    """Neither source links nor modified frozen files are trusted."""
    source = tmp_path / "source"
    _log(source, [_notice("owner", "2026-09-01T01:00:00Z")])
    (source / "logs/mindroom_link.log").symlink_to(source / "logs/mindroom_test.log")
    evidence = tmp_path / "evidence"
    report = extract_corpus(source, evidence)
    assert report["omissions"]["symlink"] == 1
    frozen = evidence / "corpus/logs/mindroom_test.log"
    frozen.write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        verify_manifest(evidence)


def test_json_framing_quarantines_tail_without_nested_recovery(tmp_path: Path) -> None:
    """Complete adjacent objects work; malformed tails cannot promote nested objects."""
    path = tmp_path / "records.jsonl"
    path.write_text('{"id":1}{"id":2}\nBROKEN {"id":3}\n{"id":4}\n')
    errors = []
    rows = list(read_records(path, errors=errors))
    assert [row["id"] for _, row in rows] == [1, 2]
    assert len(errors) == 1
    assert errors[0]["reason"] == "malformed_tail"


def test_chronological_threads_do_not_leak_across_splits(tmp_path: Path) -> None:
    """Spanning threads are excluded from later splits instead of leaking."""
    source = tmp_path / "source"
    _log(
        source,
        [
            _notice("one", "2026-01-01T00:00:00Z", "spanning"),
            _notice("two", "2026-09-01T00:00:00Z", "spanning"),
            _notice("three", "2026-08-01T00:00:00Z", "validation"),
            _notice("four", "2026-09-03T00:00:00Z", "holdout"),
        ],
    )
    evidence = tmp_path / "evidence"
    extract_corpus(source, evidence, validation_after="2026-07-01T00:00:00Z", holdout_after="2026-09-01T00:00:00Z")
    cases = [row for _, row in read_records(evidence / "cases.jsonl")]
    assert {row["split"] for row in cases if row["thread_key"] == cases[0]["thread_key"]} == {"excluded"}
    assert [row["split"] for row in cases if row["response_turn_id"] in {"three", "four"}] == ["validation", "holdout"]


def test_export_without_trigger_does_not_count_as_joined(tmp_path: Path) -> None:
    """A stale same-thread export is an inventory item, not case coverage."""
    source = tmp_path / "source"
    _log(source, [_notice("owner", "2026-09-01T01:00:00Z")])
    exports = source / "agents/agent/workspace/thread_exports/room"
    exports.mkdir(parents=True)
    (exports / "thread.yaml").write_text(
        json.dumps(
            {
                "version": 1,
                "room": {"id": "room"},
                "thread": {"id": "thread"},
                "messages": [{"event_id": "other", "body": "Synthetic text"}],
            },
        ),
    )
    report = extract_corpus(source, tmp_path / "evidence")
    assert report["coverage"]["exports"] == 0
    assert report["tiers"] == {"unobservable": 1}


def test_unjoinable_events_count_and_failed_snapshots_leave_no_file(tmp_path: Path) -> None:
    """Invalid correlation and invalid SQLite are explicit exclusions."""
    source = tmp_path / "source"
    _log(source, [{"event": "queued_message_notice_injected", "timestamp": "invalid"}])
    sessions = source / "agents/agent/sessions"
    sessions.mkdir(parents=True)
    (sessions / "broken.db").write_text("not sqlite")
    evidence = tmp_path / "evidence"
    report = extract_corpus(source, evidence)
    assert report["notice_events"] == 1
    assert report["notice_turns"] == 0
    assert report["omissions"]["snapshot_failed"] == 1
    assert not (evidence / "corpus/agents/agent/sessions/broken.db").exists()
    verify_manifest(evidence)


def test_parser_exclusions_have_file_and_byte_offset(tmp_path: Path) -> None:
    """Corruption provenance identifies its exact frozen source."""
    path = tmp_path / "broken.jsonl"
    path.write_text("{}\ninvalid")
    errors = []
    list(read_records(path, errors=errors))
    assert errors == [{"path": str(path), "offset": 3, "reason": "malformed_tail"}]


def test_sqlite_backup_and_ownership_joins_preserve_read_only_evidence(tmp_path: Path) -> None:
    """WAL snapshots retain exact marker positions and journal receipt order."""
    source = tmp_path / "source"
    _log(source, [_notice("owner", "2026-09-01T01:00:00Z")])
    sessions = source / "agents/agent/sessions"
    sessions.mkdir(parents=True)
    session_path = sessions / "agent.db"
    session = sqlite3.connect(session_path)
    session.execute("PRAGMA journal_mode=WAL")
    session.execute("CREATE TABLE agent_sessions_runs (run_id TEXT, session_id TEXT, run_data TEXT)")
    session.execute(
        "INSERT INTO agent_sessions_runs VALUES (?, ?, ?)",
        (
            "run",
            "session",
            json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "provider_data": {
                                "mindroom_queued_message_notice": "persisted",
                                "mindroom_queued_message_notice_response_turn_id": "owner",
                            },
                        },
                    ],
                },
            ),
        ),
    )
    session.commit()
    tracking = source / "tracking"
    tracking.mkdir()
    with sqlite3.connect(tracking / "event_journal.db") as journal:
        journal.execute(
            "CREATE TABLE response_attempts (principal_id TEXT, selected_receipt_order INTEGER, "
            "driving_event_id TEXT, room_id TEXT, entity_name TEXT)",
        )
        journal.execute("CREATE TABLE journal_events (principal_id TEXT, event_id TEXT, thread_id TEXT)")
        journal.execute("INSERT INTO response_attempts VALUES ('principal', 7, 'trigger', 'room', 'agent')")
        journal.execute("INSERT INTO journal_events VALUES ('principal', 'trigger', 'thread')")
    evidence = tmp_path / "evidence"
    try:
        report = extract_corpus(source, evidence)
    finally:
        session.close()
    assert report["coverage"]["sessions"] == 1
    assert report["coverage"]["journal"] == 1
    case = next(read_records(evidence / "cases.jsonl"))[1]
    assert case["sources"]["journal"][0]["selected_receipt_order"] == 7
    assert case["pending_membership_known"] is False
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert {item["snapshot_method"] for item in manifest["sources"]} == {"sqlite_backup", "stable_copy"}
    verify_manifest(evidence)


def test_source_containment_normalizes_parent_components(tmp_path: Path) -> None:
    """Spelling a source with '..' cannot permit evidence inside live exports."""
    source = tmp_path / "source"
    _log(source, [])
    alias = source / ".." / "source"
    evidence = source / "agents/agent/workspace/thread_exports/snapshot"
    with pytest.raises(ValueError, match="outside the live corpus"):
        extract_corpus(alias, evidence)
    assert not evidence.exists()
