"""Tests for `mindroom debug-report`."""

from __future__ import annotations

from mindroom.debug_report import collect_ids

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
