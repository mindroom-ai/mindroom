"""Tests for handled turn persistence and lookup."""

from __future__ import annotations

import fcntl
import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

from mindroom.handled_turns import HandledTurnLedger, HandledTurnRecord, HandledTurnState
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for ledger tests."""
    return tmp_path


def _write_responses_file(
    tracker: HandledTurnLedger,
    responses: dict[str, dict[str, object]],
) -> None:
    """Seed the persisted ledger file for tests that exercise reload semantics."""
    tracker._responses_file.write_text(json.dumps(responses), encoding="utf-8")


def _record_handled_turn(
    tracker: HandledTurnLedger,
    source_event_ids: list[str],
    *,
    response_event_id: str | None = None,
    source_event_prompts: dict[str, str] | None = None,
    response_owner: str | None = None,
    history_scope: HistoryScope | None = None,
    conversation_target: MessageTarget | None = None,
) -> None:
    """Record one normalized handled turn through the typed carrier."""
    tracker.record_handled_turn(
        HandledTurnState.create(
            source_event_ids,
            response_event_id=response_event_id,
            source_event_prompts=source_event_prompts,
            response_owner=response_owner,
            history_scope=history_scope,
            conversation_target=conversation_target,
        ),
    )


def test_handled_turn_ledger_init(temp_dir: Path) -> None:
    """Initialization should create an empty in-memory ledger."""
    tracker = HandledTurnLedger("test_agent", base_path=temp_dir)

    assert tracker.agent_name == "test_agent"
    assert tracker._responses == {}
    assert tracker._responses_file == temp_dir / "test_agent_responded.json"


def test_has_responded_empty(temp_dir: Path) -> None:
    """Unknown source events should not be marked handled."""
    tracker = HandledTurnLedger("test_empty", base_path=temp_dir)

    assert not tracker.has_responded("event123")
    assert tracker.get_turn_record("event123") is None


def test_handled_turn_state_normalizes_ids_and_prompt_map() -> None:
    """The handled-turn carrier should normalize IDs, prompts, and empty event IDs."""
    handled_turn = HandledTurnState.create(
        ["$a", "", "$a", "$b"],
        response_event_id="",
        visible_echo_event_id="",
        source_event_prompts={"$a": "prompt a", "$extra": "ignored"},
    )

    assert handled_turn.source_event_ids == ("$a", "$b")
    assert handled_turn.response_event_id is None
    assert handled_turn.visible_echo_event_id is None
    assert handled_turn.source_event_prompts == {"$a": "prompt a"}
    assert handled_turn.anchor_event_id == "$b"
    assert handled_turn.is_coalesced


def test_handled_turn_state_preserves_response_context() -> None:
    """The handled-turn carrier should keep response owner, history scope, and target intact."""
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )
    history_scope = HistoryScope(kind="team", scope_id="team_scope")

    handled_turn = HandledTurnState.create(
        ["$event:example.com"],
        response_owner="test_agent",
        history_scope=history_scope,
        conversation_target=conversation_target,
    )

    assert handled_turn.response_owner == "test_agent"
    assert handled_turn.history_scope == history_scope
    assert handled_turn.conversation_target == conversation_target


def test_record_outcome_marks_single_source_event(temp_dir: Path) -> None:
    """A single-source outcome should mark the event terminally handled."""
    tracker = HandledTurnLedger("test_mark", base_path=temp_dir)

    before_time = time.time()
    _record_handled_turn(tracker, ["event123"])
    after_time = time.time()

    assert tracker.has_responded("event123")
    assert tracker.get_response_event_id("event123") is None
    record = tracker.get_turn_record("event123")
    assert record == HandledTurnRecord(
        anchor_event_id="event123",
        source_event_ids=("event123",),
        timestamp=record.timestamp if record is not None else 0.0,
    )
    assert record is not None
    assert record.completed
    assert before_time <= record.timestamp <= after_time


def test_record_handled_turn_tracks_typed_carrier(temp_dir: Path) -> None:
    """The ledger should record the typed handled-turn carrier without losing prompt metadata."""
    tracker = HandledTurnLedger("test_state_record", base_path=temp_dir)
    history_scope = HistoryScope(kind="agent", scope_id="test_state_record")
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )

    tracker.record_handled_turn(
        HandledTurnState.create(
            ["$first", "$second"],
            response_event_id="$response",
            source_event_prompts={"$first": "first prompt", "$second": "second prompt"},
            response_owner="test_state_record",
            history_scope=history_scope,
            conversation_target=conversation_target,
        ),
    )

    turn_record = tracker.get_turn_record("$first")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"
    assert turn_record.source_event_ids == ("$first", "$second")
    assert turn_record.source_event_prompts == {
        "$first": "first prompt",
        "$second": "second prompt",
    }
    assert turn_record.response_owner == "test_state_record"
    assert turn_record.history_scope == history_scope
    assert turn_record.conversation_target == conversation_target


def test_record_outcome_tracks_response_event_id(temp_dir: Path) -> None:
    """A completed outcome should preserve the response event linkage."""
    tracker = HandledTurnLedger("test_linkage", base_path=temp_dir)

    _record_handled_turn(tracker, ["event123"], response_event_id="$response")

    assert tracker.has_responded("event123")
    assert tracker.get_response_event_id("event123") == "$response"
    assert tracker.get_turn_record("event123") == HandledTurnRecord(
        anchor_event_id="event123",
        source_event_ids=("event123",),
        response_event_id="$response",
        timestamp=tracker.get_turn_record("event123").timestamp,
    )


def test_pending_handled_turn_round_trips_response_transaction_id(temp_dir: Path) -> None:
    """Pending handled-turn records should preserve one reserved outbound transaction id."""
    tracker = HandledTurnLedger("test_pending", base_path=temp_dir)
    tracker.record_handled_turn_record(
        HandledTurnRecord(
            anchor_event_id="$event",
            source_event_ids=("$event",),
            completed=False,
            response_transaction_id="txn-123",
        ),
    )

    reloaded = HandledTurnLedger("test_pending", base_path=temp_dir)
    turn_record = reloaded.get_turn_record("$event")

    assert turn_record is not None
    assert turn_record.completed is False
    assert turn_record.response_transaction_id == "txn-123"


def test_record_outcome_deduplicates_source_event_ids(temp_dir: Path) -> None:
    """Duplicate source IDs should collapse into one stored turn record."""
    tracker = HandledTurnLedger("test_dedup", base_path=temp_dir)

    _record_handled_turn(tracker, ["$a", "$a", "$b"], response_event_id="$response")

    assert len(tracker._responses) == 2
    assert tracker.get_turn_record("$a") is not None
    assert tracker.get_turn_record("$a").source_event_ids == ("$a", "$b")
    assert tracker.get_turn_record("$b").source_event_ids == ("$a", "$b")


def test_record_outcome_tracks_coalesced_turn(temp_dir: Path) -> None:
    """Coalesced outcomes should persist one shared turn record per source ID."""
    tracker = HandledTurnLedger("test_coalesced", base_path=temp_dir)

    _record_handled_turn(
        tracker,
        ["$first", "$second"],
        response_event_id="$response",
        source_event_prompts={
            "$first": "first prompt",
            "$second": "second prompt",
        },
    )

    assert tracker.has_responded("$first")
    assert tracker.has_responded("$second")
    assert tracker.get_response_event_id("$first") == "$response"
    assert tracker.get_response_event_id("$second") == "$response"
    turn_record = tracker.get_turn_record("$second")
    assert turn_record is not None
    assert turn_record.anchor_event_id == "$second"
    assert turn_record.source_event_ids == ("$first", "$second")
    assert turn_record.source_event_prompts == {
        "$first": "first prompt",
        "$second": "second prompt",
    }
    assert turn_record.is_coalesced


def test_is_coalesced_false_for_single_source(temp_dir: Path) -> None:
    """Single-source turns should not report coalescing."""
    tracker = HandledTurnLedger("test_single", base_path=temp_dir)

    _record_handled_turn(tracker, ["$single"])

    turn_record = tracker.get_turn_record("$single")
    assert turn_record is not None
    assert not turn_record.is_coalesced


def test_record_outcome_filters_prompt_map_to_source_ids(temp_dir: Path) -> None:
    """Only prompts for recorded source IDs should be persisted."""
    tracker = HandledTurnLedger("test_prompt_filter", base_path=temp_dir)

    _record_handled_turn(
        tracker,
        ["$a", "$b"],
        response_event_id="$response",
        source_event_prompts={"$a": "prompt a", "$extra": "ignored"},
    )

    turn_record = tracker.get_turn_record("$a")
    assert turn_record is not None
    assert turn_record.source_event_prompts == {"$a": "prompt a"}


def test_record_outcome_preserves_existing_prompt_map_when_omitted(temp_dir: Path) -> None:
    """A later outcome write without prompts should keep the existing prompt map."""
    tracker = HandledTurnLedger("test_prompt_preserve", base_path=temp_dir)

    _record_handled_turn(
        tracker,
        ["$a", "$b"],
        response_event_id="$response-1",
        source_event_prompts={"$a": "prompt a", "$b": "prompt b"},
    )
    _record_handled_turn(tracker, ["$a", "$b"], response_event_id="$response-2")

    turn_record = tracker.get_turn_record("$b")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response-2"
    assert turn_record.source_event_prompts == {"$a": "prompt a", "$b": "prompt b"}


def test_record_outcome_preserves_existing_prompt_map_when_empty_dict(temp_dir: Path) -> None:
    """An empty prompt map should behave like omission rather than clearing stored prompts."""
    tracker = HandledTurnLedger("test_prompt_empty_dict", base_path=temp_dir)

    _record_handled_turn(
        tracker,
        ["$a", "$b"],
        response_event_id="$response-1",
        source_event_prompts={"$a": "prompt a", "$b": "prompt b"},
    )
    _record_handled_turn(tracker, ["$a", "$b"], response_event_id="$response-2", source_event_prompts={})

    turn_record = tracker.get_turn_record("$a")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response-2"
    assert turn_record.source_event_prompts == {"$a": "prompt a", "$b": "prompt b"}


def test_visible_echo_tracking_stays_partial_until_completed(temp_dir: Path) -> None:
    """Visible echoes should dedupe retries without completing the turn."""
    tracker = HandledTurnLedger("test_visible_echo", base_path=temp_dir)

    tracker.record_visible_echo("event123", "$echo")

    assert not tracker.has_responded("event123")
    assert tracker.get_response_event_id("event123") is None
    assert tracker.get_visible_echo_event_id("event123") == "$echo"
    turn_record = tracker.get_turn_record("event123")
    assert turn_record is not None
    assert not turn_record.completed
    assert turn_record.visible_echo_event_id == "$echo"


def test_record_outcome_preserves_existing_visible_echo(temp_dir: Path) -> None:
    """Completing a partially echoed turn should keep the visible echo ID."""
    tracker = HandledTurnLedger("test_visible_echo_completion", base_path=temp_dir)

    tracker.record_visible_echo("event123", "$echo")
    _record_handled_turn(tracker, ["event123"])

    assert tracker.has_responded("event123")
    assert tracker.get_visible_echo_event_id("event123") == "$echo"


def test_record_outcome_propagates_visible_echo_to_coalesced_sources(temp_dir: Path) -> None:
    """When one source already has a visible echo, terminal completion should copy it to the batch."""
    tracker = HandledTurnLedger("test_visible_echo_batch", base_path=temp_dir)

    tracker.record_visible_echo("$voice", "$echo")
    _record_handled_turn(tracker, ["$voice", "$text"], response_event_id="$echo")

    assert tracker.has_responded("$voice")
    assert tracker.has_responded("$text")
    assert tracker.get_visible_echo_event_id("$voice") == "$echo"
    assert tracker.get_visible_echo_event_id("$text") == "$echo"


def test_visible_echo_persists_across_reload(temp_dir: Path) -> None:
    """Visible echoes should survive a new ledger instance on the same storage path."""
    tracker1 = HandledTurnLedger("test_visible_echo_reload", base_path=temp_dir)

    tracker1.record_visible_echo("event123", "$echo")

    tracker2 = HandledTurnLedger("test_visible_echo_reload", base_path=temp_dir)

    assert not tracker2.has_responded("event123")
    assert tracker2.get_visible_echo_event_id("event123") == "$echo"
    turn_record = tracker2.get_turn_record("event123")
    assert turn_record is not None
    assert not turn_record.completed
    assert turn_record.visible_echo_event_id == "$echo"


def test_record_outcome_with_empty_source_list_is_noop(temp_dir: Path) -> None:
    """Empty outcome batches should not mutate the ledger."""
    tracker = HandledTurnLedger("test_empty_batch", base_path=temp_dir)

    _record_handled_turn(tracker, [])

    assert tracker._responses == {}


def test_persistence_round_trip(temp_dir: Path) -> None:
    """Ledger state should survive a new instance load from disk."""
    tracker1 = HandledTurnLedger("test_persist", base_path=temp_dir)
    _record_handled_turn(
        tracker1,
        ["$first", "$second"],
        response_event_id="$response",
        source_event_prompts={"$first": "first", "$second": "second"},
    )

    tracker2 = HandledTurnLedger("test_persist", base_path=temp_dir)

    assert tracker2.has_responded("$first")
    assert tracker2.has_responded("$second")
    assert tracker2.get_response_event_id("$second") == "$response"
    assert tracker2.get_turn_record("$second").source_event_prompts == {
        "$first": "first",
        "$second": "second",
    }


def test_persistence_round_trip_preserves_response_context(temp_dir: Path) -> None:
    """Reloaded ledgers should preserve response owner, history scope, and target metadata."""
    tracker1 = HandledTurnLedger("test_persist_context", base_path=temp_dir)
    history_scope = HistoryScope(kind="team", scope_id="team_scope")
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )
    _record_handled_turn(
        tracker1,
        ["$original", "$reply"],
        response_event_id="$response",
        source_event_prompts={"$original": "original", "$reply": "reply"},
        response_owner="test_team",
        history_scope=history_scope,
        conversation_target=conversation_target,
    )

    tracker2 = HandledTurnLedger("test_persist_context", base_path=temp_dir)

    turn_record = tracker2.get_turn_record("$reply")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"
    assert turn_record.source_event_ids == ("$original", "$reply")
    assert turn_record.source_event_prompts == {
        "$original": "original",
        "$reply": "reply",
    }
    assert turn_record.response_owner == "test_team"
    assert turn_record.history_scope == history_scope
    assert turn_record.conversation_target == conversation_target


def test_loads_legacy_response_tracker_shape(temp_dir: Path) -> None:
    """Legacy response tracker JSON should load into the new ledger."""
    legacy_file = temp_dir / "legacy_responded.json"
    legacy_file.write_text(
        json.dumps(
            {
                "$event": {
                    "timestamp": time.time(),
                    "response_id": "$response",
                    "completed": True,
                    "visible_echo_response_id": "$echo",
                },
            },
        ),
        encoding="utf-8",
    )

    tracker = HandledTurnLedger("legacy", base_path=temp_dir)

    assert tracker.has_responded("$event")
    assert tracker.get_response_event_id("$event") == "$response"
    assert tracker.get_visible_echo_event_id("$event") == "$echo"
    turn_record = tracker.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.anchor_event_id == "$event"
    assert turn_record.source_event_ids == ("$event",)


def test_loads_legacy_record_without_completed_flag_as_terminal(temp_dir: Path) -> None:
    """Legacy records without `completed` should remain terminally handled."""
    legacy_file = temp_dir / "legacy_default_responded.json"
    legacy_file.write_text(
        json.dumps(
            {
                "$event": {
                    "timestamp": time.time(),
                    "response_id": None,
                },
            },
        ),
        encoding="utf-8",
    )

    tracker = HandledTurnLedger("legacy_default", base_path=temp_dir)

    assert tracker.has_responded("$event")
    assert tracker.get_response_event_id("$event") is None
    turn_record = tracker.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.completed


def test_loads_legacy_coalesced_record_shape(temp_dir: Path) -> None:
    """Legacy response field names should still load coalesced turn metadata."""
    legacy_file = temp_dir / "legacy_coalesced_responded.json"
    legacy_file.write_text(
        json.dumps(
            {
                "$first": {
                    "timestamp": time.time(),
                    "response_id": "$response",
                    "visible_echo_response_id": "$echo",
                    "source_event_ids": ["$first", "$primary"],
                    "source_event_prompts": {
                        "$first": "first",
                        "$primary": "primary",
                    },
                },
                "$primary": {
                    "timestamp": time.time(),
                    "response_id": "$response",
                    "visible_echo_response_id": "$echo",
                    "source_event_ids": ["$first", "$primary"],
                    "source_event_prompts": {
                        "$first": "first",
                        "$primary": "primary",
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    tracker = HandledTurnLedger("legacy_coalesced", base_path=temp_dir)

    turn_record = tracker.get_turn_record("$first")
    assert turn_record is not None
    assert turn_record.source_event_ids == ("$first", "$primary")
    assert turn_record.response_event_id == "$response"
    assert turn_record.visible_echo_event_id == "$echo"
    assert turn_record.source_event_prompts == {
        "$first": "first",
        "$primary": "primary",
    }


def test_normalizes_empty_record_dict_to_terminal_self_anchored_turn(temp_dir: Path) -> None:
    """Semantically partial on-disk records should normalize to one terminal self-anchored turn."""
    tracker = HandledTurnLedger("test_empty_record", base_path=temp_dir)
    _write_responses_file(tracker, {"$event": {}})

    turn_record = tracker.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.source_event_ids == ("$event",)
    assert turn_record.anchor_event_id == "$event"
    assert turn_record.response_event_id is None
    assert turn_record.source_event_prompts is None
    assert turn_record.completed
    assert turn_record.timestamp == 0.0


def test_normalizes_empty_source_ids_and_filters_partial_prompt_map(temp_dir: Path) -> None:
    """Invalid source lists should fall back to the event ID and keep only matching prompt entries."""
    tracker = HandledTurnLedger("test_partial_record", base_path=temp_dir)
    _write_responses_file(
        tracker,
        {
            "$event": {
                "timestamp": 123.0,
                "response_event_id": "$response",
                "source_event_ids": [],
                "source_event_prompts": {
                    "$event": "prompt",
                    "$extra": "ignored",
                },
            },
        },
    )

    turn_record = tracker.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.source_event_ids == ("$event",)
    assert turn_record.response_event_id == "$response"
    assert turn_record.source_event_prompts == {"$event": "prompt"}


def test_large_coalesced_turn_round_trips(temp_dir: Path) -> None:
    """Large coalesced prompt maps should survive atomic write and reload intact."""
    tracker = HandledTurnLedger("test_large_coalesced", base_path=temp_dir)
    source_event_ids = [f"$event-{index}" for index in range(200)]
    prompt_map = {event_id: f"prompt {index}" for index, event_id in enumerate(source_event_ids)}

    _record_handled_turn(
        tracker,
        source_event_ids,
        response_event_id="$response",
        source_event_prompts=prompt_map,
    )

    reloaded = HandledTurnLedger("test_large_coalesced", base_path=temp_dir)

    turn_record = reloaded.get_turn_record(source_event_ids[-1])
    assert turn_record is not None
    assert turn_record.source_event_ids == tuple(source_event_ids)
    assert turn_record.source_event_prompts == prompt_map


def test_cleanup_by_count_keeps_most_recent_records(temp_dir: Path) -> None:
    """Cleanup should keep the newest events when count exceeds the cap."""
    tracker = HandledTurnLedger("test_cleanup", base_path=temp_dir)
    base_time = time.time()
    responses = {
        f"event{index:03d}": {
            "timestamp": base_time + index,
            "response_event_id": None,
        }
        for index in range(20)
    }
    _write_responses_file(tracker, responses)
    tracker._cleanup_old_events(max_events=10)

    assert len(tracker._responses) == 10
    assert tracker.has_responded("event019")
    assert tracker.has_responded("event010")
    assert not tracker.has_responded("event009")


def test_cleanup_by_count_keeps_coalesced_groups_intact(temp_dir: Path) -> None:
    """Count cleanup should evict entire coalesced turns rather than splitting them."""
    tracker = HandledTurnLedger("test_cleanup_groups", base_path=temp_dir)
    base_time = time.time()
    responses = {
        "$a": {
            "timestamp": base_time + 1,
            "response_event_id": "$ra",
            "source_event_ids": ["$a", "$b"],
        },
        "$b": {
            "timestamp": base_time + 1,
            "response_event_id": "$ra",
            "source_event_ids": ["$a", "$b"],
        },
        "$c": {
            "timestamp": base_time + 2,
            "response_event_id": "$rc",
            "source_event_ids": ["$c"],
        },
        "$d": {
            "timestamp": base_time + 3,
            "response_event_id": "$rd",
            "source_event_ids": ["$d", "$e"],
        },
        "$e": {
            "timestamp": base_time + 3,
            "response_event_id": "$rd",
            "source_event_ids": ["$d", "$e"],
        },
    }
    _write_responses_file(tracker, responses)

    tracker._cleanup_old_events(max_events=2)

    assert set(tracker._responses) == {"$c", "$d", "$e"}
    assert tracker.has_responded("$c")
    assert tracker.has_responded("$d")
    assert tracker.has_responded("$e")
    assert not tracker.has_responded("$a")
    assert not tracker.has_responded("$b")


def test_cleanup_by_age_removes_old_records(temp_dir: Path) -> None:
    """Cleanup should remove records older than the retention window."""
    tracker = HandledTurnLedger("test_age_cleanup", base_path=temp_dir)
    current_time = time.time()
    responses = {}
    for index in range(5):
        responses[f"old_event{index}"] = {
            "timestamp": current_time - (40 * 24 * 60 * 60),
            "response_event_id": None,
        }
        responses[f"new_event{index}"] = {
            "timestamp": current_time - (10 * 24 * 60 * 60),
            "response_event_id": None,
        }
    _write_responses_file(tracker, responses)

    tracker._cleanup_old_events(max_events=100, max_age_days=30)

    assert len(tracker._responses) == 5
    for index in range(5):
        assert tracker.has_responded(f"new_event{index}")
        assert not tracker.has_responded(f"old_event{index}")


def test_concurrent_access_keeps_json_valid(temp_dir: Path) -> None:
    """Concurrent writes should keep the persisted file readable."""
    tracker = HandledTurnLedger("test_concurrent", base_path=temp_dir)

    def mark_events(start: int, count: int) -> None:
        for index in range(start, start + count):
            _record_handled_turn(tracker, [f"event_{index}"], response_event_id=f"$response_{index}")

    threads = [threading.Thread(target=mark_events, args=(offset, 25)) for offset in range(0, 100, 25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(tracker._responses) == 100
    with tracker._responses_file.open() as file:
        assert len(json.load(file)) == 100


def test_concurrent_cross_instance_writes_wait_for_lock_and_merge(temp_dir: Path) -> None:
    """A second ledger instance should wait on the lock file and preserve both writes."""
    tracker_a = HandledTurnLedger("test_cross_instance_lock", base_path=temp_dir)
    tracker_b = HandledTurnLedger("test_cross_instance_lock", base_path=temp_dir)
    _record_handled_turn(tracker_a, ["$first"], response_event_id="$response-a")

    writer_started = threading.Event()
    writer_finished = threading.Event()

    def write_second_turn() -> None:
        writer_started.set()
        _record_handled_turn(tracker_b, ["$second"], response_event_id="$response-b")
        writer_finished.set()

    with tracker_a._responses_lock_file.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        writer_thread = threading.Thread(target=write_second_turn)
        writer_thread.start()
        assert writer_started.wait(timeout=1.0)
        time.sleep(0.05)
        assert not writer_finished.is_set()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        writer_thread.join(timeout=1.0)
        assert writer_finished.is_set()

    tracker_c = HandledTurnLedger("test_cross_instance_lock", base_path=temp_dir)
    assert tracker_c.get_response_event_id("$first") == "$response-a"
    assert tracker_c.get_response_event_id("$second") == "$response-b"


def test_multiple_instances_merge_updates(temp_dir: Path) -> None:
    """Stale instances should merge with disk state instead of clobbering prior writes."""
    tracker_a = HandledTurnLedger("test_multi_instance", base_path=temp_dir)
    tracker_b = HandledTurnLedger("test_multi_instance", base_path=temp_dir)

    _record_handled_turn(tracker_a, ["$first"], response_event_id="$response-a")
    _record_handled_turn(tracker_b, ["$second"], response_event_id="$response-b")

    tracker_c = HandledTurnLedger("test_multi_instance", base_path=temp_dir)
    assert tracker_c.get_response_event_id("$first") == "$response-a"
    assert tracker_c.get_response_event_id("$second") == "$response-b"


def test_multiple_instances_refresh_reads_from_disk(temp_dir: Path) -> None:
    """Long-lived instances should observe sibling writes during read-side queries."""
    tracker_a = HandledTurnLedger("test_multi_instance_reads", base_path=temp_dir)
    tracker_b = HandledTurnLedger("test_multi_instance_reads", base_path=temp_dir)

    _record_handled_turn(
        tracker_a,
        ["$first", "$second"],
        response_event_id="$response-a",
        source_event_prompts={"$first": "first", "$second": "second"},
    )

    assert tracker_b.has_responded("$first")
    assert tracker_b.get_response_event_id("$second") == "$response-a"
    turn_record = tracker_b.get_turn_record("$first")
    assert turn_record is not None
    assert turn_record.source_event_ids == ("$first", "$second")
    assert turn_record.source_event_prompts == {"$first": "first", "$second": "second"}


def test_quarantines_malformed_ledger_file(temp_dir: Path) -> None:
    """Malformed JSON should be quarantined so ledger initialization still succeeds."""
    responses_file = temp_dir / "bad_json_responded.json"
    responses_file.write_text("{not valid json", encoding="utf-8")

    tracker = HandledTurnLedger("bad_json", base_path=temp_dir)

    assert tracker._responses == {}
    assert json.loads(responses_file.read_text(encoding="utf-8")) == {}
    quarantined_files = list(temp_dir.glob("bad_json_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1


def test_quarantines_non_utf8_ledger_file(temp_dir: Path) -> None:
    """Invalid UTF-8 should be quarantined so ledger initialization still succeeds."""
    responses_file = temp_dir / "bad_utf8_responded.json"
    responses_file.write_bytes(b"\xff\xfe\x00")

    tracker = HandledTurnLedger("bad_utf8", base_path=temp_dir)

    assert tracker._responses == {}
    assert json.loads(responses_file.read_text(encoding="utf-8")) == {}
    quarantined_files = list(temp_dir.glob("bad_utf8_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1


def test_quarantines_structurally_invalid_ledger_file(temp_dir: Path) -> None:
    """Valid JSON with the wrong top-level shape should still be quarantined."""
    responses_file = temp_dir / "bad_shape_responded.json"
    responses_file.write_text(json.dumps(["oops"]), encoding="utf-8")

    tracker = HandledTurnLedger("bad_shape", base_path=temp_dir)

    assert tracker._responses == {}
    assert json.loads(responses_file.read_text(encoding="utf-8")) == {}
    quarantined_files = list(temp_dir.glob("bad_shape_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1
    assert json.loads(quarantined_files[0].read_text(encoding="utf-8")) == ["oops"]


def test_quarantines_ledger_file_with_invalid_event_entry(temp_dir: Path) -> None:
    """Per-event entries with invalid shapes should be quarantined before rewrite."""
    responses_file = temp_dir / "bad_entry_responded.json"
    responses_file.write_text(json.dumps({"$event": []}), encoding="utf-8")

    tracker = HandledTurnLedger("bad_entry", base_path=temp_dir)

    assert tracker._responses == {}
    assert json.loads(responses_file.read_text(encoding="utf-8")) == {}
    quarantined_files = list(temp_dir.glob("bad_entry_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1
    assert json.loads(quarantined_files[0].read_text(encoding="utf-8")) == {"$event": []}


def test_shared_reads_fail_soft_on_corrupt_file_without_quarantining(temp_dir: Path) -> None:
    """Shared-lock reads should return empty state instead of trying to repair the ledger."""
    tracker_a = HandledTurnLedger("bad_race", base_path=temp_dir)
    tracker_b = HandledTurnLedger("bad_race", base_path=temp_dir)
    tracker_a._responses_file.write_text("{not valid json", encoding="utf-8")

    results: list[bool] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def read_has_responded(tracker: HandledTurnLedger) -> None:
        try:
            barrier.wait()
            results.append(tracker.has_responded("$event"))
        except Exception as exc:  # pragma: no cover - this is the failure we are guarding against
            errors.append(exc)

    thread_a = threading.Thread(target=read_has_responded, args=(tracker_a,))
    thread_b = threading.Thread(target=read_has_responded, args=(tracker_b,))
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()

    assert errors == []
    assert results == [False, False]


def test_invalid_agent_name_rejected(temp_dir: Path) -> None:
    """Ledger paths should reject agent names that can escape the tracking directory."""
    with pytest.raises(ValueError, match="Invalid handled-turn ledger agent name"):
        HandledTurnLedger("../escape", base_path=temp_dir)


def test_record_outcome_overwrites_previous_response_id(temp_dir: Path) -> None:
    """A later outcome write should replace the stored response event ID."""
    tracker = HandledTurnLedger("test_replace_response", base_path=temp_dir)

    _record_handled_turn(tracker, ["$event"], response_event_id="$response-1")
    _record_handled_turn(tracker, ["$event"], response_event_id="$response-2")

    assert tracker.get_response_event_id("$event") == "$response-2"


def test_get_turn_record_returns_none_for_unknown_source(temp_dir: Path) -> None:
    """Missing sources should not synthesize turn records."""
    tracker = HandledTurnLedger("test_missing", base_path=temp_dir)

    assert tracker.get_turn_record("$missing") is None
