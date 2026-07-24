"""Tests for handled turn persistence and lookup."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

import mindroom.handled_turns as handled_turns_module
from mindroom.file_locks import advisory_file_lock
from mindroom.handled_turns import (
    HandledTurnLedger,
    SourceEventMetadata,
    TurnRecord,
    TurnRecordCodec,
    _reset_handled_turn_ledger_runtime,
)
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for ledger tests."""
    return tmp_path


def _reload_ledger(agent_name: str, base_path: Path) -> HandledTurnLedger:
    """Simulate a process restart: flush persists, drop shared state, reload from disk."""
    _reset_handled_turn_ledger_runtime()
    return HandledTurnLedger(agent_name, base_path=base_path)


def _write_responses_file(
    tracker: HandledTurnLedger,
    responses: dict[str, dict[str, object]],
) -> None:
    """Seed current-schema ledger records for reload and cleanup tests."""
    serialized_records: dict[str, dict[str, object]] = {}
    for event_id, raw_record in responses.items():
        raw_source_event_ids = raw_record.get("source_event_ids")
        source_event_ids = raw_source_event_ids if isinstance(raw_source_event_ids, list) else [event_id]
        raw_discovery_event_ids = raw_record.get("discovery_event_ids")
        discovery_event_ids = raw_discovery_event_ids if isinstance(raw_discovery_event_ids, list) else []
        record = TurnRecord.create(
            source_event_ids,
            discovery_event_ids=discovery_event_ids,
            anchor_event_id=raw_record.get("anchor_event_id")
            if isinstance(raw_record.get("anchor_event_id"), str)
            else None,
            response_event_id=raw_record.get("response_event_id")
            if isinstance(raw_record.get("response_event_id"), str)
            else None,
            completed=raw_record.get("completed") if isinstance(raw_record.get("completed"), bool) else True,
            visible_echo_event_id=raw_record.get("visible_echo_event_id")
            if isinstance(raw_record.get("visible_echo_event_id"), str)
            else None,
            source_event_prompts=raw_record.get("source_event_prompts")
            if isinstance(raw_record.get("source_event_prompts"), dict)
            else None,
            source_event_metadata=raw_record.get("source_event_metadata")
            if isinstance(raw_record.get("source_event_metadata"), dict)
            else None,
            response_owner=raw_record.get("response_owner")
            if isinstance(raw_record.get("response_owner"), str)
            else None,
            requester_id=raw_record.get("requester_id") if isinstance(raw_record.get("requester_id"), str) else None,
            correlation_id=raw_record.get("correlation_id")
            if isinstance(raw_record.get("correlation_id"), str)
            else None,
            history_scope=HistoryScope.from_metadata(raw_record.get("history_scope")),
            conversation_target=MessageTarget.from_metadata(raw_record.get("conversation_target")),
            timestamp=float(raw_record.get("timestamp", 0.0)),
        )
        serialized_records[event_id] = TurnRecordCodec.to_ledger_record(record)
    tracker._responses_file.write_text(
        json.dumps(
            {
                "schema_version": TurnRecordCodec.schema_version(),
                "records": serialized_records,
            },
        ),
        encoding="utf-8",
    )


def _record_handled_turn(
    tracker: HandledTurnLedger,
    source_event_ids: list[str],
    *,
    response_event_id: str | None = None,
    source_event_prompts: dict[str, str] | None = None,
    response_owner: str | None = None,
    requester_id: str | None = None,
    correlation_id: str | None = None,
    history_scope: HistoryScope | None = None,
    conversation_target: MessageTarget | None = None,
) -> None:
    """Record one normalized handled turn through the typed carrier."""
    tracker.record_handled_turn(
        TurnRecord.create(
            source_event_ids,
            response_event_id=response_event_id,
            source_event_prompts=source_event_prompts,
            response_owner=response_owner,
            requester_id=requester_id,
            correlation_id=correlation_id,
            history_scope=history_scope,
            conversation_target=conversation_target,
        ),
    )


def _get_response_event_id(tracker: HandledTurnLedger, source_event_id: str) -> str | None:
    turn_record = tracker.get_turn_record(source_event_id)
    return turn_record.response_event_id if turn_record is not None else None


def _read_persisted_records(tracker: HandledTurnLedger) -> dict[str, object]:
    payload = json.loads(tracker._responses_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == TurnRecordCodec.schema_version()
    records = payload["records"]
    assert isinstance(records, dict)
    return records


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


def test_turn_record_normalizes_ids_and_prompt_map() -> None:
    """The handled-turn carrier should normalize IDs, prompts, and empty event IDs."""
    handled_turn = TurnRecord.create(
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


def test_turn_record_preserves_response_context() -> None:
    """The handled-turn carrier should keep response owner, history scope, and target intact."""
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )
    history_scope = HistoryScope(kind="team", scope_id="team_scope")

    handled_turn = TurnRecord.create(
        ["$event:example.com"],
        response_owner="test_agent",
        history_scope=history_scope,
        conversation_target=conversation_target,
    )

    assert handled_turn.response_owner == "test_agent"
    assert handled_turn.history_scope == history_scope
    assert handled_turn.conversation_target == conversation_target


def test_turn_record_preserves_requester_and_correlation() -> None:
    """The handled-turn carrier should keep requester and correlation ids intact."""
    handled_turn = TurnRecord.create(
        ["$event:example.com"],
        requester_id="@user:example.com",
        correlation_id="corr-123",
    )

    updated = replace(handled_turn, response_owner="agent")

    assert updated.requester_id == "@user:example.com"
    assert updated.correlation_id == "corr-123"


def test_record_outcome_marks_single_source_event(temp_dir: Path) -> None:
    """A single-source outcome should mark the event terminally handled."""
    tracker = HandledTurnLedger("test_mark", base_path=temp_dir)

    before_time = time.time()
    _record_handled_turn(tracker, ["event123"])
    after_time = time.time()

    assert tracker.has_responded("event123")
    assert _get_response_event_id(tracker, "event123") is None
    record = tracker.get_turn_record("event123")
    assert record == TurnRecord(
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
        TurnRecord.create(
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
    assert _get_response_event_id(tracker, "event123") == "$response"
    assert tracker.get_turn_record("event123") == TurnRecord(
        anchor_event_id="event123",
        source_event_ids=("event123",),
        response_event_id="$response",
        timestamp=tracker.get_turn_record("event123").timestamp,
    )


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
    assert _get_response_event_id(tracker, "$first") == "$response"
    assert _get_response_event_id(tracker, "$second") == "$response"
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


def test_visible_echo_tracking_stays_partial_until_completed(temp_dir: Path) -> None:
    """The ledger should persist an exact partial record without completing it."""
    tracker = HandledTurnLedger("test_visible_echo", base_path=temp_dir)

    tracker.record_handled_turn(
        TurnRecord.create(["event123"], completed=False, visible_echo_event_id="$echo"),
    )

    assert not tracker.has_responded("event123")
    assert _get_response_event_id(tracker, "event123") is None
    assert tracker.get_visible_echo_event_id("event123") == "$echo"
    turn_record = tracker.get_turn_record("event123")
    assert turn_record is not None
    assert not turn_record.completed
    assert turn_record.visible_echo_event_id == "$echo"


def test_visible_echo_persists_across_reload(temp_dir: Path) -> None:
    """Visible echoes should survive a new ledger instance on the same storage path."""
    tracker1 = HandledTurnLedger("test_visible_echo_reload", base_path=temp_dir)

    tracker1.record_handled_turn(
        TurnRecord.create(["event123"], completed=False, visible_echo_event_id="$echo"),
    )

    tracker2 = _reload_ledger("test_visible_echo_reload", temp_dir)

    assert not tracker2.has_responded("event123")
    assert tracker2.get_visible_echo_event_id("event123") == "$echo"
    turn_record = tracker2.get_turn_record("event123")
    assert turn_record is not None
    assert not turn_record.completed
    assert turn_record.visible_echo_event_id == "$echo"


def test_source_event_metadata_persists_across_reload(temp_dir: Path) -> None:
    """Coalesced source-event metadata should survive a ledger reload from disk as floats."""
    tracker1 = HandledTurnLedger("test_source_metadata_reload", base_path=temp_dir)
    tracker1.record_handled_turn(
        TurnRecord.create(
            ["$first", "$second"],
            response_event_id="$response",
            source_event_prompts={"$first": "first", "$second": "second"},
            source_event_metadata={
                "$first": SourceEventMetadata(sender="@alice:localhost", timestamp_ms=1_774_019_700_000),
                "$second": SourceEventMetadata(sender="@bob:localhost", timestamp_ms=None),
            },
        ),
    )

    turn_record = _reload_ledger("test_source_metadata_reload", temp_dir).get_turn_record("$second")

    assert turn_record is not None
    assert turn_record.source_event_metadata == {
        "$first": SourceEventMetadata(sender="@alice:localhost", timestamp_ms=1_774_019_700_000.0),
        "$second": SourceEventMetadata(sender="@bob:localhost", timestamp_ms=None),
    }


def test_missing_source_event_metadata_loads_as_none(temp_dir: Path) -> None:
    """Records persisted before source_event_metadata existed should load cleanly as None."""
    tracker1 = HandledTurnLedger("test_source_metadata_absent", base_path=temp_dir)
    _record_handled_turn(
        tracker1,
        ["$first", "$second"],
        response_event_id="$response",
        source_event_prompts={"$first": "first", "$second": "second"},
    )

    turn_record = _reload_ledger("test_source_metadata_absent", temp_dir).get_turn_record("$second")

    assert turn_record is not None
    assert turn_record.source_event_metadata is None


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

    tracker2 = _reload_ledger("test_persist", temp_dir)

    assert tracker2.has_responded("$first")
    assert tracker2.has_responded("$second")
    assert _get_response_event_id(tracker2, "$second") == "$response"
    assert tracker2.get_turn_record("$second").source_event_prompts == {
        "$first": "first",
        "$second": "second",
    }


def test_discovery_alias_persists_without_becoming_a_coalesced_source(temp_dir: Path) -> None:
    """Discovery aliases should rehydrate to the canonical record without changing source semantics."""
    tracker1 = HandledTurnLedger("test_discovery_alias", base_path=temp_dir)
    tracker1.record_handled_turn(
        TurnRecord.create(
            ["$question"],
            discovery_event_ids=["$selection"],
            response_event_id="$response",
        ),
    )

    tracker2 = _reload_ledger("test_discovery_alias", temp_dir)

    question_record = tracker2.get_turn_record("$question")
    selection_record = tracker2.get_turn_record("$selection")
    assert question_record is not None
    assert selection_record == question_record
    assert question_record.source_event_ids == ("$question",)
    assert question_record.discovery_event_ids == ("$selection",)
    assert not question_record.is_coalesced


def test_discovery_alias_redaction_and_cleanup_intent_persist(temp_dir: Path) -> None:
    """Selection aliases must retain both their tombstone and owed cleanup across restart."""
    tracker = HandledTurnLedger("test_discovery_redaction", base_path=temp_dir)
    tracker.record_handled_turn(
        TurnRecord.create(
            ["$question"],
            discovery_event_ids=["$selection"],
            redacted_source_event_ids=["$selection"],
            pending_redaction_cleanup_event_ids=["$selection"],
            completed=False,
        ),
    )

    reloaded = _reload_ledger("test_discovery_redaction", temp_dir)
    record = reloaded.get_turn_record("$selection")

    assert record is not None
    assert record.redacted_source_event_ids == ("$selection",)
    assert record.pending_redaction_cleanup_event_ids == ("$selection",)
    assert reloaded.pending_redaction_cleanup_event_ids() == ("$selection",)
    assert reloaded.has_responded("$selection") is True
    assert reloaded.has_responded("$question") is False


def test_flush_propagates_persistence_failure(temp_dir: Path) -> None:
    """Durability barriers must fail before callers mutate source cache state."""
    tracker = HandledTurnLedger("test_persist_failure", base_path=temp_dir)

    with patch.object(tracker, "_write_responses_file_locked", side_effect=OSError("disk full")):
        tracker.record_handled_turn(TurnRecord.create(["$source"], completed=False))
        with pytest.raises(OSError, match="disk full"):
            tracker.flush()


def test_flush_waits_for_every_captured_persist_before_raising(temp_dir: Path) -> None:
    """One failed persist must not drop later captured work from the barrier."""
    tracker = HandledTurnLedger("test_flush_drains", base_path=temp_dir)
    first_future = MagicMock()
    first_future.result.side_effect = OSError("first persist failed")
    second_future = MagicMock()
    tracker._state.pending_persists = [first_future, second_future]

    with pytest.raises(OSError, match="first persist failed"):
        tracker.flush()

    first_future.result.assert_called_once_with()
    second_future.result.assert_called_once_with()


def test_later_record_does_not_prune_unobserved_persistence_failure(temp_dir: Path) -> None:
    """A later write must not hide an earlier completed persistence failure."""
    tracker = HandledTurnLedger("test_interleaved_persist_failure", base_path=temp_dir)
    tracker.warm()
    real_persist = tracker._persist_records
    first_failed = threading.Event()
    second_completed = threading.Event()
    failed_once = False

    def persist_with_first_failure(turn_records: tuple[TurnRecord, ...]) -> None:
        nonlocal failed_once
        if not failed_once and any("$redacted" in record.indexed_event_ids for record in turn_records):
            failed_once = True
            first_failed.set()
            message = "redaction persist failed"
            raise OSError(message)
        real_persist(turn_records)
        if any("$later" in record.indexed_event_ids for record in turn_records):
            second_completed.set()

    with patch.object(tracker, "_persist_records", side_effect=persist_with_first_failure):
        tracker.record_handled_turn(TurnRecord.create(["$redacted"], completed=False))
        assert first_failed.wait(timeout=5)
        failure = tracker._state.pending_persists[0].exception(timeout=5)
        assert isinstance(failure, OSError)
        tracker.record_handled_turn(TurnRecord.create(["$later"], completed=False))
        assert second_completed.wait(timeout=5)
        with pytest.raises(OSError, match="redaction persist failed"):
            tracker.flush()


def test_concurrent_records_coalesce_into_one_follow_up_ledger_write(temp_dir: Path) -> None:
    """A burst arriving during one write should persist as one follow-up batch."""
    tracker = HandledTurnLedger("test_persist_batch", base_path=temp_dir)
    tracker.warm()
    real_persist = tracker._persist_records
    first_started = threading.Event()
    release_first = threading.Event()
    batch_sizes: list[int] = []

    def persist_with_barrier(turn_records: tuple[TurnRecord, ...]) -> None:
        batch_sizes.append(len(turn_records))
        if len(batch_sizes) == 1:
            first_started.set()
            assert release_first.wait(timeout=5)
        real_persist(turn_records)

    with patch.object(tracker, "_persist_records", side_effect=persist_with_barrier):
        tracker.record_handled_turn(TurnRecord.create(["$event-0"], completed=False))
        assert first_started.wait(timeout=5)
        for index in range(1, 100):
            tracker.record_handled_turn(TurnRecord.create([f"$event-{index}"], completed=False))
        release_first.set()
        tracker.flush()

    assert batch_sizes == [1, 99]
    reloaded = _reload_ledger("test_persist_batch", temp_dir)
    assert all(reloaded.get_turn_record(f"$event-{index}") is not None for index in range(100))


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

    tracker2 = _reload_ledger("test_persist_context", temp_dir)

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


def test_persistence_round_trip_preserves_requester_and_correlation(temp_dir: Path) -> None:
    """Reloaded ledgers should preserve requester and correlation ids."""
    tracker1 = HandledTurnLedger("test_persist_request_context", base_path=temp_dir)
    _record_handled_turn(
        tracker1,
        ["$original", "$reply"],
        response_event_id="$response",
        requester_id="@user:example.com",
        correlation_id="corr-123",
    )

    tracker2 = _reload_ledger("test_persist_request_context", temp_dir)

    turn_record = tracker2.get_turn_record("$reply")
    assert turn_record is not None
    assert turn_record.requester_id == "@user:example.com"
    assert turn_record.correlation_id == "corr-123"


def test_unversioned_ledger_is_quarantined_instead_of_migrated(temp_dir: Path) -> None:
    """Pre-schema ledgers should be discarded rather than adding migration scaffolding."""
    tracker_file = temp_dir / "unversioned_responded.json"
    tracker_file.write_text(
        json.dumps(
            {
                "$event": {
                    "timestamp": time.time(),
                    "response_event_id": "$response",
                    "completed": True,
                },
            },
        ),
        encoding="utf-8",
    )

    tracker = HandledTurnLedger("unversioned", base_path=temp_dir)

    assert not tracker.has_responded("$event")
    assert tracker.get_turn_record("$event") is None
    assert len(list(temp_dir.glob("unversioned_responded.json.corrupt-*"))) == 1


def test_record_without_requester_or_correlation_loads_cleanly(temp_dir: Path) -> None:
    """Requester and correlation IDs remain optional record context."""
    tracker = HandledTurnLedger("missing_request_context", base_path=temp_dir)
    _write_responses_file(
        tracker,
        {
            "$event": {
                "timestamp": time.time(),
                "response_event_id": "$response",
                "completed": True,
            },
        },
    )

    reloaded = _reload_ledger("missing_request_context", temp_dir)
    turn_record = reloaded.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"
    assert turn_record.requester_id is None
    assert turn_record.correlation_id is None


def test_current_codec_rejects_incomplete_ledger_records() -> None:
    """Current-version ledger rows require the full canonical identity and outcome fields."""
    assert TurnRecordCodec.from_ledger_record("$event", {}) is None
    assert (
        TurnRecordCodec.from_ledger_record(
            "$event",
            {
                "anchor_event_id": "$event",
                "source_event_ids": ["", None],
                "discovery_event_ids": ["$event"],
                "response_event_id": "$response",
                "completed": True,
                "timestamp": time.time(),
            },
        )
        is None
    )


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

    reloaded = _reload_ledger("test_large_coalesced", temp_dir)

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


def test_cleanup_by_age_retains_pending_redaction_intent(temp_dir: Path) -> None:
    """Age retention must not discard cleanup work before the next response."""
    tracker = HandledTurnLedger("test_pending_age_cleanup", base_path=temp_dir)
    old_timestamp = time.time() - (40 * 24 * 60 * 60)
    tracker.record_handled_turn(
        TurnRecord.create(
            ["$pending"],
            redacted_source_event_ids=["$pending"],
            pending_redaction_cleanup_event_ids=["$pending"],
            timestamp=old_timestamp,
        ),
    )
    tracker.record_handled_turn(TurnRecord.create(["$ordinary"], timestamp=old_timestamp))
    tracker.flush()

    tracker._cleanup_old_events(max_events=100, max_age_days=30)

    assert tracker.get_turn_record("$pending") is not None
    assert tracker.pending_redaction_cleanup_event_ids() == ("$pending",)
    assert tracker.get_turn_record("$ordinary") is None


def test_cleanup_by_count_retains_pending_redaction_intent(temp_dir: Path) -> None:
    """Count retention may exceed its limit rather than lose owed cleanup work."""
    tracker = HandledTurnLedger("test_pending_count_cleanup", base_path=temp_dir)
    tracker.record_handled_turn(
        TurnRecord.create(
            ["$pending"],
            redacted_source_event_ids=["$pending"],
            pending_redaction_cleanup_event_ids=["$pending"],
            timestamp=time.time() - 2,
        ),
    )
    tracker.record_handled_turn(TurnRecord.create(["$newest"], timestamp=time.time()))
    tracker.flush()

    tracker._cleanup_old_events(max_events=1, max_age_days=30)

    assert tracker.get_turn_record("$pending") is not None
    assert tracker.pending_redaction_cleanup_event_ids() == ("$pending",)
    assert tracker.get_turn_record("$newest") is not None


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
    tracker.flush()
    assert len(_read_persisted_records(tracker)) == 100


def test_file_lock_defers_persist_without_blocking_writers(temp_dir: Path) -> None:
    """A held file lock should stall only disk persistence, never the recording caller."""
    tracker_a = HandledTurnLedger("test_cross_instance_lock", base_path=temp_dir)
    tracker_b = HandledTurnLedger("test_cross_instance_lock", base_path=temp_dir)
    _record_handled_turn(tracker_a, ["$first"], response_event_id="$response-a")
    tracker_a.flush()

    with advisory_file_lock(tracker_a._responses_lock_file):
        # Recording returns immediately and is visible in shared memory even
        # while the ledger file lock is held.
        _record_handled_turn(tracker_b, ["$second"], response_event_id="$response-b")
        assert _get_response_event_id(tracker_b, "$second") == "$response-b"
        # The queued disk merge cannot complete while the lock is held.
        assert "$second" not in _read_persisted_records(tracker_a)

    tracker_b.flush()
    tracker_c = _reload_ledger("test_cross_instance_lock", temp_dir)
    assert _get_response_event_id(tracker_c, "$first") == "$response-a"
    assert _get_response_event_id(tracker_c, "$second") == "$response-b"


def test_sibling_ledgers_merge_updates(temp_dir: Path) -> None:
    """Sibling ledgers should share and persist updates."""
    tracker_a = HandledTurnLedger("test_multi_instance", base_path=temp_dir)
    tracker_b = HandledTurnLedger("test_multi_instance", base_path=temp_dir)

    _record_handled_turn(tracker_a, ["$first"], response_event_id="$response-a")
    _record_handled_turn(tracker_b, ["$second"], response_event_id="$response-b")

    tracker_c = _reload_ledger("test_multi_instance", temp_dir)
    assert _get_response_event_id(tracker_c, "$first") == "$response-a"
    assert _get_response_event_id(tracker_c, "$second") == "$response-b"


def test_sibling_ledgers_share_live_state(temp_dir: Path) -> None:
    """Sibling ledgers should observe process-shared state."""
    tracker_a = HandledTurnLedger("test_multi_instance_reads", base_path=temp_dir)
    tracker_b = HandledTurnLedger("test_multi_instance_reads", base_path=temp_dir)

    _record_handled_turn(
        tracker_a,
        ["$first", "$second"],
        response_event_id="$response-a",
        source_event_prompts={"$first": "first", "$second": "second"},
    )

    assert tracker_b.has_responded("$first")
    assert _get_response_event_id(tracker_b, "$second") == "$response-a"
    turn_record = tracker_b.get_turn_record("$first")
    assert turn_record is not None
    assert turn_record.source_event_ids == ("$first", "$second")
    assert turn_record.source_event_prompts == {"$first": "first", "$second": "second"}


def test_quarantines_malformed_ledger_file(temp_dir: Path) -> None:
    """Malformed JSON should be quarantined so ledger initialization still succeeds."""
    responses_file = temp_dir / "bad_json_responded.json"
    responses_file.write_text("{not valid json", encoding="utf-8")

    tracker = HandledTurnLedger("bad_json", base_path=temp_dir)
    tracker.warm()

    assert tracker._responses == {}
    assert _read_persisted_records(tracker) == {}
    quarantined_files = list(temp_dir.glob("bad_json_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1


def test_quarantines_non_utf8_ledger_file(temp_dir: Path) -> None:
    """Invalid UTF-8 should be quarantined so ledger initialization still succeeds."""
    responses_file = temp_dir / "bad_utf8_responded.json"
    responses_file.write_bytes(b"\xff\xfe\x00")

    tracker = HandledTurnLedger("bad_utf8", base_path=temp_dir)
    tracker.warm()

    assert tracker._responses == {}
    assert _read_persisted_records(tracker) == {}
    quarantined_files = list(temp_dir.glob("bad_utf8_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1


def test_quarantines_structurally_invalid_ledger_file(temp_dir: Path) -> None:
    """Valid JSON with the wrong top-level shape should still be quarantined."""
    responses_file = temp_dir / "bad_shape_responded.json"
    responses_file.write_text(json.dumps(["oops"]), encoding="utf-8")

    tracker = HandledTurnLedger("bad_shape", base_path=temp_dir)
    tracker.warm()

    assert tracker._responses == {}
    assert _read_persisted_records(tracker) == {}
    quarantined_files = list(temp_dir.glob("bad_shape_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1
    assert json.loads(quarantined_files[0].read_text(encoding="utf-8")) == ["oops"]


def test_quarantines_ledger_file_with_invalid_event_entry(temp_dir: Path) -> None:
    """Per-event entries with invalid shapes should be quarantined before rewrite."""
    responses_file = temp_dir / "bad_entry_responded.json"
    invalid_payload = {
        "schema_version": TurnRecordCodec.schema_version(),
        "records": {"$event": []},
    }
    responses_file.write_text(json.dumps(invalid_payload), encoding="utf-8")

    tracker = HandledTurnLedger("bad_entry", base_path=temp_dir)
    tracker.warm()

    assert tracker._responses == {}
    assert _read_persisted_records(tracker) == {}
    quarantined_files = list(temp_dir.glob("bad_entry_responded.json.corrupt-*"))
    assert len(quarantined_files) == 1
    assert json.loads(quarantined_files[0].read_text(encoding="utf-8")) == invalid_payload


def test_partial_invalid_coalesced_ledger_rehydrates_and_persists_valid_group(temp_dir: Path) -> None:
    """A surviving coalesced row should restore its invalid sibling before the next write."""
    responses_file = temp_dir / "partial_bad_entry_responded.json"
    valid_record = TurnRecord.create(
        ["$valid", "$invalid"],
        response_event_id="$valid-response",
        timestamp=time.time(),
    )
    responses_file.write_text(
        json.dumps(
            {
                "schema_version": TurnRecordCodec.schema_version(),
                "records": {
                    "$valid": TurnRecordCodec.to_ledger_record(valid_record),
                    "$invalid": [],
                },
            },
        ),
        encoding="utf-8",
    )
    tracker = HandledTurnLedger("partial_bad_entry", base_path=temp_dir)

    assert tracker.has_responded("$valid")
    assert tracker.has_responded("$invalid")
    _record_handled_turn(tracker, ["$new"], response_event_id="$new-response")
    tracker.flush()

    reloaded = _reload_ledger("partial_bad_entry", temp_dir)
    reloaded.warm()
    assert reloaded.has_responded("$valid")
    assert reloaded.has_responded("$invalid")
    assert reloaded.has_responded("$new")
    assert _get_response_event_id(reloaded, "$valid") == "$valid-response"
    assert _get_response_event_id(reloaded, "$invalid") == "$valid-response"
    assert _get_response_event_id(reloaded, "$new") == "$new-response"
    assert not list(temp_dir.glob("partial_bad_entry_responded.json.corrupt-*"))


def test_concurrent_reads_fail_soft_on_corrupt_file(temp_dir: Path) -> None:
    """Concurrent reads over a corrupt file should fail soft from shared memory state."""
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


def test_record_outcome_overwrites_previous_response_event_id(temp_dir: Path) -> None:
    """A later outcome write should replace the stored response event ID."""
    tracker = HandledTurnLedger("test_replace_response", base_path=temp_dir)

    _record_handled_turn(tracker, ["$event"], response_event_id="$response-1")
    _record_handled_turn(tracker, ["$event"], response_event_id="$response-2")

    assert _get_response_event_id(tracker, "$event") == "$response-2"


def test_get_turn_record_returns_none_for_unknown_source(temp_dir: Path) -> None:
    """Missing sources should not synthesize turn records."""
    tracker = HandledTurnLedger("test_missing", base_path=temp_dir)

    assert tracker.get_turn_record("$missing") is None


def test_construction_touches_no_filesystem(temp_dir: Path) -> None:
    """Binding a ledger must stay free of filesystem access so bot init never stalls the loop."""
    missing_parent = temp_dir / "does-not-exist" / "tracking"

    tracker = HandledTurnLedger("test_lazy_init", base_path=missing_parent)

    assert not missing_parent.exists()
    assert tracker._responses == {}


@pytest.mark.asyncio
async def test_warm_on_slow_filesystem_does_not_block_event_loop(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """warm() must run its advisory-lock load off the loop so heartbeats keep ticking."""
    gate = threading.Event()
    real_lock = handled_turns_module.advisory_file_lock

    @contextmanager
    def gated_lock(lock_path: Path, *, exclusive: bool = True) -> object:
        gate.wait()
        with real_lock(lock_path, exclusive=exclusive):
            yield

    monkeypatch.setattr(handled_turns_module, "advisory_file_lock", gated_lock)
    tracker = HandledTurnLedger("test_slow_warm", base_path=temp_dir)
    warm_task = asyncio.create_task(asyncio.to_thread(tracker.warm))

    # The warm thread is parked on the gated lock; the loop must stay live.
    heartbeats = 0
    while heartbeats < 50:
        await asyncio.sleep(0)
        heartbeats += 1
    assert not warm_task.done()

    gate.set()
    await warm_task
    assert tracker._responses == {}


def test_record_returns_before_disk_persist_completes(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording must apply in memory and return while the disk merge is still blocked."""
    tracker = HandledTurnLedger("test_async_persist", base_path=temp_dir)
    tracker.warm()

    gate = threading.Event()
    real_lock = handled_turns_module.advisory_file_lock

    @contextmanager
    def gated_lock(lock_path: Path, *, exclusive: bool = True) -> object:
        gate.wait()
        with real_lock(lock_path, exclusive=exclusive):
            yield

    monkeypatch.setattr(handled_turns_module, "advisory_file_lock", gated_lock)

    # If recording touched the (gated) file lock on the calling thread this
    # would deadlock; returning at all proves the disk merge is write-behind.
    _record_handled_turn(tracker, ["$event"], response_event_id="$response")
    assert tracker.has_responded("$event")
    assert "$event" not in _read_persisted_records(tracker)

    gate.set()
    tracker.flush()
    persisted = _read_persisted_records(tracker)
    assert persisted["$event"]["response_event_id"] == "$response"
