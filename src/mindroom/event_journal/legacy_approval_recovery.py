"""Recognize approval ownership stranded by historical deleted-response cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import outbox
from .models import DeliveryStage
from .projection import is_tombstoned

if TYPE_CHECKING:
    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction

# Legacy format: A live approval owns a retired INITIAL whose sources and response were deleted.
# Last legacy release: v2026.9.63; replacement: unreleased approval-aware INITIAL cleanup.
# Handling: Recognize terminal deletion inside the caller's transaction; current owners expire cards,
# fence failure, settle sources, and delete the continuation without replaying tools or sending text.
# Coverage: tests/test_event_journal_store.py::TestApprovalContinuations::test_deleted_approval_failure_settles_only_proven_terminal_delivery
# and tests/test_response_runner_focused.py::test_deleted_approval_recovery_expires_cards_without_editing_or_executing.


def deleted_delivery_is_terminal(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
) -> bool:
    """Prove the old deletion state without changing rows or taking transaction ownership."""
    if continuation.state != "failing":
        return False
    delivery_id = continuation.source_event_ids[0]
    if outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=DeliveryStage.FINAL) is not None:
        return False
    initial = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=DeliveryStage.INITIAL)
    return (
        initial is not None
        and initial.retired
        and initial.room_id == continuation.room_id
        and initial.acknowledged_event_id == continuation.response_event_id
        and all(
            is_tombstoned(transaction, principal_id, room_id=continuation.room_id, event_id=event_id)
            for event_id in (*continuation.source_event_ids, continuation.response_event_id)
        )
    )
