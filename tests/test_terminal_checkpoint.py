"""Canonical TurnRecord terminal-edit checkpoint invariants."""

# ruff: noqa: D103

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mindroom.handled_turns import (
    TerminalEditCheckpoint,
    TurnRecord,
    TurnRecordCodec,
    _cleaned_responses,
)
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


def _checkpoint(*, after_response_claimed: bool = False) -> TerminalEditCheckpoint:
    return TerminalEditCheckpoint(
        transaction_id="mindroom-terminal-checkpoint-1",
        wire_content={
            "body": "* final",
            "m.new_content": {"body": "final", "msgtype": "m.text"},
        },
        response_text="final",
        response_kind="ai",
        target_was_placeholder=True,
        response_envelope={
            "source_event_id": "$source",
            "target": {
                "room_id": "!room:example.org",
                "thread_id": "$thread",
                "reply_to_event_id": "$source",
            },
            "body": "question",
        },
        correlation_id="corr-1",
        interactive_metadata={
            "question_text": "Proceed?",
            "option_map": {"1": "yes"},
            "option_labels": {"1": "Yes"},
            "options_list": [{"emoji": "✅", "label": "Yes", "value": "yes"}],
        },
        after_response_claimed=after_response_claimed,
    )


def _pending_turn() -> TurnRecord:
    return TurnRecord.create(
        ["$source"],
        completed=False,
        response_owner="agent",
        requester_id="@user:example.org",
        correlation_id="corr-1",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=MessageTarget.resolve(
            "!room:example.org",
            "$thread",
            "$source",
        ),
    )


def test_checkpoint_codec_round_trip_and_absent_legacy_field() -> None:
    checkpoint = _checkpoint()
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


def test_settled_terminal_delivery_receipt_round_trips_in_ledger() -> None:
    record = TurnRecord.create(
        ["$source"],
        response_event_id="$visible",
        correlation_id="corr-1",
        settled_terminal_delivery_correlation_id="corr-1",
    )

    decoded = TurnRecordCodec.from_ledger_record(
        "$source",
        TurnRecordCodec.to_ledger_record(record),
    )

    assert decoded is not None
    assert decoded.settled_terminal_delivery_correlation_id == "corr-1"


def test_checkpoint_commit_sets_canonical_terminal_authority_before_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None

    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )

    assert committed is not None
    assert committed.completed
    assert committed.response_event_id == "$visible"
    assert committed.terminal_edit_checkpoint == _checkpoint()
    restarted = _store(tmp_path).get_turn_record("$source")
    assert restarted == committed


def test_completed_turn_commits_newer_same_target_regeneration_checkpoint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    store.record_turn(replace(pending, completed=True, response_event_id="$visible"))
    completed = store.get_turn_record("$source")
    assert completed is not None
    candidate = replace(
        completed,
        correlation_id="$edit",
        source_event_revisions={"$source": (1, "$edit")},
    )
    edit_checkpoint = replace(
        _checkpoint(),
        transaction_id="mindroom-terminal-edit-episode",
        correlation_id="$edit",
        target_was_placeholder=False,
    )

    committed = store.commit_terminal_checkpoint(
        completed,
        response_event_id="$visible",
        checkpoint=edit_checkpoint,
        regeneration_turn_record=candidate,
    )

    assert committed is not None
    assert committed.response_event_id == "$visible"
    assert committed.terminal_edit_checkpoint == edit_checkpoint


def test_completed_turn_rejects_checkpoint_for_unrelated_regeneration_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    store.record_turn(replace(pending, completed=True, response_event_id="$visible"))
    completed = store.get_turn_record("$source")
    assert completed is not None

    committed = store.commit_terminal_checkpoint(
        completed,
        response_event_id="$visible",
        checkpoint=replace(
            _checkpoint(),
            correlation_id="$edit",
            target_was_placeholder=False,
        ),
        regeneration_turn_record=replace(
            completed,
            response_event_id="$other-visible",
            correlation_id="$edit",
            source_event_revisions={"$source": (1, "$edit")},
        ),
    )

    assert committed is None
    assert store.get_turn_record("$source") == completed


def test_completed_regeneration_episode_cannot_rearm_old_finalization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    completed_edit = replace(
        pending,
        completed=True,
        response_event_id="$visible",
        correlation_id="$edit",
        source_event_revisions={"$source": (1, "$edit")},
    )
    store.record_turn(completed_edit)
    completed_edit = store.get_turn_record("$source")
    assert completed_edit is not None

    assert (
        store.commit_terminal_checkpoint(
            completed_edit,
            response_event_id="$visible",
            checkpoint=replace(
                _checkpoint(),
                transaction_id="mindroom-terminal-duplicate-edit",
                correlation_id="$edit",
                target_was_placeholder=False,
            ),
            regeneration_turn_record=completed_edit,
        )
        is None
    )


def test_checkpoint_scan_returns_each_canonical_turn_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(
        TurnRecord.create(
            ["$first", "$second"],
            completed=False,
            response_owner="agent",
            correlation_id="corr-1",
        ),
    )
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )

    assert committed is not None
    assert store.terminal_checkpoint_records() == (committed,)


def test_checkpoint_persist_failure_does_not_publish_memory_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    store.flush()

    with (
        patch.object(store._ledger, "_persist_records", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        store.commit_terminal_checkpoint(
            pending,
            response_event_id="$visible",
            checkpoint=_checkpoint(),
        )

    assert store.get_turn_record("$source") == pending
    assert store.terminal_checkpoint_records() == ()


def test_checkpoint_clear_requires_matching_transaction_and_lifecycle_convergence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )
    assert committed is not None

    assert store.clear_terminal_checkpoint(committed, expected_transaction_id="stale") is None
    assert (
        store.clear_terminal_checkpoint(
            committed,
            expected_transaction_id=_checkpoint().transaction_id,
        )
        is None
    )
    converged = store.update_terminal_checkpoint(
        committed,
        expected_transaction_id=_checkpoint().transaction_id,
        update=lambda checkpoint: replace(
            checkpoint,
            after_response_claimed=True,
            interactive_completed=True,
        ),
    )
    assert converged is not None

    cleared = store.clear_terminal_checkpoint(
        converged,
        expected_transaction_id=_checkpoint().transaction_id,
    )

    assert cleared is not None
    assert cleared.terminal_edit_checkpoint is None
    assert cleared.settled_terminal_delivery_correlation_id == _checkpoint().correlation_id


@pytest.mark.parametrize("frozen_time", [100.0, 0.0], ids=["equal", "regressed"])
def test_checkpoint_lifecycle_cas_advances_without_wall_clock_order(
    tmp_path: Path,
    frozen_time: float,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None

    with patch("mindroom.handled_turns.time.time", return_value=frozen_time):
        committed = store.commit_terminal_checkpoint(
            pending,
            response_event_id="$visible",
            checkpoint=_checkpoint(),
        )
        assert committed is not None
        claimed = store.update_terminal_checkpoint(
            committed,
            expected_transaction_id=_checkpoint().transaction_id,
            update=lambda checkpoint: replace(
                checkpoint,
                after_response_claimed=True,
                interactive_completed=True,
            ),
        )
        assert claimed is not None
        cleared = store.clear_terminal_checkpoint(
            claimed,
            expected_transaction_id=_checkpoint().transaction_id,
        )

    assert cleared is not None
    assert cleared.terminal_edit_checkpoint is None


@pytest.mark.parametrize("redacted_event_id", ["$source", "$visible"], ids=["source", "target"])
def test_redaction_cas_advances_when_wall_clock_regresses(
    tmp_path: Path,
    redacted_event_id: str,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )
    assert committed is not None

    with patch("mindroom.handled_turns.time.time", return_value=0.0):
        store.mark_source_redacted(redacted_event_id)

    owner = store.get_turn_record("$source")
    assert owner is not None
    if redacted_event_id == "$source":
        assert owner.redacted_source_event_ids == ("$source",)
        assert owner.terminal_edit_checkpoint is not None
    else:
        assert owner.terminal_edit_checkpoint is None
        assert owner.response_event_id is None
        tombstone = store.get_turn_record("$visible")
        assert tombstone is not None
        assert tombstone.redacted_source_event_ids == ("$visible",)


def test_record_turn_merge_and_pruning_retain_unsettled_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(after_response_claimed=True),
    )
    assert committed is not None

    store.record_turn(replace(pending, completed=True, response_event_id="$visible"))
    merged = store.get_turn_record("$source")
    assert merged is not None
    assert merged.terminal_edit_checkpoint == committed.terminal_edit_checkpoint

    cleaned = _cleaned_responses(
        {"$source": replace(merged, timestamp=1.0)},
        max_events=0,
        max_age_days=0,
    )
    assert cleaned["$source"].terminal_edit_checkpoint == committed.terminal_edit_checkpoint


def test_target_redaction_atomically_clears_checkpoint_and_persists_tombstone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )
    assert committed is not None

    redacted = store.mark_source_redacted("$visible")

    assert redacted is not None
    owner = store.get_turn_record("$source")
    assert owner is not None
    assert owner.terminal_edit_checkpoint is None
    target_tombstone = store.get_turn_record("$visible")
    assert target_tombstone is not None
    assert target_tombstone.redacted_source_event_ids == ("$visible",)
    restarted = _store(tmp_path)
    assert restarted.get_turn_record("$source") == owner
    assert restarted.get_turn_record("$visible") == target_tombstone


def test_checkpoint_lookup_accepts_subset_or_reordering_but_rejects_new_ids(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(
        TurnRecord.create(
            ["$first", "$second"],
            discovery_event_ids=("$alias",),
            completed=False,
            response_owner="agent",
            correlation_id="corr-1",
        ),
    )
    assert pending is not None
    committed = store.commit_terminal_checkpoint(
        pending,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )
    assert committed is not None

    assert store.terminal_checkpoint_for_sources(("$second", "$first")) == committed
    assert store.terminal_checkpoint_for_sources(("$alias",)) == committed
    assert store.terminal_checkpoint_for_sources(("$first", "$new")) is None


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"transaction_id": ""}, ValueError),
        ({"wire_content": {1: "bad"}}, TypeError),
        ({"wire_content": {"bad": {"not-json"}}}, TypeError),
        ({"response_envelope": {}}, ValueError),
    ],
)
def test_checkpoint_constructor_rejects_state_the_codec_cannot_restore(
    changes: dict[str, object],
    error: type[Exception],
) -> None:
    fields = {
        "transaction_id": "mindroom-terminal-checkpoint-1",
        "wire_content": {"body": "final"},
        "response_text": "final",
        "response_kind": "ai",
        "target_was_placeholder": True,
        "response_envelope": {"source_event_id": "$source"},
        "correlation_id": "corr-1",
    }
    fields.update(changes)

    with pytest.raises(error):
        TerminalEditCheckpoint(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("source_event_ids", [("$first",), ("$second", "$first")])
def test_checkpoint_commit_rejects_stale_subset_or_reordered_caller(
    tmp_path: Path,
    source_event_ids: tuple[str, ...],
) -> None:
    store = _store(tmp_path)
    authority = store.record_pending_turn(
        TurnRecord.create(
            ["$first", "$second"],
            completed=False,
            response_owner="agent",
            correlation_id="corr-1",
        ),
    )
    assert authority is not None
    stale = TurnRecord.create(
        source_event_ids,
        completed=False,
        response_owner="agent",
        correlation_id="corr-1",
    )

    assert (
        store.commit_terminal_checkpoint(
            stale,
            response_event_id="$visible",
            checkpoint=_checkpoint(),
        )
        is None
    )
    assert store.get_turn_record("$first") == authority
    assert store.get_turn_record("$second") == authority


@pytest.mark.parametrize("conflicting_event_id", ["$second", "$alias"])
def test_checkpoint_commit_rejects_fragmented_physical_or_alias_mapping(
    tmp_path: Path,
    conflicting_event_id: str,
) -> None:
    store = _store(tmp_path)
    authority = store.record_pending_turn(
        TurnRecord.create(
            ["$first", "$second"],
            discovery_event_ids=("$alias",),
            completed=False,
            response_owner="agent",
            correlation_id="corr-1",
        ),
    )
    assert authority is not None
    incompatible = store.record_pending_turn(
        TurnRecord.create(
            [conflicting_event_id],
            completed=False,
            response_owner="agent",
            correlation_id="corr-other",
        ),
    )
    assert incompatible is not None
    assert store.get_turn_record("$first") == authority
    assert store.get_turn_record(conflicting_event_id) == incompatible

    assert (
        store.commit_terminal_checkpoint(
            authority,
            response_event_id="$visible",
            checkpoint=_checkpoint(),
        )
        is None
    )
    assert store.get_turn_record("$first") == authority
    assert store.get_turn_record(conflicting_event_id) == incompatible
    assert store.terminal_checkpoint_records() == ()


def test_target_redaction_and_checkpoint_commit_have_one_atomic_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record_pending_turn(_pending_turn())
    assert pending is not None
    barrier = threading.Barrier(2)
    result: list[TurnRecord | None] = []

    def commit() -> None:
        barrier.wait()
        result.append(
            store.commit_terminal_checkpoint(
                pending,
                response_event_id="$visible",
                checkpoint=_checkpoint(),
            ),
        )

    def redact() -> None:
        barrier.wait()
        store.mark_source_redacted("$visible")

    commit_thread = threading.Thread(target=commit)
    redact_thread = threading.Thread(target=redact)
    commit_thread.start()
    redact_thread.start()
    commit_thread.join()
    redact_thread.join()

    owner = store.get_turn_record("$source")
    tombstone = store.get_turn_record("$visible")
    assert owner is not None
    assert owner.terminal_edit_checkpoint is None
    assert tombstone is not None
    assert tombstone.redacted_source_event_ids == ("$visible",)
    assert len(result) == 1


def test_same_target_checkpoint_commit_supersedes_old_owner_before_redaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = store.record_pending_turn(_pending_turn())
    second = store.record_pending_turn(
        replace(
            _pending_turn(),
            source_event_ids=("$second",),
            anchor_event_id="$second",
            correlation_id="corr-2",
        ),
    )
    assert first is not None
    assert second is not None
    first_committed = store.commit_terminal_checkpoint(
        first,
        response_event_id="$visible",
        checkpoint=_checkpoint(),
    )
    second_checkpoint = replace(
        _checkpoint(),
        transaction_id="mindroom-terminal-checkpoint-2",
        correlation_id="corr-2",
    )
    second_committed = store.commit_terminal_checkpoint(
        second,
        response_event_id="$visible",
        checkpoint=second_checkpoint,
    )

    assert first_committed is not None
    assert second_committed is not None
    first_after = store.get_turn_record("$source")
    assert first_after is not None
    assert first_after.response_event_id is None
    assert first_after.terminal_edit_checkpoint is None
    assert store.turn_for_event("$visible") == second_committed
    assert store.terminal_checkpoint_records() == (second_committed,)

    store.mark_source_redacted("$visible")

    winner_after = store.get_turn_record("$second")
    assert winner_after is not None
    assert winner_after.terminal_edit_checkpoint is None
    tombstone = store.get_turn_record("$visible")
    assert tombstone is not None
    assert tombstone.redacted_source_event_ids == ("$visible",)


def test_concurrent_same_target_checkpoint_commits_leave_one_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.record_pending_turn(_pending_turn())
    second = store.record_pending_turn(
        replace(
            _pending_turn(),
            source_event_ids=("$second",),
            anchor_event_id="$second",
            correlation_id="corr-2",
        ),
    )
    assert first is not None
    assert second is not None
    barrier = threading.Barrier(2)
    results: list[TurnRecord | None] = []

    def commit(turn: TurnRecord, transaction_id: str) -> None:
        barrier.wait()
        results.append(
            store.commit_terminal_checkpoint(
                turn,
                response_event_id="$visible",
                checkpoint=replace(_checkpoint(), transaction_id=transaction_id),
            ),
        )

    threads = [
        threading.Thread(target=commit, args=(first, "mindroom-terminal-checkpoint-1")),
        threading.Thread(target=commit, args=(second, "mindroom-terminal-checkpoint-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    [winner] = store.terminal_checkpoint_records()
    assert all(result is not None for result in results)
    assert store.turn_for_event("$visible") == winner
    losers = [record for source in ("$source", "$second") if (record := store.get_turn_record(source)) != winner]
    assert len(losers) == 1
    assert losers[0].response_event_id is None
    assert losers[0].terminal_edit_checkpoint is None
