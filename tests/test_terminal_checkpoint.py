"""Durable state contract for terminal Matrix edits."""

# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from mindroom.handled_turns import TerminalEditCheckpoint, TurnRecord, TurnRecordCodec, _cleaned_responses
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.turn_store import TurnStore, TurnStoreDeps

if TYPE_CHECKING:
    from pathlib import Path


def _store(tmp_path: Path) -> TurnStore:
    return TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tmp_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _checkpoint(
    *,
    transaction_id: str = "mindroom-terminal-checkpoint-1",
) -> TerminalEditCheckpoint:
    return TerminalEditCheckpoint(
        transaction_id=transaction_id,
        wire_content={
            "body": "* final",
            "m.new_content": {"body": "final", "msgtype": "m.text"},
        },
        correlation_id="corr-1",
    )


def _pending_turn(
    *,
    source_event_ids: tuple[str, ...] = ("$source",),
) -> TurnRecord:
    return TurnRecord.create(
        source_event_ids,
        completed=False,
        response_owner="agent",
        requester_id="@user:example.org",
        correlation_id="corr-1",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=MessageTarget.resolve(
            "!room:example.org",
            "$thread",
            source_event_ids[-1],
        ),
    )


def _commit(store: TurnStore, turn: TurnRecord | None = None) -> TurnRecord:
    pending = store.record_pending_turn(turn or _pending_turn())
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )
    assert committed is not None
    return committed


def test_checkpoint_codec_round_trips_minimal_transport_state() -> None:
    checkpoint = replace(
        _checkpoint(),
        accepted_redacted_source_event_ids=("$already-redacted",),
    )
    record = TurnRecord.create(
        ["$source"],
        response_event_id="$visible",
        terminal_edit_checkpoint=checkpoint,
    )

    encoded = TurnRecordCodec.to_ledger_record(record)
    decoded = TurnRecordCodec.from_ledger_record("$source", encoded)
    legacy = TurnRecordCodec.from_ledger_record(
        "$source",
        {key: value for key, value in encoded.items() if key != "terminal_edit_checkpoint"},
    )

    assert decoded is not None
    assert decoded.terminal_edit_checkpoint == checkpoint
    assert legacy is not None
    assert legacy.terminal_edit_checkpoint is None


def test_checkpoint_commit_is_complete_and_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)

    committed = _commit(store)

    assert committed.completed
    assert committed.response_event_id == "$visible"
    assert committed.terminal_edit_checkpoint == _checkpoint()
    assert _store(tmp_path).get_turn_record("$source") == committed


def test_checkpoint_scan_returns_coalesced_turn_once(tmp_path: Path) -> None:
    store = _store(tmp_path)

    committed = _commit(store, _pending_turn(source_event_ids=("$first", "$second")))

    assert store.terminal_checkpoint_records() == (committed,)


def test_clear_requires_matching_transaction_and_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    committed = _commit(store)

    assert store.clear_terminal_checkpoint(committed, expected_transaction_id="stale") is None

    cleared = store.clear_terminal_checkpoint(
        committed,
        expected_transaction_id=_checkpoint().transaction_id,
    )

    assert cleared is not None
    assert cleared.terminal_edit_checkpoint is None
    restarted = _store(tmp_path).get_turn_record("$source")
    assert restarted is not None
    assert restarted.terminal_edit_checkpoint is None
    assert restarted.completed
    assert restarted.response_event_id == "$visible"


def test_response_event_reverse_lookup_finds_checkpoint_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    committed = _commit(store)

    assert store.turn_for_event("$visible") == committed


def test_record_merge_and_cleanup_retain_pending_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    committed = _commit(store)
    stale_outer_record = replace(
        _pending_turn(),
        completed=True,
        response_event_id="$visible",
    )

    store.record_turn(stale_outer_record)

    merged = store.get_turn_record("$source")
    assert merged is not None
    assert merged.terminal_edit_checkpoint == committed.terminal_edit_checkpoint
    cleaned = _cleaned_responses(
        {"$source": replace(merged, timestamp=1.0)},
        max_events=0,
        max_age_days=0,
    )
    assert cleaned["$source"].terminal_edit_checkpoint == committed.terminal_edit_checkpoint


def test_checkpoint_snapshots_redactions_already_accepted_by_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    turn = replace(
        _pending_turn(source_event_ids=("$redacted", "$surviving")),
        redacted_source_event_ids=("$redacted",),
    )

    committed = _commit(store, turn)

    assert committed.terminal_edit_checkpoint is not None
    assert committed.terminal_edit_checkpoint.accepted_redacted_source_event_ids == ("$redacted",)


def test_source_redaction_keeps_checkpoint_for_visible_target_cleanup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _commit(store)

    redacted = store.mark_source_redacted("$source")

    assert redacted is not None
    assert redacted.redacted_source_event_ids == ("$source",)
    assert redacted.terminal_edit_checkpoint is not None
    assert redacted.response_event_id == "$visible"


def test_target_redaction_tombstones_target_before_clearing_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _commit(store)

    store.mark_source_redacted("$visible")

    tombstone = store.get_turn_record("$visible")
    assert tombstone is not None
    assert tombstone.redacted_source_event_ids == ("$visible",)
    owner = store.get_turn_record("$source")
    assert owner is not None
    assert owner.terminal_edit_checkpoint is None
    assert owner.response_event_id is None


def test_conflicting_completed_turn_cannot_claim_another_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _commit(store)
    store.clear_terminal_checkpoint(
        first,
        expected_transaction_id=_checkpoint().transaction_id,
    )
    second_pending = store.record_pending_turn(
        replace(
            _pending_turn(source_event_ids=("$second",)),
            correlation_id="corr-2",
        ),
    )
    assert second_pending is not None

    second = store.commit_terminal_checkpoint(
        second_pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(transaction_id="mindroom-terminal-checkpoint-2"),
    )

    assert second is None
    assert store.get_turn_record("$second") == second_pending
