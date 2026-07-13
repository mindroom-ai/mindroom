"""Pending auto-resume ledger tests."""

from __future__ import annotations

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
    target_event_id: str = "$target",
    created_at_ms: int = 1_000,
) -> PendingResumeRecord:
    return PendingResumeRecord(
        agent_name=agent_name,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        target_event_id=target_event_id,
        requester_user_id="@user:example.com",
        created_at_ms=created_at_ms,
    )


def test_store_replaces_by_conversation_and_discards_exact_versions(tmp_path: Path) -> None:
    """An old settlement cannot delete a newer same-conversation record."""
    path = tmp_path / "pending_resumes.json"
    old = _record(target_event_id="$old")
    replacement = _record(target_event_id="$new", created_at_ms=2_000)
    other = _record(agent_name="other")

    for record in (old, other, replacement):
        _upsert_pending_resume_record(path, record)

    discard_pending_resume_records(path, (record for record in (old,)))
    assert load_pending_resume_records(path) == {replacement.key: replacement, other.key: other}

    discard_pending_resume_records(path, (replacement,))
    assert load_pending_resume_records(path) == {other.key: other}

    path.write_text("{not json", encoding="utf-8")
    assert load_pending_resume_records(path) == {}


def test_tracker_records_only_threaded_turns_and_settles_terminal_outcomes(tmp_path: Path) -> None:
    """Tracker lifecycle leaves only restart-resumable threaded attempts."""
    path = pending_resume_ledger_path(test_runtime_paths(tmp_path))
    tracker = PendingResumeTracker(path, "test_agent")
    target = MessageTarget.resolve(ROOM_ID, THREAD_ID, "$reply")

    record = tracker.note_started("$target", target=target, requester_user_id="@user:example.com")
    assert record is not None
    tracker.note_settled(record, resumable=True)
    assert load_pending_resume_records(path) == {record.key: record}

    tracker.note_settled(record, resumable=False)
    assert load_pending_resume_records(path) == {}

    room_target = MessageTarget.resolve(ROOM_ID, None, "$reply", room_mode=True)
    assert tracker.note_started("$room", target=room_target, requester_user_id=None) is None

    blocked_path = tmp_path / "blocked"
    blocked_path.mkdir()
    blocked_tracker = PendingResumeTracker(blocked_path, "test_agent")
    blocked_record = blocked_tracker.note_started("$target", target=target, requester_user_id=None)
    blocked_tracker.note_settled(blocked_record, resumable=False)
