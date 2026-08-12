"""Tests for handled turn persistence and lookup."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import FrozenInstanceError, dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.event_journal.store import TurnRecordStore
from mindroom.handled_turns import (
    HandledTurnLedger,
    SourceEventMetadata,
    TurnRecord,
    TurnRecordCodec,
    _reset_handled_turn_ledger_runtime,
    canonicalize_turn_record,
)
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore


def _ledger(
    journal_store: EventJournalStore,
    agent_name: str,
    *,
    legacy_responses_file: Path | None = None,
) -> HandledTurnLedger:
    """Bind one ledger to this store's turn records without reading them."""
    return HandledTurnLedger(
        agent_name,
        records=journal_store.turn_records(agent_name),
        legacy_responses_file=legacy_responses_file,
    )


async def _open_ledger(
    journal_store: EventJournalStore,
    agent_name: str,
    *,
    legacy_responses_file: Path | None = None,
) -> HandledTurnLedger:
    """Return a ledger whose records are in memory and therefore readable."""
    ledger = _ledger(journal_store, agent_name, legacy_responses_file=legacy_responses_file)
    await ledger.load()
    return ledger


async def _reload_ledger(
    journal_store: EventJournalStore,
    agent_name: str,
    *,
    legacy_responses_file: Path | None = None,
) -> HandledTurnLedger:
    """Simulate a process restart: drop shared state, reload from the database."""
    _reset_handled_turn_ledger_runtime()
    return await _open_ledger(journal_store, agent_name, legacy_responses_file=legacy_responses_file)


def _write_legacy_ledger(path: Path, records: dict[str, dict[str, object]]) -> Path:
    """Write one pre-database JSON ledger exactly as the retired writer left it."""
    path.write_text(
        json.dumps({"schema_version": TurnRecordCodec.schema_version(), "records": records}),
        encoding="utf-8",
    )
    return path


def _legacy_record(
    source_event_ids: list[str],
    *,
    response_event_id: str | None,
    completed: bool = True,
    **extra: object,
) -> dict[str, object]:
    """Return one record in the physical shape the JSON ledger stored."""
    return {
        "anchor_event_id": source_event_ids[-1],
        "source_event_ids": source_event_ids,
        "redacted_source_event_ids": [],
        "pending_redaction_cleanup_event_ids": [],
        "response_event_id": response_event_id,
        "completed": completed,
        "timestamp": 1_000.0,
        **extra,
    }


async def _seed_records(
    journal_store: EventJournalStore,
    agent_name: str,
    responses: dict[str, dict[str, object]],
) -> None:
    """Write current-schema records straight to the database, bypassing the ledger.

    Reload and cleanup tests need records that this process never recorded, so
    they go in through the store rather than through ``record_handled_turn``.
    """
    records = journal_store.turn_records(agent_name)
    seeded: dict[tuple[str, ...], TurnRecord] = {}
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
            source_event_revisions=raw_record.get("source_event_revisions")
            if isinstance(raw_record.get("source_event_revisions"), dict)
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
        seeded.setdefault(record.indexed_event_ids, record)
    # One write per turn, indexed by every event that finds it. Writing a
    # coalesced turn once per source instead would have each write evict the
    # sibling rows the previous one just created, since they share an anchor.
    for indexed_event_ids, record in seeded.items():
        assert record.anchor_event_id is not None
        await records.upsert(
            index_event_ids=indexed_event_ids,
            anchor_event_id=record.anchor_event_id,
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
        )


async def _record_handled_turn(
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
    await tracker.record_handled_turn(
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


async def _read_persisted_records(
    journal_store: EventJournalStore,
    agent_name: str,
) -> dict[str, dict[str, object]]:
    """Return every stored record for one agent, keyed by the event that indexes it."""
    stored = await journal_store.turn_records(agent_name).load_all()
    return {index_event_id: json.loads(record_json) for index_event_id, _anchor, record_json in stored}


@pytest.mark.asyncio
async def test_handled_turn_ledger_init(journal_store: EventJournalStore) -> None:
    """Initialization should create an empty in-memory ledger."""
    tracker = await _open_ledger(journal_store, "test_agent")

    assert tracker.agent_name == "test_agent"
    assert tracker._responses == {}


@pytest.mark.asyncio
async def test_has_responded_empty(journal_store: EventJournalStore) -> None:
    """Unknown source events should not be marked handled."""
    tracker = await _open_ledger(journal_store, "test_empty")

    assert not tracker.has_responded("event123")
    assert tracker.get_turn_record("event123") is None


@pytest.mark.asyncio
async def test_reading_before_load_refuses_instead_of_answering_no(journal_store: EventJournalStore) -> None:
    """An unloaded ledger must refuse rather than report every turn unhandled.

    Reads are synchronous and answer from the in-memory map, so before ``load``
    fills it the honest answer is unavailable. Returning "no record" would read
    as "never handled" and the bot would answer a message it already answered.
    """
    tracker = _ledger(journal_store, "test_unloaded")

    with pytest.raises(RuntimeError, match="were read before they were loaded"):
        tracker.has_responded("$source")
    with pytest.raises(RuntimeError, match="were read before they were loaded"):
        tracker.get_turn_record("$source")

    await tracker.load()

    assert not tracker.has_responded("$source")


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


def test_turn_record_has_no_post_init_normalization_hook() -> None:
    """Canonical records should be constructed explicitly without hidden mutation."""
    assert "__post_init__" not in TurnRecord.__dict__


@pytest.mark.asyncio
async def test_ledger_canonicalizes_record_before_identity_resolution(journal_store: EventJournalStore) -> None:
    """The persistence boundary should compare canonical turn identities."""
    tracker = await _open_ledger(journal_store, "test_canonical_identity_boundary")
    await tracker.record_handled_turn(
        TurnRecord.create(["$source"], response_event_id="$old", timestamp=1.0),
    )

    await tracker.record_handled_turn(
        TurnRecord(source_event_ids=("$source",), response_event_id="$new", timestamp=2.0),
    )

    record = tracker.get_turn_record("$source")
    assert record is not None
    assert record.anchor_event_id == "$source"
    assert record.response_event_id == "$new"


def test_turn_record_create_normalizes_coupled_source_state() -> None:
    """The explicit factory should canonicalize interdependent source facts."""
    record = TurnRecord.create(
        ["source", "source", ""],
        discovery_event_ids=["source", "edit", "edit"],
        redacted_source_event_ids=["missing", "edit"],
        pending_redaction_cleanup_event_ids=["source", "edit"],
        source_event_prompts={"source": "prompt"},
        source_event_revisions={"edit": [4, "edit-event"]},
    )

    assert record.source_event_ids == ("source",)
    assert record.discovery_event_ids == ("edit",)
    assert record.redacted_source_event_ids == ("edit",)
    assert record.pending_redaction_cleanup_event_ids == ("edit",)
    assert record.source_event_prompts == {"source": "prompt"}
    assert record.source_event_revisions is None


def test_canonicalize_turn_record_prunes_new_redactions() -> None:
    """Explicit updates should reapply source-state invariants."""
    record = TurnRecord.create(
        ["first", "second"],
        source_event_prompts={"first": "one", "second": "two"},
    )

    updated = canonicalize_turn_record(
        record,
        redacted_source_event_ids=("first",),
        pending_redaction_cleanup_event_ids=("first", "second"),
    )

    assert updated.redacted_source_event_ids == ("first",)
    assert updated.pending_redaction_cleanup_event_ids == ("first",)
    assert updated.source_event_prompts == {"second": "two"}


def test_canonicalize_turn_record_links_command_result_to_started_checkpoint() -> None:
    """A stored command result should explicitly imply execution started."""
    record = TurnRecord.create(["source"], command_execution_started=False)

    updated = canonicalize_turn_record(record, command_result_text="done")

    assert updated.command_execution_started is True
    assert updated.command_result_text == "done"


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


@pytest.mark.asyncio
async def test_record_outcome_marks_single_source_event(journal_store: EventJournalStore) -> None:
    """A single-source outcome should mark the event terminally handled."""
    tracker = await _open_ledger(journal_store, "test_mark")

    before_time = time.time()
    await _record_handled_turn(tracker, ["event123"])
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


@pytest.mark.asyncio
async def test_record_handled_turn_tracks_typed_carrier(journal_store: EventJournalStore) -> None:
    """The ledger should record the typed handled-turn carrier without losing prompt metadata."""
    tracker = await _open_ledger(journal_store, "test_state_record")
    history_scope = HistoryScope(kind="agent", scope_id="test_state_record")
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )

    await tracker.record_handled_turn(
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


@pytest.mark.asyncio
async def test_record_outcome_tracks_response_event_id(journal_store: EventJournalStore) -> None:
    """A completed outcome should preserve the response event linkage."""
    tracker = await _open_ledger(journal_store, "test_linkage")

    await _record_handled_turn(tracker, ["event123"], response_event_id="$response")

    assert tracker.has_responded("event123")
    assert _get_response_event_id(tracker, "event123") == "$response"
    assert tracker.get_turn_record("event123") == TurnRecord(
        anchor_event_id="event123",
        source_event_ids=("event123",),
        response_event_id="$response",
        timestamp=tracker.get_turn_record("event123").timestamp,
    )


@pytest.mark.asyncio
async def test_record_outcome_deduplicates_source_event_ids(journal_store: EventJournalStore) -> None:
    """Duplicate source IDs should collapse into one stored turn record."""
    tracker = await _open_ledger(journal_store, "test_dedup")

    await _record_handled_turn(tracker, ["$a", "$a", "$b"], response_event_id="$response")

    assert len(tracker._responses) == 2
    assert tracker.get_turn_record("$a") is not None
    assert tracker.get_turn_record("$a").source_event_ids == ("$a", "$b")
    assert tracker.get_turn_record("$b").source_event_ids == ("$a", "$b")


@pytest.mark.asyncio
async def test_record_outcome_tracks_coalesced_turn(journal_store: EventJournalStore) -> None:
    """Coalesced outcomes should persist one shared turn record per source ID."""
    tracker = await _open_ledger(journal_store, "test_coalesced")

    await _record_handled_turn(
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


@pytest.mark.asyncio
async def test_is_coalesced_false_for_single_source(journal_store: EventJournalStore) -> None:
    """Single-source turns should not report coalescing."""
    tracker = await _open_ledger(journal_store, "test_single")

    await _record_handled_turn(tracker, ["$single"])

    turn_record = tracker.get_turn_record("$single")
    assert turn_record is not None
    assert not turn_record.is_coalesced


@pytest.mark.asyncio
async def test_record_outcome_filters_prompt_map_to_source_ids(journal_store: EventJournalStore) -> None:
    """Only prompts for recorded source IDs should be persisted."""
    tracker = await _open_ledger(journal_store, "test_prompt_filter")

    await _record_handled_turn(
        tracker,
        ["$a", "$b"],
        response_event_id="$response",
        source_event_prompts={"$a": "prompt a", "$extra": "ignored"},
    )

    turn_record = tracker.get_turn_record("$a")
    assert turn_record is not None
    assert turn_record.source_event_prompts == {"$a": "prompt a"}


@pytest.mark.asyncio
async def test_visible_echo_tracking_stays_partial_until_completed(journal_store: EventJournalStore) -> None:
    """The ledger should persist an exact partial record without completing it."""
    tracker = await _open_ledger(journal_store, "test_visible_echo")

    await tracker.record_handled_turn(
        TurnRecord.create(["event123"], completed=False, visible_echo_event_id="$echo"),
    )

    assert not tracker.has_responded("event123")
    assert _get_response_event_id(tracker, "event123") is None
    assert tracker.get_visible_echo_event_id("event123") == "$echo"
    turn_record = tracker.get_turn_record("event123")
    assert turn_record is not None
    assert not turn_record.completed
    assert turn_record.visible_echo_event_id == "$echo"


@pytest.mark.asyncio
async def test_visible_echo_persists_across_reload(journal_store: EventJournalStore) -> None:
    """Visible echoes should survive a new ledger instance on the same database."""
    tracker1 = await _open_ledger(journal_store, "test_visible_echo_reload")

    await tracker1.record_handled_turn(
        TurnRecord.create(["event123"], completed=False, visible_echo_event_id="$echo"),
    )

    tracker2 = await _reload_ledger(journal_store, "test_visible_echo_reload")

    assert not tracker2.has_responded("event123")
    assert tracker2.get_visible_echo_event_id("event123") == "$echo"
    turn_record = tracker2.get_turn_record("event123")
    assert turn_record is not None
    assert not turn_record.completed
    assert turn_record.visible_echo_event_id == "$echo"


@pytest.mark.asyncio
async def test_source_event_metadata_persists_across_reload(journal_store: EventJournalStore) -> None:
    """Coalesced source-event metadata should survive a ledger reload as floats."""
    tracker1 = await _open_ledger(journal_store, "test_source_metadata_reload")
    await tracker1.record_handled_turn(
        TurnRecord.create(
            ["$first", "$second"],
            discovery_event_ids=["$human-first"],
            response_event_id="$response",
            source_event_prompts={"$first": "first", "$second": "second"},
            source_event_metadata={
                "$first": SourceEventMetadata(
                    sender="@alice:localhost",
                    timestamp_ms=1_774_019_700_000,
                    discovery_event_id="$human-first",
                ),
                "$second": SourceEventMetadata(sender="@bob:localhost", timestamp_ms=None),
            },
        ),
    )

    tracker2 = await _reload_ledger(journal_store, "test_source_metadata_reload")
    turn_record = tracker2.get_turn_record("$second")

    assert turn_record is not None
    assert turn_record.source_event_metadata == {
        "$first": SourceEventMetadata(
            sender="@alice:localhost",
            timestamp_ms=1_774_019_700_000.0,
            discovery_event_id="$human-first",
        ),
        "$second": SourceEventMetadata(sender="@bob:localhost", timestamp_ms=None),
    }
    redacted = canonicalize_turn_record(turn_record, redacted_source_event_ids=("$human-first",))
    assert redacted.replay_source_event_ids == ("$second",)
    assert redacted.source_event_prompts == {"$second": "second"}
    await tracker2.record_handled_turn(redacted)

    tracker3 = await _reload_ledger(journal_store, "test_source_metadata_reload")
    reloaded = tracker3.get_turn_record("$human-first")
    assert reloaded is not None
    assert reloaded.redacted_source_event_ids == ("$human-first",)
    assert reloaded.source_event_prompts == {"$second": "second"}


def test_turn_record_cannot_mutate_after_ledger_publication() -> None:
    """Turn records are immutable snapshots once shared with ledger readers and writers."""
    record = TurnRecord.create(["$source"], response_event_id="$response")

    with pytest.raises(FrozenInstanceError):
        record.response_event_id = "$replacement"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_command_execution_checkpoint_persists_across_restart(journal_store: EventJournalStore) -> None:
    """Command effect and result evidence must survive process replacement."""
    tracker = await _open_ledger(journal_store, "test_command_checkpoint")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$command"],
            completed=False,
            command_execution_started=True,
            command_result_text="✅ Applied once",
        ),
    )

    reloaded = await _reload_ledger(journal_store, "test_command_checkpoint")
    recovered = reloaded.get_turn_record("$command")

    assert recovered is not None
    assert recovered.completed is False
    assert recovered.command_execution_started
    assert recovered.command_result_text == "✅ Applied once"


@pytest.mark.asyncio
async def test_source_event_revisions_persist_across_restart_and_run_recovery(
    journal_store: EventJournalStore,
) -> None:
    """Per-source edit order should survive both durable turn projections."""
    revisions = {
        "$first": (1_000_010, "$edit-first"),
        "$second": (1_000_020, "$edit-second"),
    }
    record = TurnRecord.create(
        ["$first", "$second"],
        response_event_id="$response",
        source_event_prompts={"$first": "edited first", "$second": "edited second"},
        source_event_revisions=revisions,
        requester_id="@user:example.com",
    )
    tracker = await _open_ledger(journal_store, "test_source_revisions_reload")
    await tracker.record_handled_turn(record)

    reloaded_ledger = await _reload_ledger(journal_store, "test_source_revisions_reload")
    reloaded = reloaded_ledger.get_turn_record("$second")

    assert reloaded is not None
    assert reloaded.source_event_revisions == revisions

    run_metadata = TurnRecordCodec.to_run_metadata(record)
    run_metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = "$second"
    run_metadata[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = "$response"
    recovered = TurnRecordCodec.from_run_metadata(run_metadata)

    assert recovered is not None
    assert recovered.source_event_revisions == revisions
    assert recovered.requester_id == "@user:example.com"


@pytest.mark.asyncio
async def test_user_stop_state_persists_across_restart(journal_store: EventJournalStore) -> None:
    """Durable STOP order and visible completion survive outside run metadata."""
    tracker = await _open_ledger(journal_store, "test_user_stop_cutoff")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$source"],
            response_event_id="$response",
            latest_edit_receipt_order=6,
            user_stop_receipt_order=7,
            user_stop_settled_receipt_order=7,
        ),
    )

    reloaded = await _reload_ledger(journal_store, "test_user_stop_cutoff")
    recovered = reloaded.get_turn_record("$source")

    assert recovered is not None
    assert recovered.latest_edit_receipt_order == 6
    assert recovered.user_stop_receipt_order == 7
    assert recovered.user_stop_settled_receipt_order == 7


@pytest.mark.asyncio
async def test_suppressed_source_event_revisions_persist_across_restart(journal_store: EventJournalStore) -> None:
    """Hook suppression must survive Matrix replay through the durable ledger."""
    suppressed_revisions = {"$source": (1_000_010, "$edit")}
    tracker = await _open_ledger(journal_store, "test_suppressed_source_revisions_reload")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$source"],
            response_event_id="$response",
            source_event_revisions=suppressed_revisions,
            suppressed_source_event_revisions=suppressed_revisions,
        ),
    )

    reloaded_ledger = await _reload_ledger(journal_store, "test_suppressed_source_revisions_reload")
    reloaded = reloaded_ledger.get_turn_record("$source")

    assert reloaded is not None
    assert reloaded.suppressed_source_event_revisions == suppressed_revisions


def test_source_event_revisions_keep_only_valid_live_sources() -> None:
    """Revision identity should stay bounded to replayable sources in one turn."""
    record = TurnRecord.create(
        ["$first", "$second"],
        discovery_event_ids=["$malformed"],
        redacted_source_event_ids=["$second"],
        source_event_revisions={
            "$first": (1_000_010, "$edit-first"),
            "$second": (1_000_020, "$edit-second"),
            "$extra": (1_000_030, "$edit-extra"),
            "$malformed": ["bad-timestamp", "$edit-malformed"],
        },
    )

    assert record.source_event_revisions == {
        "$first": (1_000_010, "$edit-first"),
    }


@pytest.mark.asyncio
async def test_missing_source_event_metadata_loads_as_none(journal_store: EventJournalStore) -> None:
    """Records persisted before source_event_metadata existed should load cleanly as None."""
    tracker1 = await _open_ledger(journal_store, "test_source_metadata_absent")
    await _record_handled_turn(
        tracker1,
        ["$first", "$second"],
        response_event_id="$response",
        source_event_prompts={"$first": "first", "$second": "second"},
    )

    reloaded = await _reload_ledger(journal_store, "test_source_metadata_absent")
    turn_record = reloaded.get_turn_record("$second")

    assert turn_record is not None
    assert turn_record.source_event_metadata is None


@pytest.mark.asyncio
async def test_a_retired_input_snapshot_key_loads_without_dropping_the_record(
    journal_store: EventJournalStore,
) -> None:
    """A record written by a version that stored turn media still loads.

    Turn records used to carry an ``input_snapshot`` holding each media
    source's whole Matrix event, which for an encrypted attachment is its
    decryption key. That was removed rather than migrated, and rows already
    stored still have the key in them.

    Refusing such a record would discard the live turn identity around it, and
    a turn whose identity is gone is a turn the bot answers twice. So the key
    is dropped on the next write and everything beside it survives the load,
    which is the tolerance every retired optional field depends on.
    """
    await journal_store.turn_records("test_retired_snapshot").upsert(
        index_event_ids=("$sealed",),
        anchor_event_id="$sealed",
        record_json=json.dumps(
            {
                "anchor_event_id": "$sealed",
                "source_event_ids": ["$sealed"],
                "redacted_source_event_ids": [],
                "pending_redaction_cleanup_event_ids": [],
                "response_event_id": "$response",
                "completed": True,
                "timestamp": 1_000.0,
                "response_owner": "agent",
                "input_snapshot": {
                    "media_sources": [
                        {
                            "event_id": "$sealed",
                            "source": {
                                "event_id": "$sealed",
                                "type": "m.room.message",
                                "content": {
                                    "msgtype": "m.image",
                                    "body": "sealed.png",
                                    "file": {
                                        "url": "mxc://example.org/sealed",
                                        "key": {"k": "cipher-key-material", "alg": "A256CTR"},
                                        "iv": "initialization-vector",
                                    },
                                },
                            },
                        },
                    ],
                    "message_attachment_ids": ["att_first"],
                },
            },
        ),
    )

    tracker = await _open_ledger(journal_store, "test_retired_snapshot")
    turn_record = tracker.get_turn_record("$sealed")

    assert turn_record is not None
    assert turn_record.source_event_ids == ("$sealed",)
    assert turn_record.response_event_id == "$response"
    assert turn_record.response_owner == "agent"
    assert "input_snapshot" not in TurnRecordCodec._to_ledger_record(turn_record)


@pytest.mark.asyncio
async def test_record_outcome_with_empty_source_list_is_noop(journal_store: EventJournalStore) -> None:
    """Empty outcome batches should not mutate the ledger."""
    tracker = await _open_ledger(journal_store, "test_empty_batch")

    await _record_handled_turn(tracker, [])

    assert tracker._responses == {}


@pytest.mark.asyncio
async def test_persistence_round_trip(journal_store: EventJournalStore) -> None:
    """Ledger state should survive a new instance load from the database."""
    tracker1 = await _open_ledger(journal_store, "test_persist")
    await _record_handled_turn(
        tracker1,
        ["$first", "$second"],
        response_event_id="$response",
        source_event_prompts={"$first": "first", "$second": "second"},
    )

    tracker2 = await _reload_ledger(journal_store, "test_persist")

    assert tracker2.has_responded("$first")
    assert tracker2.has_responded("$second")
    assert _get_response_event_id(tracker2, "$second") == "$response"
    assert tracker2.get_turn_record("$second").source_event_prompts == {
        "$first": "first",
        "$second": "second",
    }


@pytest.mark.asyncio
async def test_record_is_durable_when_the_call_returns(journal_store: EventJournalStore) -> None:
    """Recording awaits its own write, so no flush stands between it and durability.

    The file-backed ledger returned as soon as the record reached memory and
    persisted behind the caller, which is why callers that could not tolerate
    losing it had to ask for a durability barrier. There is no such barrier to
    forget any more.
    """
    tracker = await _open_ledger(journal_store, "test_durable_on_return")

    await _record_handled_turn(tracker, ["$event"], response_event_id="$response")

    persisted = await _read_persisted_records(journal_store, "test_durable_on_return")
    assert persisted["$event"]["response_event_id"] == "$response"


@dataclass(frozen=True, slots=True)
class _FailingWriteStore(TurnRecordStore):
    """A record store whose write stays open until the test makes it fail."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def upsert(
        self,
        *,
        index_event_ids: Sequence[str],
        anchor_event_id: str,
        record_json: str,
    ) -> None:
        """Hold the write open, then fail it, leaving the database untouched."""
        _ = (index_event_ids, anchor_event_id, record_json)
        self.started.set()
        await self.released.wait()
        msg = "the journal refused the record"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_a_cancelled_failed_write_stops_claiming_the_turn_was_handled(
    journal_store: EventJournalStore,
) -> None:
    """A write that failed after its caller was cancelled must take its publication back.

    The record is published to memory before the write so no reader sees "not
    handled" while the write is in flight. A cancelled caller that never learns
    the write failed leaves that publication standing over a database with no
    such record: this process refuses to answer the message, and the restart
    that reads the database answers it a second time.
    """
    records = _FailingWriteStore(_backend=journal_store.backend, _agent_name="test_cancelled_failed_write")
    ledger = HandledTurnLedger("test_cancelled_failed_write", records=records)
    await ledger.load()

    recording = asyncio.create_task(
        ledger.record_handled_turn(TurnRecord.create(["$event"], response_event_id="$response")),
    )
    await records.started.wait()
    recording.cancel()
    # One turn of the loop, so the cancellation lands while the write is still
    # open rather than after it has already reported its failure.
    await asyncio.sleep(0)
    assert ledger.has_responded("$event"), "nothing was published, so the rollback would prove nothing"
    records.released.set()

    with pytest.raises(asyncio.CancelledError):
        await recording

    assert not ledger.has_responded("$event")
    assert ledger.get_turn_record("$event") is None
    assert await _read_persisted_records(journal_store, "test_cancelled_failed_write") == {}


@dataclass(frozen=True, slots=True)
class _CommittingWriteStore(TurnRecordStore):
    """A record store whose write stays open until the test lets it commit for real."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def upsert(
        self,
        *,
        index_event_ids: Sequence[str],
        anchor_event_id: str,
        record_json: str,
    ) -> None:
        """Hold the write open, then let it land exactly as the real one would."""
        self.started.set()
        await self.released.wait()
        await TurnRecordStore.upsert(
            self,
            index_event_ids=index_event_ids,
            anchor_event_id=anchor_event_id,
            record_json=record_json,
        )


@pytest.mark.asyncio
async def test_a_cancelled_committed_write_keeps_claiming_the_turn_was_handled(
    journal_store: EventJournalStore,
) -> None:
    """A write that committed after its caller was cancelled must keep its publication.

    Cancelling the await does not cancel the write. The backend hands the
    statement to a worker that outlives the await, so the transaction commits
    while the caller is already unwinding. Rolling the publication back on the
    strength of the cancellation alone therefore erases a record the database
    holds: this process reports the source unhandled and answers the same
    message a second time, which is the exact outcome publishing before the
    commit exists to prevent.

    The rollback is only correct for a write that reported failure, so this
    pairs with the failed-write test above: between them they pin both answers
    the branch can give.
    """
    records = _CommittingWriteStore(_backend=journal_store.backend, _agent_name="test_cancelled_committed_write")
    ledger = HandledTurnLedger("test_cancelled_committed_write", records=records)
    await ledger.load()

    recording = asyncio.create_task(
        ledger.record_handled_turn(TurnRecord.create(["$event"], response_event_id="$response")),
    )
    await records.started.wait()
    recording.cancel()
    # One turn of the loop, so the cancellation lands while the write is still
    # open rather than after it has already committed.
    await asyncio.sleep(0)
    records.released.set()

    with pytest.raises(asyncio.CancelledError):
        await recording

    persisted = await _read_persisted_records(journal_store, "test_cancelled_committed_write")
    assert persisted["$event"]["response_event_id"] == "$response", "the write did land, so there is a claim to keep"
    assert ledger.has_responded("$event")
    assert _get_response_event_id(ledger, "$event") == "$response"


@pytest.mark.asyncio
async def test_discovery_alias_persists_without_becoming_a_coalesced_source(
    journal_store: EventJournalStore,
) -> None:
    """Discovery aliases should rehydrate to the canonical record without changing source semantics."""
    tracker1 = await _open_ledger(journal_store, "test_discovery_alias")
    await tracker1.record_handled_turn(
        TurnRecord.create(
            ["$question"],
            discovery_event_ids=["$selection"],
            response_event_id="$response",
        ),
    )

    tracker2 = await _reload_ledger(journal_store, "test_discovery_alias")

    question_record = tracker2.get_turn_record("$question")
    selection_record = tracker2.get_turn_record("$selection")
    assert question_record is not None
    assert selection_record == question_record
    assert question_record.source_event_ids == ("$question",)
    assert question_record.discovery_event_ids == ("$selection",)
    assert not question_record.is_coalesced


@pytest.mark.asyncio
async def test_discovery_alias_redaction_and_cleanup_intent_persist(journal_store: EventJournalStore) -> None:
    """Selection aliases must retain both their tombstone and owed cleanup across restart."""
    tracker = await _open_ledger(journal_store, "test_discovery_redaction")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$question"],
            discovery_event_ids=["$selection"],
            redacted_source_event_ids=["$selection"],
            pending_redaction_cleanup_event_ids=["$selection"],
            completed=False,
        ),
    )

    reloaded = await _reload_ledger(journal_store, "test_discovery_redaction")
    record = reloaded.get_turn_record("$selection")

    assert record is not None
    assert record.redacted_source_event_ids == ("$selection",)
    assert record.pending_redaction_cleanup_event_ids == ("$selection",)
    assert reloaded.pending_redaction_cleanup_event_ids() == ("$selection",)
    assert reloaded.has_responded("$selection") is True
    assert reloaded.has_responded("$question") is False


@pytest.mark.asyncio
async def test_persistence_round_trip_preserves_response_context(journal_store: EventJournalStore) -> None:
    """Reloaded ledgers should preserve response owner, history scope, and target metadata."""
    tracker1 = await _open_ledger(journal_store, "test_persist_context")
    history_scope = HistoryScope(kind="team", scope_id="team_scope")
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )
    await _record_handled_turn(
        tracker1,
        ["$original", "$reply"],
        response_event_id="$response",
        source_event_prompts={"$original": "original", "$reply": "reply"},
        response_owner="test_team",
        history_scope=history_scope,
        conversation_target=conversation_target,
    )

    tracker2 = await _reload_ledger(journal_store, "test_persist_context")

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


@pytest.mark.asyncio
async def test_persistence_round_trip_preserves_requester_and_correlation(
    journal_store: EventJournalStore,
) -> None:
    """Reloaded ledgers should preserve requester and correlation ids."""
    tracker1 = await _open_ledger(journal_store, "test_persist_request_context")
    await _record_handled_turn(
        tracker1,
        ["$original", "$reply"],
        response_event_id="$response",
        requester_id="@user:example.com",
        correlation_id="corr-123",
    )

    tracker2 = await _reload_ledger(journal_store, "test_persist_request_context")

    turn_record = tracker2.get_turn_record("$reply")
    assert turn_record is not None
    assert turn_record.requester_id == "@user:example.com"
    assert turn_record.correlation_id == "corr-123"


@pytest.mark.asyncio
async def test_record_without_requester_or_correlation_loads_cleanly(journal_store: EventJournalStore) -> None:
    """Requester and correlation IDs remain optional record context."""
    await _seed_records(
        journal_store,
        "missing_request_context",
        {
            "$event": {
                "timestamp": time.time(),
                "response_event_id": "$response",
                "completed": True,
            },
        },
    )

    reloaded = await _open_ledger(journal_store, "missing_request_context")
    turn_record = reloaded.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"
    assert turn_record.requester_id is None
    assert turn_record.correlation_id is None


def test_current_codec_rejects_incomplete_ledger_records() -> None:
    """Current-version ledger rows require the full canonical identity and outcome fields."""
    assert TurnRecordCodec._from_ledger_record("$event", {}) is None
    assert (
        TurnRecordCodec._from_ledger_record(
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


@pytest.mark.asyncio
async def test_large_coalesced_turn_round_trips(journal_store: EventJournalStore) -> None:
    """Large coalesced prompt maps should survive the write and reload intact."""
    tracker = await _open_ledger(journal_store, "test_large_coalesced")
    source_event_ids = [f"$event-{index}" for index in range(200)]
    prompt_map = {event_id: f"prompt {index}" for index, event_id in enumerate(source_event_ids)}

    await _record_handled_turn(
        tracker,
        source_event_ids,
        response_event_id="$response",
        source_event_prompts=prompt_map,
    )

    reloaded = await _reload_ledger(journal_store, "test_large_coalesced")

    turn_record = reloaded.get_turn_record(source_event_ids[-1])
    assert turn_record is not None
    assert turn_record.source_event_ids == tuple(source_event_ids)
    assert turn_record.source_event_prompts == prompt_map


@pytest.mark.asyncio
async def test_cleanup_by_count_keeps_most_recent_records(journal_store: EventJournalStore) -> None:
    """Cleanup should keep the newest events when count exceeds the cap."""
    base_time = time.time()
    responses = {
        f"event{index:03d}": {
            "timestamp": base_time + index,
            "response_event_id": None,
        }
        for index in range(20)
    }
    await _seed_records(journal_store, "test_cleanup", responses)
    tracker = await _open_ledger(journal_store, "test_cleanup")

    await tracker._cleanup_old_events(max_events=10)

    assert len(tracker._responses) == 10
    assert tracker.has_responded("event019")
    assert tracker.has_responded("event010")
    assert not tracker.has_responded("event009")


@pytest.mark.asyncio
async def test_cleanup_drops_the_records_it_evicts_from_the_database(
    journal_store: EventJournalStore,
) -> None:
    """Eviction must reach storage, not just memory, or a restart resurrects it."""
    base_time = time.time()
    await _seed_records(
        journal_store,
        "test_cleanup_deletes",
        {f"event{index:03d}": {"timestamp": base_time + index, "response_event_id": None} for index in range(6)},
    )
    tracker = await _open_ledger(journal_store, "test_cleanup_deletes")

    await tracker._cleanup_old_events(max_events=2)

    persisted = await _read_persisted_records(journal_store, "test_cleanup_deletes")
    assert set(persisted) == {"event004", "event005"}
    reloaded = await _reload_ledger(journal_store, "test_cleanup_deletes")
    assert not reloaded.has_responded("event000")
    assert reloaded.has_responded("event005")


@pytest.mark.asyncio
async def test_cleanup_by_count_keeps_coalesced_groups_intact(journal_store: EventJournalStore) -> None:
    """Count cleanup should evict entire coalesced turns rather than splitting them."""
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
    await _seed_records(journal_store, "test_cleanup_groups", responses)
    tracker = await _open_ledger(journal_store, "test_cleanup_groups")

    await tracker._cleanup_old_events(max_events=2)

    assert set(tracker._responses) == {"$c", "$d", "$e"}
    assert tracker.has_responded("$c")
    assert tracker.has_responded("$d")
    assert tracker.has_responded("$e")
    assert not tracker.has_responded("$a")
    assert not tracker.has_responded("$b")


@pytest.mark.asyncio
async def test_cleanup_by_age_removes_old_records(journal_store: EventJournalStore) -> None:
    """Cleanup should remove records older than the retention window."""
    current_time = time.time()
    responses: dict[str, dict[str, object]] = {}
    for index in range(5):
        responses[f"old_event{index}"] = {
            "timestamp": current_time - (40 * 24 * 60 * 60),
            "response_event_id": None,
        }
        responses[f"new_event{index}"] = {
            "timestamp": current_time - (10 * 24 * 60 * 60),
            "response_event_id": None,
        }
    await _seed_records(journal_store, "test_age_cleanup", responses)
    tracker = await _open_ledger(journal_store, "test_age_cleanup")

    await tracker._cleanup_old_events(max_events=100, max_age_days=30)

    assert len(tracker._responses) == 5
    for index in range(5):
        assert tracker.has_responded(f"new_event{index}")
        assert not tracker.has_responded(f"old_event{index}")


@pytest.mark.asyncio
async def test_cleanup_by_age_retains_pending_redaction_intent(journal_store: EventJournalStore) -> None:
    """Age retention must not discard cleanup work before the next response."""
    tracker = await _open_ledger(journal_store, "test_pending_age_cleanup")
    old_timestamp = time.time() - (40 * 24 * 60 * 60)
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$pending"],
            redacted_source_event_ids=["$pending"],
            pending_redaction_cleanup_event_ids=["$pending"],
            timestamp=old_timestamp,
        ),
    )
    await tracker.record_handled_turn(TurnRecord.create(["$ordinary"], timestamp=old_timestamp))

    await tracker._cleanup_old_events(max_events=100, max_age_days=30)

    assert tracker.get_turn_record("$pending") is not None
    assert tracker.pending_redaction_cleanup_event_ids() == ("$pending",)
    assert tracker.get_turn_record("$ordinary") is None


@pytest.mark.asyncio
async def test_cleanup_by_age_retains_incomplete_turn(journal_store: EventJournalStore) -> None:
    """Age cleanup must not discard a turn whose durable work is unfinished."""
    tracker = await _open_ledger(journal_store, "test_incomplete_age_cleanup")
    old_timestamp = time.time() - (40 * 24 * 60 * 60)
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$incomplete"],
            completed=False,
            command_execution_started=True,
            timestamp=old_timestamp,
        ),
    )
    await tracker.record_handled_turn(TurnRecord.create(["$terminal"], timestamp=old_timestamp))

    await tracker._cleanup_old_events(max_events=100, max_age_days=30)

    incomplete = tracker.get_turn_record("$incomplete")
    assert incomplete is not None
    assert incomplete.command_execution_started
    assert tracker.get_turn_record("$terminal") is None


@pytest.mark.asyncio
async def test_cleanup_by_age_retains_terminal_turn_for_unsettled_source(
    journal_store: EventJournalStore,
) -> None:
    """Cross-store cleanup must retain old terminal truth while dispatch still owns it."""
    tracker = await _open_ledger(journal_store, "test_unsettled_age_cleanup")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$terminal"],
            response_event_id="$response",
            timestamp=time.time() - (40 * 24 * 60 * 60),
        ),
    )

    await tracker.cleanup(unsettled_source_event_ids={"$terminal"})

    assert tracker.get_turn_record("$terminal") is not None

    await tracker.cleanup()

    assert tracker.get_turn_record("$terminal") is None


@pytest.mark.asyncio
async def test_cleanup_by_age_retains_only_unsettled_user_stop(journal_store: EventJournalStore) -> None:
    """A STOP-owned turn remains until its visible terminal edit is settled."""
    tracker = await _open_ledger(journal_store, "test_unsettled_stop_age_cleanup")
    old_timestamp = time.time() - (40 * 24 * 60 * 60)
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$unsettled-stop"],
            response_event_id="$unsettled-response",
            user_stop_receipt_order=2,
            timestamp=old_timestamp,
        ),
    )
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$settled-stop"],
            response_event_id="$settled-response",
            user_stop_receipt_order=3,
            user_stop_settled_receipt_order=3,
            timestamp=old_timestamp,
        ),
    )

    await tracker.cleanup()

    assert tracker.get_turn_record("$unsettled-stop") is not None
    assert tracker.get_turn_record("$settled-stop") is None


@pytest.mark.asyncio
async def test_cleanup_by_age_removes_terminal_redaction_only_turn(journal_store: EventJournalStore) -> None:
    """A fully redacted turn without cleanup or dispatch work must not live forever."""
    tracker = await _open_ledger(journal_store, "test_redacted_age_cleanup")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$redacted"],
            redacted_source_event_ids=["$redacted"],
            completed=False,
            timestamp=time.time() - (40 * 24 * 60 * 60),
        ),
    )

    await tracker.cleanup()

    assert tracker.get_turn_record("$redacted") is None


@pytest.mark.asyncio
async def test_cleanup_by_count_retains_pending_redaction_intent(journal_store: EventJournalStore) -> None:
    """Count retention may exceed its limit rather than lose owed cleanup work."""
    tracker = await _open_ledger(journal_store, "test_pending_count_cleanup")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$pending"],
            redacted_source_event_ids=["$pending"],
            pending_redaction_cleanup_event_ids=["$pending"],
            timestamp=time.time() - 2,
        ),
    )
    await tracker.record_handled_turn(TurnRecord.create(["$newest"], timestamp=time.time()))

    await tracker._cleanup_old_events(max_events=1, max_age_days=30)

    assert tracker.get_turn_record("$pending") is not None
    assert tracker.pending_redaction_cleanup_event_ids() == ("$pending",)
    assert tracker.get_turn_record("$newest") is not None


@pytest.mark.asyncio
async def test_cleanup_by_count_retains_incomplete_turn(journal_store: EventJournalStore) -> None:
    """Count cleanup may exceed its limit rather than discard unfinished work."""
    tracker = await _open_ledger(journal_store, "test_incomplete_count_cleanup")
    await tracker.record_handled_turn(
        TurnRecord.create(
            ["$incomplete"],
            completed=False,
            command_execution_started=True,
            timestamp=time.time() - 2,
        ),
    )
    await tracker.record_handled_turn(TurnRecord.create(["$newest"], timestamp=time.time()))

    await tracker._cleanup_old_events(max_events=1, max_age_days=30)

    incomplete = tracker.get_turn_record("$incomplete")
    assert incomplete is not None
    assert incomplete.command_execution_started
    assert tracker.get_turn_record("$newest") is not None


@pytest.mark.asyncio
async def test_concurrent_records_all_reach_storage(journal_store: EventJournalStore) -> None:
    """Interleaved recordings must all land, in memory and in the database."""
    tracker = await _open_ledger(journal_store, "test_concurrent")

    await asyncio.gather(
        *(
            _record_handled_turn(tracker, [f"event_{index}"], response_event_id=f"$response_{index}")
            for index in range(100)
        ),
    )

    assert len(tracker._responses) == 100
    persisted = await _read_persisted_records(journal_store, "test_concurrent")
    assert len(persisted) == 100


@pytest.mark.asyncio
async def test_sibling_ledgers_merge_updates(journal_store: EventJournalStore) -> None:
    """Sibling ledgers should share and persist updates."""
    tracker_a = await _open_ledger(journal_store, "test_multi_instance")
    tracker_b = await _open_ledger(journal_store, "test_multi_instance")

    await _record_handled_turn(tracker_a, ["$first"], response_event_id="$response-a")
    await _record_handled_turn(tracker_b, ["$second"], response_event_id="$response-b")

    tracker_c = await _reload_ledger(journal_store, "test_multi_instance")
    assert _get_response_event_id(tracker_c, "$first") == "$response-a"
    assert _get_response_event_id(tracker_c, "$second") == "$response-b"


@pytest.mark.asyncio
async def test_sibling_ledgers_share_live_state(journal_store: EventJournalStore) -> None:
    """Sibling ledgers should observe process-shared state."""
    tracker_a = await _open_ledger(journal_store, "test_multi_instance_reads")
    tracker_b = await _open_ledger(journal_store, "test_multi_instance_reads")

    await _record_handled_turn(
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


@pytest.mark.asyncio
async def test_record_outcome_overwrites_previous_response_event_id(journal_store: EventJournalStore) -> None:
    """A later outcome write should replace the stored response event ID."""
    tracker = await _open_ledger(journal_store, "test_replace_response")

    await _record_handled_turn(tracker, ["$event"], response_event_id="$response-1")
    await _record_handled_turn(tracker, ["$event"], response_event_id="$response-2")

    assert _get_response_event_id(tracker, "$event") == "$response-2"


@pytest.mark.asyncio
async def test_get_turn_record_returns_none_for_unknown_source(journal_store: EventJournalStore) -> None:
    """Missing sources should not synthesize turn records."""
    tracker = await _open_ledger(journal_store, "test_missing")

    assert tracker.get_turn_record("$missing") is None


@pytest.mark.asyncio
async def test_a_pre_database_ledger_is_adopted_on_first_load(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """An installation's existing terminal truth survives the move into the database.

    Without this the upgrade reads an empty table, concludes nothing has ever
    been answered, and re-answers the entire backlog on its first replay.
    """
    legacy_file = _write_legacy_ledger(
        tmp_path / "agent_responded.json",
        {"$source": _legacy_record(["$source"], response_event_id="$reply")},
    )

    tracker = await _open_ledger(journal_store, "legacy_adopted", legacy_responses_file=legacy_file)

    assert tracker.has_responded("$source")
    record = tracker.get_turn_record("$source")
    assert record is not None
    assert record.response_event_id == "$reply"
    # Adopted into the database, not just into memory: a restart reads rows.
    persisted = await _read_persisted_records(journal_store, "legacy_adopted")
    assert persisted["$source"]["response_event_id"] == "$reply"


@pytest.mark.asyncio
async def test_an_adopted_ledger_file_is_renamed_so_it_is_never_read_twice(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """The rename is the idempotence marker, and it is what makes compaction safe.

    Cleanup legitimately empties the table, which is the same condition that
    triggers the import. Only the rename distinguishes "never imported" from
    "imported and since compacted", so without it every compaction would
    resurrect the deleted history and answer those turns again.
    """
    legacy_file = _write_legacy_ledger(
        tmp_path / "agent_responded.json",
        {"$stale": _legacy_record(["$stale"], response_event_id="$reply")},
    )

    tracker = await _open_ledger(journal_store, "legacy_renamed", legacy_responses_file=legacy_file)

    assert not legacy_file.exists()
    assert legacy_file.with_suffix(".json.imported").exists()

    # Compact the adopted record away, then restart onto the empty table.
    await tracker._cleanup_old_events(max_events=0, max_age_days=0)
    assert await _read_persisted_records(journal_store, "legacy_renamed") == {}
    restarted = await _reload_ledger(journal_store, "legacy_renamed", legacy_responses_file=legacy_file)

    assert restarted.get_turn_record("$stale") is None


@pytest.mark.asyncio
async def test_a_partly_stored_legacy_turn_keeps_both_halves(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A coalesced legacy turn that overlaps a stored record only partially.

    This is the case whole-record upsert gets wrong in the most expensive way.
    The file holds one record indexing two sources of a coalesced turn; the
    runtime has since written a newer record under the first of them, and the
    second has no record at all.

    Upserting the legacy record overwrites the newer one, and the file is
    renamed immediately afterwards, so that copy is gone for good. Filtering
    the record out instead leaves the second source unrecorded, so a message
    that was already answered can be answered again. Only filling the gap and
    leaving the occupied index alone loses neither.
    """
    await _seed_records(
        journal_store,
        "partial_overlap",
        {"$first": {"source_event_ids": ["$first"], "response_event_id": "$current", "completed": True}},
    )
    legacy_file = _write_legacy_ledger(
        tmp_path / "agent_responded.json",
        {"$first": _legacy_record(["$first", "$second"], response_event_id="$legacy")},
    )

    tracker = await _open_ledger(journal_store, "partial_overlap", legacy_responses_file=legacy_file)

    assert _get_response_event_id(tracker, "$first") == "$current", "the newer record was overwritten"
    assert _get_response_event_id(tracker, "$second") == "$legacy", "the unrecorded source was not adopted"
    assert not legacy_file.exists(), "the file must still be retired"


@pytest.mark.asyncio
async def test_a_populated_database_is_never_overwritten_by_a_legacy_file(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A record the table already holds is never replaced by the file's copy.

    A non-empty table is not proof the file was imported, so ignoring the file
    outright would strand an import that crashed partway -- every turn it had
    not reached would stay missing for good. What must not happen is the
    opposite: a record already stored was written by this runtime or by an
    earlier pass, so it is at least as current as the file's, and overwriting
    it with an older copy would undo real work.

    So the file's *unseen* records are adopted, the stored one is left exactly
    as it is, and the file is renamed either way so no later pass can read it
    again.
    """
    await _seed_records(
        journal_store,
        "legacy_ignored",
        {"$current": {"timestamp": time.time(), "response_event_id": "$current-reply", "completed": True}},
    )
    legacy_file = _write_legacy_ledger(
        tmp_path / "agent_responded.json",
        {"$superseded": _legacy_record(["$superseded"], response_event_id="$old-reply")},
    )

    tracker = await _open_ledger(journal_store, "legacy_ignored", legacy_responses_file=legacy_file)

    assert _get_response_event_id(tracker, "$current") == "$current-reply", "a stored record was overwritten"
    assert _get_response_event_id(tracker, "$superseded") == "$old-reply", "an unseen record was not adopted"
    assert not legacy_file.exists()
    assert legacy_file.with_suffix(".json.imported").exists()


@pytest.mark.asyncio
async def test_an_absent_legacy_file_leaves_a_fresh_install_empty(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A fresh install has nothing to inherit and must not fail looking for it."""
    tracker = await _open_ledger(
        journal_store,
        "legacy_absent",
        legacy_responses_file=tmp_path / "never_written_responded.json",
    )

    assert tracker._responses == {}


@pytest.mark.asyncio
async def test_an_adopted_coalesced_turn_keeps_every_source_that_indexes_it(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A coalesced turn is adopted under every source, not just the one it is filed under.

    The JSON ledger stored one entry per source, so the obvious import writes
    each entry under its own event id. That is wrong here: ``upsert`` evicts
    the rows sharing an anchor that the new write does not name, so entry by
    entry the batch collapses to its last source and every earlier message in
    it gets answered a second time. The write has to carry the whole indexed
    set, which is what this pins.
    """
    coalesced = _legacy_record(["$first", "$second"], response_event_id="$reply")
    legacy_file = _write_legacy_ledger(
        tmp_path / "agent_responded.json",
        {"$first": coalesced, "$second": coalesced},
    )

    tracker = await _open_ledger(journal_store, "legacy_coalesced", legacy_responses_file=legacy_file)

    assert tracker.has_responded("$first")
    assert tracker.has_responded("$second")
    assert set(await _read_persisted_records(journal_store, "legacy_coalesced")) == {"$first", "$second"}


@pytest.mark.asyncio
async def test_an_adopted_record_survives_a_key_this_version_retired(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """The upgrade path inherits the same tolerance an ordinary load has.

    A file written before ``input_snapshot`` was retired still has it, and
    refusing that record would discard the live turn identity around it -- a
    turn whose identity is gone is a turn the bot answers twice.
    """
    legacy_file = _write_legacy_ledger(
        tmp_path / "agent_responded.json",
        {
            "$sealed": _legacy_record(
                ["$sealed"],
                response_event_id="$reply",
                input_snapshot={"media_sources": [], "message_attachment_ids": ["att_first"]},
            ),
        },
    )

    tracker = await _open_ledger(journal_store, "legacy_retired_key", legacy_responses_file=legacy_file)

    record = tracker.get_turn_record("$sealed")
    assert record is not None
    assert record.response_event_id == "$reply"
    assert "input_snapshot" not in TurnRecordCodec._to_ledger_record(record)
