"""One-shot approvals for tool calls a requester approved while scheduling them.

A scheduled call's card is a detached exact-call card on the shared
background-approval lifecycle. This module owns the binding a later call must
match: the scheduled task fires, arms the binding only if the task is unchanged
and on time, and the first matching call consumes it. The requester approves
either the exact arguments shown on the card or any arguments for that tool.
It is the only module that reads or writes the binding table.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_approval_grants import ANY_ARGUMENTS, EXACT_ARGUMENTS, approval_timestamp

from . import approval_card_state, approval_grants, background_approvals, outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from .approval_card_state import ApprovalCardReservation, ApprovalDecisionMetadata, RecordedApprovalDecision
    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction

__all__ = [
    "RECEIPT",
    "SCHEDULED_APPROVAL_WINDOW_NS",
    "ScheduledApprovalArmState",
    "ScheduledCallBinding",
    "apply_armed",
    "arm",
    "prune",
    "record_decision",
    "remember_receipt_alias",
    "reserve",
    "withdraw",
]

SCHEDULED_APPROVAL_WINDOW_NS = 15 * 60 * 1_000_000_000
_RETENTION_NS = 30 * 24 * 60 * 60 * 1_000_000_000
ScheduledApprovalArmState = Literal["none", "armed", "denied", "unarmed"]
# A consumed binding marks the receipt it published as an automatic terminal approval.
RECEIPT = """EXISTS (
    SELECT 1 FROM scheduled_call_approvals AS scheduled
    WHERE scheduled.principal_id = {initial}.principal_id
      AND scheduled.consumed_delivery_id = {initial}.delivery_id
)"""
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledCallBinding:
    """The call, scope, task, and time one scheduled approval covers."""

    task_id: str
    room_id: str
    thread_id: str
    requester_id: str
    # The responding agent or team; a team's call is made by one of its members.
    entity_name: str
    tool_name: str
    arguments_digest: str
    workflow_digest: str
    execute_at_ns: int


def prune(transaction: Transaction, principal_id: str, now_ns: int) -> None:
    """Forget bindings past their send time or withdrawal once their card has retired and any receipt is settled.

    A receipt Matrix never accepted, because it was retired or failed for good, has no
    event a click could target, so it is dropped with the binding that explains it.
    """
    cutoff_ns = now_ns - _RETENTION_NS
    rows = transaction.fetchall(
        """
        SELECT task_id, consumed_delivery_id FROM scheduled_call_approvals AS scheduled
        WHERE principal_id = ? AND (execute_at_ns < ? OR revoked_at_ns < ?)
          AND NOT EXISTS (
              SELECT 1 FROM matrix_delivery_outbox AS receipt
              WHERE receipt.principal_id = scheduled.principal_id
                AND receipt.delivery_id = scheduled.consumed_delivery_id
                AND receipt.retired = 0 AND receipt.permanent_failure_reason IS NULL
          )
        """,
        (principal_id, cutoff_ns, cutoff_ns),
    )
    for row in rows:
        task_id = str(row["task_id"])
        if not background_approvals.prune_calls(
            transaction,
            principal_id,
            run_id=background_approvals.scheduled_call_run_id(task_id),
        ):
            continue
        if row["consumed_delivery_id"] is not None:
            transaction.execute(
                "DELETE FROM matrix_delivery_outbox WHERE principal_id = ? AND delivery_id = ?",
                (principal_id, str(row["consumed_delivery_id"])),
            )
        transaction.execute(
            "DELETE FROM scheduled_call_approvals WHERE principal_id = ? AND task_id = ?",
            (principal_id, task_id),
        )


def reserve(
    transaction: Transaction,
    principal_id: str,
    *,
    binding: ScheduledCallBinding,
    card: ApprovalCardReservation,
) -> bool:
    """Reserve the scheduling-time card and its binding in one commit."""
    if not background_approvals.reserve_delivery(
        transaction,
        principal_id,
        room_id=binding.room_id,
        thread_id=binding.thread_id,
        run_id=background_approvals.scheduled_call_run_id(binding.task_id),
        call_id=binding.task_id,
        expires_at_ns=binding.execute_at_ns,
        card=card,
    ):
        return False
    epoch = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, binding.room_id),
    )
    transaction.execute(
        """
        INSERT INTO scheduled_call_approvals (
            principal_id, task_id, delivery_id, room_id, thread_id, requester_id, entity_name,
            tool_name, arguments_digest, workflow_digest, execute_at_ns, membership_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, task_id) DO NOTHING
        """,
        (
            principal_id,
            binding.task_id,
            card.delivery_id,
            binding.room_id,
            binding.thread_id,
            binding.requester_id,
            binding.entity_name,
            binding.tool_name,
            binding.arguments_digest,
            binding.workflow_digest,
            binding.execute_at_ns,
            0 if epoch is None else int(epoch["membership_epoch"]),
        ),
    )
    return True


def record_decision(
    transaction: Transaction,
    principal_id: str,
    *,
    recorded: RecordedApprovalDecision,
    requested_status: str,
    metadata: ApprovalDecisionMetadata | None,
) -> None:
    """Record who decided a scheduling-time card, when, and the scope they approved.

    Only the requester's own decision names a decider; denials MindRoom makes by
    itself leave it empty, so they never skip the send.
    """
    if not recorded.recorded or recorded.delivery_id is None or recorded.resolution is None:
        return
    status = recorded.resolution.get("status")
    decided_by = metadata.resolved_by if metadata is not None and status == requested_status else None
    approved_scope = None
    if decided_by is not None and status == "approved":
        assert metadata is not None
        approved_scope = metadata.scheduled_scope or EXACT_ARGUMENTS
    transaction.execute(
        """
        UPDATE scheduled_call_approvals SET card_event_id = ?, decided_at_ns = ?, decided_by = ?, approved_scope = ?
        WHERE principal_id = ? AND delivery_id = ?
        """,
        (recorded.card_event_id, time.time_ns(), decided_by, approved_scope, principal_id, recorded.delivery_id),
    )


def remember_receipt_alias(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    delivery_id: str,
) -> None:
    """Remember another copy of a consumed binding's receipt as terminal."""
    transaction.execute(
        """
        INSERT INTO approval_action_tombstones (principal_id, room_id, card_event_id)
        SELECT scheduled.principal_id, scheduled.room_id, ? FROM scheduled_call_approvals AS scheduled
        WHERE scheduled.principal_id = ? AND scheduled.room_id = ? AND scheduled.consumed_delivery_id = ?
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        """,
        (card_event_id, principal_id, room_id, delivery_id),
    )


def arm(
    transaction: Transaction,
    principal_id: str,
    *,
    task_id: str,
    workflow_digest: str,
    any_arguments_allowed: bool,
    now_ns: int,
) -> ScheduledApprovalArmState:
    """Arm an approved binding for the unchanged task firing on time; only the requester's denial skips it.

    When operators no longer allow approving any arguments, an approval given for
    any arguments arms for the exact call only.
    """
    row = transaction.fetchone(
        """
        SELECT scheduled.workflow_digest, scheduled.execute_at_ns, scheduled.revoked_at_ns, scheduled.decided_by,
               background.decision
        FROM scheduled_call_approvals AS scheduled
        JOIN background_approval_calls AS background
          ON background.principal_id = scheduled.principal_id AND background.delivery_id = scheduled.delivery_id
        WHERE scheduled.principal_id = ? AND scheduled.task_id = ?
        """,
        (principal_id, task_id),
    )
    if row is None:
        return "none"
    if row["revoked_at_ns"] is not None or str(row["workflow_digest"]) != workflow_digest:
        return "unarmed"
    if row["decision"] == "denied" and row["decided_by"] is not None:
        return "denied"
    if row["decision"] != "approved" or abs(now_ns - int(row["execute_at_ns"])) > SCHEDULED_APPROVAL_WINDOW_NS:
        return "unarmed"
    transaction.execute(
        """
        UPDATE scheduled_call_approvals
        SET armed_at_ns = ?, approved_scope = CASE WHEN ? THEN approved_scope ELSE ? END
        WHERE principal_id = ? AND task_id = ? AND consumed_at_ns IS NULL
        """,
        (now_ns, any_arguments_allowed, EXACT_ARGUMENTS, principal_id, task_id),
    )
    return "armed"


def withdraw(transaction: Transaction, principal_id: str, *, task_id: str, reason: str) -> RecordedApprovalDecision:
    """Withdraw a cancelled or edited task's approval for good and deny its card if still pending."""
    # Serialize with consumption, which runs under this lock during card reservation,
    # then lock the card's row before the binding, in the same order as a card decision.
    approval_grants.lock(transaction, principal_id)
    recorded = background_approvals.resolve_call(
        transaction,
        principal_id,
        run_id=background_approvals.scheduled_call_run_id(task_id),
        call_id=task_id,
        requested_status="denied",
        reason=reason,
    )
    transaction.execute(
        """
        UPDATE scheduled_call_approvals SET revoked_at_ns = ?
        WHERE principal_id = ? AND task_id = ? AND revoked_at_ns IS NULL
        """,
        (time.time_ns(), principal_id, task_id),
    )
    return recorded


def apply_armed(
    transaction: Transaction,
    principal_id: str,
    *,
    continuation_principal_id: str,
    continuation: ApprovalContinuation,
    card: ApprovalCardReservation,
    membership_epoch: int,
) -> bool:
    """Approve one call matching an armed binding and publish its receipt instead of a card."""
    call = next(call for call in continuation.calls if call.tool_call_id == card.tool_call_id)
    if call.arguments_digest is None or continuation.thread_id is None:
        return False
    now = time.time_ns()
    row = transaction.fetchone(
        """
        SELECT scheduled.task_id, scheduled.execute_at_ns, scheduled.decided_at_ns, scheduled.decided_by,
               scheduled.card_event_id, scheduled.approved_scope
        FROM scheduled_call_approvals AS scheduled
        JOIN background_approval_calls AS background
          ON background.principal_id = scheduled.principal_id AND background.delivery_id = scheduled.delivery_id
        WHERE scheduled.principal_id = ? AND scheduled.room_id = ? AND scheduled.thread_id = ?
          AND scheduled.requester_id = ? AND scheduled.entity_name = ? AND scheduled.tool_name = ?
          AND (scheduled.arguments_digest = ? OR scheduled.approved_scope = ?)
          AND scheduled.membership_epoch = ?
          AND scheduled.armed_at_ns IS NOT NULL AND scheduled.revoked_at_ns IS NULL
          AND scheduled.consumed_at_ns IS NULL
          AND scheduled.execute_at_ns BETWEEN ? AND ?
          AND background.decision = 'approved'
        ORDER BY CASE WHEN scheduled.arguments_digest = ? THEN 0 ELSE 1 END, scheduled.execute_at_ns, scheduled.task_id
        LIMIT 1
        """,
        (
            principal_id,
            continuation.room_id,
            continuation.thread_id,
            continuation.requester_id,
            continuation.entity_name,
            call.tool_name,
            call.arguments_digest,
            ANY_ARGUMENTS,
            membership_epoch,
            now - SCHEDULED_APPROVAL_WINDOW_NS,
            now + SCHEDULED_APPROVAL_WINDOW_NS,
            call.arguments_digest,
        ),
    )
    if row is None:
        return False
    decided = transaction.fetchone(
        """
        UPDATE approval_continuation_calls SET decision = 'approved'
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND tool_call_id = ?
          AND decision IS NULL AND expires_at_ns > ?
        RETURNING tool_call_id
        """,
        (continuation_principal_id, continuation.approval_id, continuation.generation, call.tool_call_id, now),
    )
    if decided is None:
        return False
    task_id = str(row["task_id"])
    transaction.execute(
        """
        UPDATE scheduled_call_approvals SET consumed_at_ns = ?, consumed_delivery_id = ?
        WHERE principal_id = ? AND task_id = ?
        """,
        (now, card.delivery_id, principal_id, task_id),
    )
    approved_at = None if row["decided_at_ns"] is None else approval_timestamp(int(row["decided_at_ns"]))
    # An approved card always records its approver, which may be the canonical account behind an aliased requester.
    approved_by = str(row["decided_by"])
    provenance = {
        "kind": "scheduled_approval",
        "task_id": task_id,
        "approval_card_event_id": row["card_event_id"],
        "approved_by": approved_by,
        "approved_at": approved_at,
        "scheduled_for": approval_timestamp(int(row["execute_at_ns"])),
        "scope": str(row["approved_scope"]),
        "arguments_digest": call.arguments_digest,
    }
    # Timed-grant scope is bound only when a card is reserved, so this receipt
    # must not carry the unbound placeholder its prepared payload still holds.
    receipt = approval_card_state.terminal_content(
        {key: value for key, value in card.payload.items() if key != "approval_scope"},
        status="approved",
        reason=None,
        metadata=approval_card_state.ApprovalDecisionMetadata(
            resolved_by=approved_by,
            resolved_at=approved_at,
            provenance=provenance,
        ),
        publication="receipt",
    )
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=card.delivery_id,
        stage=DeliveryStage.INITIAL,
        event_type=card.event_type,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        membership_epoch=membership_epoch,
        payload=receipt,
        edits_event_id=None,
    )
    logger.info("scheduled_tool_call_approval_consumed", tool_name=call.tool_name, **provenance)
    return True
