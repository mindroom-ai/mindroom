"""Approve one pending call without a card and publish its approved receipt.

Timed grants and scheduled-call approvals each decide which calls they cover
and record their own audit; both then apply this one transition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import approval_card_state, outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from .approval_card_state import ApprovalCardReservation, ApprovalDecisionMetadata
    from .backend import Transaction

__all__ = ["apply"]


def apply(
    transaction: Transaction,
    principal_id: str,
    *,
    continuation_principal_id: str,
    call_identity: tuple[str, int, str],
    card: ApprovalCardReservation,
    room_id: str,
    thread_id: str,
    membership_epoch: int,
    metadata: ApprovalDecisionMetadata,
    now_ns: int,
) -> bool:
    """Approve the still-pending call and reserve its receipt in place of its card, or change nothing."""
    approval_id, generation, tool_call_id = call_identity
    decided = transaction.fetchone(
        """
        UPDATE approval_continuation_calls SET decision = 'approved'
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND tool_call_id = ?
          AND decision IS NULL AND expires_at_ns > ?
        RETURNING tool_call_id
        """,
        (continuation_principal_id, approval_id, generation, tool_call_id, now_ns),
    )
    if decided is None:
        return False
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=card.delivery_id,
        stage=DeliveryStage.INITIAL,
        event_type=card.event_type,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
        payload=approval_card_state.terminal_content(
            card.payload,
            status="approved",
            reason=None,
            metadata=metadata,
            publication="receipt",
        ),
        edits_event_id=None,
    )
    return True
