"""Exact durable transactions for background-script approval calls."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .approval_card_state import ApprovalCardReservation, RecordedApprovalDecision
    from .backend import Transaction

from . import approval_card_state, reads

__all__ = [
    "BackgroundApprovalDecision",
    "background_identity",
    "decision",
    "prune_calls",
    "reserve_delivery",
    "resolve",
    "resolve_call",
    "resolve_pending_calls",
]


@dataclass(frozen=True, slots=True)
class BackgroundApprovalDecision:
    """One durable terminal decision for an exact background-script call."""

    status: Literal["approved", "denied", "expired"]
    reason: str | None


def background_identity(card: Mapping[str, Any]) -> tuple[str, str]:
    """Extract strict background-script exact-call identity from one card."""
    content = card.get("content")
    if not isinstance(content, dict) or content.get("approval_target") != "background_script":
        msg = "Approval card is missing background-script target identity."
        raise TypeError(msg)
    run_id = content.get("background_run_id")
    call_id = content.get("background_call_id")
    if not isinstance(run_id, str) or not run_id or not isinstance(call_id, str) or not call_id:
        msg = "Approval card is missing background-script target identity."
        raise ValueError(msg)
    return run_id, call_id


def reserve_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    run_id: str,
    call_id: str,
    expires_at_ns: int,
    card: ApprovalCardReservation,
) -> bool:
    """Atomically reserve one exact background-call target and frozen card."""
    if card.tool_call_id != call_id or background_identity({"content": card.payload}) != (run_id, call_id):
        msg = f"Background approval delivery {card.delivery_id!r} changed exact-call identity"
        raise ValueError(msg)
    epoch = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    membership_epoch = 0 if epoch is None else int(epoch["membership_epoch"])
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    ):
        return False
    inserted = transaction.fetchone(
        """
        INSERT INTO background_approval_calls (
            principal_id, delivery_id, run_id, call_id, expires_at_ns, decision, reason
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT (principal_id, run_id, call_id) DO NOTHING
        RETURNING delivery_id
        """,
        (
            principal_id,
            card.delivery_id,
            run_id,
            call_id,
            expires_at_ns,
        ),
    )
    if inserted is None:
        return True
    approval_card_state.reserve_delivery(
        transaction,
        principal_id,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
        identity=(run_id, -1, call_id),
        card=card,
    )
    return True


def resolve(
    transaction: Transaction,
    principal_id: str,
    *,
    requested_status: Literal["approved", "denied", "expired"],
    reason: str | None,
    resolution: Mapping[str, object] | None,
    card_event_id: str | None = None,
    delivery_id: str | None = None,
) -> RecordedApprovalDecision:
    """Commit the first terminal decision for one background-call card."""
    unacknowledged = card_event_id is None
    selector = (
        "background.delivery_id = ? AND initial.acknowledged_event_id IS NULL"
        if unacknowledged
        else "initial.acknowledged_event_id = ?"
    )
    selector_value = delivery_id if unacknowledged else card_event_id
    transaction.execute(
        f"""
        UPDATE background_approval_calls AS background SET decision = decision
        FROM matrix_delivery_outbox AS initial
        WHERE background.principal_id = ?
          AND initial.principal_id = background.principal_id
          AND initial.delivery_id = background.delivery_id
          AND initial.stage = 'initial' AND {selector}
        """,  # noqa: S608 - selector is chosen from two fixed clauses above
        (principal_id, selector_value),
    )
    row = transaction.fetchone(
        f"""
        SELECT background.delivery_id, background.run_id, background.call_id,
               background.expires_at_ns, background.decision, background.reason,
               initial.event_type, initial.room_id, initial.thread_id, initial.payload_json,
               initial.acknowledged_event_id, initial.membership_epoch,
               final.payload_json AS resolution_json
        FROM background_approval_calls AS background
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = background.principal_id
         AND initial.delivery_id = background.delivery_id
         AND initial.stage = 'initial'
        LEFT JOIN matrix_delivery_outbox AS final
          ON final.principal_id = background.principal_id
         AND final.delivery_id = background.delivery_id
         AND final.stage = 'final'
        WHERE background.principal_id = ? AND {selector}
        """,  # noqa: S608 - selector is chosen from two fixed clauses above
        (principal_id, selector_value),
    )
    if row is None:
        return approval_card_state.RecordedApprovalDecision(resolution=None, recorded=False)
    if row["decision"] is not None:
        return approval_card_state.RecordedApprovalDecision(
            resolution=approval_card_state.decode_resolution(cast("str | None", row["resolution_json"])),
            recorded=False,
        )
    expired = time.time_ns() >= int(row["expires_at_ns"])
    decision_status: Literal["approved", "denied", "expired"]
    decision_reason = reason
    if requested_status == "expired" or expired:
        decision_status = "expired"
        decision_reason = approval_card_state.TIMEOUT_REASON
    else:
        decision_status = requested_status
    stored = approval_card_state.stored_resolution(
        row,
        resolution=resolution,
        requested_status=requested_status,
        decision=decision_status,
        reason=decision_reason,
        description="background approval payload",
    )
    decided = transaction.fetchone(
        """
        UPDATE background_approval_calls SET decision = ?, reason = ?
        WHERE principal_id = ? AND delivery_id = ? AND decision IS NULL
        RETURNING delivery_id
        """,
        (decision_status, decision_reason, principal_id, str(row["delivery_id"])),
    )
    if decided is None:
        msg = f"Background approval call {row['call_id']!r} changed during its exact-call decision"
        raise RuntimeError(msg)
    approval_card_state.enqueue_resolution(transaction, principal_id, row, stored)
    return approval_card_state.RecordedApprovalDecision(resolution=stored, recorded=True)


def resolve_call(
    transaction: Transaction,
    principal_id: str,
    *,
    run_id: str,
    call_id: str,
    requested_status: Literal["denied", "expired"],
    reason: str,
) -> RecordedApprovalDecision:
    """Resolve one trusted exact background target through the shared card path."""
    row = transaction.fetchone(
        """
        SELECT background.delivery_id, initial.acknowledged_event_id
        FROM background_approval_calls AS background
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = background.principal_id
         AND initial.delivery_id = background.delivery_id
         AND initial.stage = 'initial'
        WHERE background.principal_id = ? AND background.run_id = ? AND background.call_id = ?
        """,
        (principal_id, run_id, call_id),
    )
    if row is None:
        return approval_card_state.RecordedApprovalDecision(resolution=None, recorded=False)
    card_event_id = cast("str | None", row["acknowledged_event_id"])
    return resolve(
        transaction,
        principal_id,
        card_event_id=card_event_id,
        delivery_id=None if card_event_id is not None else str(row["delivery_id"]),
        requested_status=requested_status,
        reason=reason,
        resolution=None,
    )


def resolve_pending_calls(
    transaction: Transaction,
    principal_id: str,
    *,
    run_id: str,
    reason: str,
) -> int:
    """Deny every currently pending target for one run in one transaction."""
    rows = transaction.fetchall(
        """
        SELECT call_id FROM background_approval_calls
        WHERE principal_id = ? AND run_id = ? AND decision IS NULL
        ORDER BY call_id
        """,
        (principal_id, run_id),
    )
    recorded = 0
    for row in rows:
        recorded_decision = resolve_call(
            transaction,
            principal_id,
            run_id=run_id,
            call_id=str(row["call_id"]),
            requested_status="denied",
            reason=reason,
        )
        recorded += int(recorded_decision.recorded)
    return recorded


def decision(
    transaction: Transaction,
    principal_id: str,
    *,
    run_id: str,
    call_id: str,
) -> BackgroundApprovalDecision | None:
    """Return the exact call's first terminal decision, if one exists."""
    row = transaction.fetchone(
        """
        SELECT decision, reason FROM background_approval_calls
        WHERE principal_id = ? AND run_id = ? AND call_id = ?
        """,
        (principal_id, run_id, call_id),
    )
    if row is None or row["decision"] is None:
        return None
    return BackgroundApprovalDecision(
        status=cast('Literal["approved", "denied", "expired"]', str(row["decision"])),
        reason=cast("str | None", row["reason"]),
    )


def prune_calls(transaction: Transaction, principal_id: str, *, run_id: str) -> bool:
    """Delete settled background targets only after their shared cards retire."""
    blocked = transaction.fetchone(
        """
        SELECT 1 AS present
        FROM background_approval_calls AS background
        LEFT JOIN approval_cards AS cards
          ON cards.principal_id = background.principal_id
         AND cards.delivery_id = background.delivery_id
        WHERE background.principal_id = ? AND background.run_id = ?
          AND (background.decision IS NULL OR cards.delivery_id IS NOT NULL)
        LIMIT 1
        """,
        (principal_id, run_id),
    )
    if blocked is not None:
        return False
    transaction.execute(
        "DELETE FROM background_approval_calls WHERE principal_id = ? AND run_id = ?",
        (principal_id, run_id),
    )
    return True
