"""Pending auto-resume ledger tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.message_target import MessageTarget
from mindroom.pending_resume_store import (
    PendingResumeRecord,
    PendingResumeTracker,
    _upsert_pending_resume_record,
    discard_pending_resume_records,
    load_pending_resume_records,
    pending_resume_ledger_path,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ROOM_ID = "!room:example.com"
THREAD_ID = "$thread"


def _record(
    *,
    agent_name: str = "test_agent",
    thread_id: str = THREAD_ID,
    target_event_id: str = "$target",
    requester_user_id: str | None = "@user:example.com",
    created_at_ms: int = 1_000,
) -> PendingResumeRecord:
    return PendingResumeRecord(
        agent_name=agent_name,
        room_id=ROOM_ID,
        thread_id=thread_id,
        target_event_id=target_event_id,
        requester_user_id=requester_user_id,
        created_at_ms=created_at_ms,
    )


def test_pending_resume_ledger_path_lives_under_tracking(tmp_path: Path) -> None:
    """The ledger belongs next to the other durable turn-tracking state."""
    runtime_paths = test_runtime_paths(tmp_path)
    ledger_path = pending_resume_ledger_path(runtime_paths)
    assert ledger_path == runtime_paths.storage_root / "tracking" / "pending_resumes.json"


def test_upsert_and_load_round_trip(tmp_path: Path) -> None:
    """Stored records load back keyed by conversation."""
    ledger_path = tmp_path / "tracking" / "pending_resumes.json"
    record = _record()

    _upsert_pending_resume_record(ledger_path, record)

    assert load_pending_resume_records(ledger_path) == {record.key: record}


def test_upsert_replaces_record_for_same_conversation(tmp_path: Path) -> None:
    """One conversation keeps only its latest in-flight turn."""
    ledger_path = tmp_path / "pending_resumes.json"
    _upsert_pending_resume_record(ledger_path, _record(target_event_id="$old", created_at_ms=1_000))
    replacement = _record(target_event_id="$new", created_at_ms=2_000)

    _upsert_pending_resume_record(ledger_path, replacement)

    assert load_pending_resume_records(ledger_path) == {replacement.key: replacement}


def test_records_for_distinct_agents_and_threads_coexist(tmp_path: Path) -> None:
    """Different agents and threads keep independent records."""
    ledger_path = tmp_path / "pending_resumes.json"
    first = _record()
    second = _record(agent_name="other")
    third = _record(thread_id="$other-thread")

    for record in (first, second, third):
        _upsert_pending_resume_record(ledger_path, record)

    assert load_pending_resume_records(ledger_path) == {
        first.key: first,
        second.key: second,
        third.key: third,
    }


def test_discard_removes_only_matching_records(tmp_path: Path) -> None:
    """Discarding consumes exactly the evaluated record versions."""
    ledger_path = tmp_path / "pending_resumes.json"
    kept = _record(agent_name="other")
    dropped = _record()
    _upsert_pending_resume_record(ledger_path, kept)
    _upsert_pending_resume_record(ledger_path, dropped)

    missing = _record(agent_name="missing", thread_id="$z")
    discard_pending_resume_records(ledger_path, (record for record in (dropped, missing)))

    assert load_pending_resume_records(ledger_path) == {kept.key: kept}


def test_discard_preserves_a_newer_record_for_the_same_conversation(tmp_path: Path) -> None:
    """Settling an old attempt must not delete a replacement written under the same key."""
    ledger_path = tmp_path / "pending_resumes.json"
    old = _record(target_event_id="$old", created_at_ms=1_000)
    replacement = _record(target_event_id="$new", created_at_ms=2_000)
    _upsert_pending_resume_record(ledger_path, old)
    _upsert_pending_resume_record(ledger_path, replacement)

    discard_pending_resume_records(ledger_path, (old,))

    assert load_pending_resume_records(ledger_path) == {replacement.key: replacement}


def test_load_tolerates_missing_corrupt_and_malformed_ledgers(tmp_path: Path) -> None:
    """A missing, corrupt, or malformed ledger never breaks startup."""
    missing = tmp_path / "missing.json"
    assert load_pending_resume_records(missing) == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert load_pending_resume_records(corrupt) == {}

    malformed = tmp_path / "malformed.json"
    malformed.write_text('["list", "payload"]', encoding="utf-8")
    assert load_pending_resume_records(malformed) == {}


def test_load_drops_invalid_and_mismatched_entries(tmp_path: Path) -> None:
    """Entries with missing fields or mismatched keys are ignored."""
    ledger_path = tmp_path / "pending_resumes.json"
    valid = _record()
    ledger_path.write_text(
        json.dumps(
            {
                valid.key: valid.to_payload(),
                "junk": {"agent_name": "a"},
                "wrong|key": _record(agent_name="b").to_payload(),
            },
        ),
        encoding="utf-8",
    )

    assert load_pending_resume_records(ledger_path) == {valid.key: valid}


def test_tracker_records_thread_turn_and_discards_on_terminal_settle(tmp_path: Path) -> None:
    """The tracker persists in-flight turns and drops them once visibly settled."""
    ledger_path = tmp_path / "pending_resumes.json"
    tracker = PendingResumeTracker(ledger_path=ledger_path, agent_name="test_agent")
    target = MessageTarget.resolve(ROOM_ID, THREAD_ID, "$reply")

    record = tracker.note_started("$target", target=target, requester_user_id="@user:example.com")

    records = load_pending_resume_records(ledger_path)
    assert set(records) == {f"test_agent|{ROOM_ID}|{THREAD_ID}"}
    record = records[f"test_agent|{ROOM_ID}|{THREAD_ID}"]
    assert record.target_event_id == "$target"
    assert record.requester_user_id == "@user:example.com"
    assert record.created_at_ms > 0

    tracker.note_settled(record, resumable=True)
    assert set(load_pending_resume_records(ledger_path)) == {f"test_agent|{ROOM_ID}|{THREAD_ID}"}

    tracker.note_settled(record, resumable=False)
    assert load_pending_resume_records(ledger_path) == {}


def test_tracker_skips_room_mode_turns(tmp_path: Path) -> None:
    """Room-level turns have no thread to resume, so nothing is persisted."""
    ledger_path = tmp_path / "pending_resumes.json"
    tracker = PendingResumeTracker(ledger_path=ledger_path, agent_name="test_agent")
    target = MessageTarget.resolve(ROOM_ID, None, "$reply", room_mode=True)

    record = tracker.note_started("$target", target=target, requester_user_id="@user:example.com")
    tracker.note_settled(record, resumable=False)

    assert not ledger_path.exists()


def test_tracker_contains_ledger_write_failures(tmp_path: Path) -> None:
    """Ledger I/O failures must never escape into the response turn."""
    blocked_path = tmp_path / "blocked"
    blocked_path.mkdir()
    tracker = PendingResumeTracker(ledger_path=blocked_path, agent_name="test_agent")
    target = MessageTarget.resolve(ROOM_ID, THREAD_ID, "$reply")

    record = tracker.note_started("$target", target=target, requester_user_id=None)
    tracker.note_settled(record, resumable=False)
