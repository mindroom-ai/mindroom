"""One-shot approvals for tool calls a requester approved while scheduling them.

A scheduled call's card is a detached exact-call card on the shared
background-approval lifecycle, as the single call of the run its task names.
This module owns the binding that stores the approved call: the scheduled task
fires, arms the binding only if the task is unchanged and on time, and the
scheduling agent claims it once by task ID, which spends the approval and
publishes its receipt. The requester approves either the stored arguments or any
arguments for that tool. It is the only module that reads or writes the binding table.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_approval_grants import ANY_ARGUMENTS, EXACT_ARGUMENTS, ScheduledCallBinding, approval_timestamp

from . import approval_card_state, approval_grants, background_approvals, membership_state, outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .approval_card_state import ApprovalCardReservation, ApprovalDecisionMetadata, RecordedApprovalDecision
    from .backend import Row, Transaction

__all__ = [
    "RECEIPT",
    "SCHEDULED_APPROVAL_WINDOW_NS",
    "ScheduledApprovalArmState",
    "ScheduledCall",
    "ScheduledCallBinding",
    "ScheduledCallClaim",
    "ScheduledCallOutcome",
    "ScheduledCallRefusal",
    "arm",
    "card_identity",
    "claim",
    "prune",
    "record_decision",
    "record_outcome",
    "remember_receipt_alias",
    "reserve",
    "scheduled_call_run_id",
    "stored_call",
    "withdraw",
]

SCHEDULED_APPROVAL_WINDOW_NS = 15 * 60 * 1_000_000_000
_RUN_PREFIX = "scheduled-task:"
_RETENTION_NS = 30 * 24 * 60 * 60 * 1_000_000_000
ScheduledApprovalArmState = Literal["none", "armed", "denied", "unarmed"]
ScheduledCallRefusal = Literal[
    "missing",
    "elsewhere",
    "withdrawn",
    "not_approved",
    "not_armed",
    "used",
    "late",
    "arguments",
    "left_room",
]
# Completed means the call returned; a claimed binding with no outcome ended in an unknown state.
ScheduledCallOutcome = Literal["completed", "failed"]
# A consumed binding marks the receipt it published as an automatic terminal approval.
RECEIPT = """EXISTS (
    SELECT 1 FROM scheduled_call_approvals AS scheduled
    WHERE scheduled.principal_id = {initial}.principal_id
      AND scheduled.consumed_delivery_id = {initial}.delivery_id
)"""
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledCall:
    """A stored scheduled call as its scheduling agent reads it before claiming it."""

    task_id: str
    room_id: str
    thread_id: str
    requester_id: str
    agent_name: str
    toolkit_name: str
    tool_name: str
    arguments_json: str
    execute_at_ns: int
    # The scope the requester approved, narrowed to exact arguments when operators stop allowing any.
    approved_scope: str | None


@dataclass(frozen=True, slots=True)
class ScheduledCallClaim:
    """One spent approval: the exact call to run and the provenance its receipt published."""

    toolkit_name: str
    tool_name: str
    arguments_json: str
    provenance: dict[str, object]


def scheduled_call_run_id(task_id: str) -> str:
    """Return the detached run that owns one scheduled task's single approval call."""
    return _RUN_PREFIX + task_id


def card_identity(card: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the detached call a scheduling-time card names, or None for another kind of card."""
    content = card.get("content")
    if not isinstance(content, dict) or content.get("approval_target") != "scheduled_call":
        return None
    task_id = content.get("scheduled_task_id")
    if not isinstance(task_id, str) or not task_id:
        msg = "Approval card is missing scheduled-call target identity."
        raise ValueError(msg)
    return scheduled_call_run_id(task_id), task_id


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
            run_id=scheduled_call_run_id(task_id),
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
    """Reserve the scheduling-time card and its binding in one commit; a task ID already in use reserves nothing."""
    if transaction.fetchone(
        "SELECT 1 AS present FROM scheduled_call_approvals WHERE principal_id = ? AND task_id = ?",
        (principal_id, binding.task_id),
    ):
        return False
    run_id = scheduled_call_run_id(binding.task_id)
    if card_identity({"content": card.payload}) != (run_id, binding.task_id):
        msg = f"Scheduled approval delivery {card.delivery_id!r} changed exact-call identity"
        raise ValueError(msg)
    if not background_approvals.reserve_delivery(
        transaction,
        principal_id,
        room_id=binding.room_id,
        thread_id=binding.thread_id,
        run_id=run_id,
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
            principal_id, task_id, delivery_id, room_id, thread_id, requester_id, agent_name,
            toolkit_name, tool_name, arguments_json, workflow_digest, execute_at_ns, membership_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, task_id) DO NOTHING
        """,
        (
            principal_id,
            binding.task_id,
            card.delivery_id,
            binding.room_id,
            binding.thread_id,
            binding.requester_id,
            binding.agent_name,
            binding.toolkit_name,
            binding.tool_name,
            binding.arguments_json,
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
    # Serialize with a claim, which runs under this lock, then lock the card's row
    # before the binding, in the same order as a card decision.
    approval_grants.lock(transaction, principal_id)
    recorded = background_approvals.resolve_call(
        transaction,
        principal_id,
        run_id=scheduled_call_run_id(task_id),
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


def stored_call(transaction: Transaction, principal_id: str, *, task_id: str) -> ScheduledCall | None:
    """Read the call a scheduled task stored, so its agent can prepare it before claiming."""
    row = transaction.fetchone(
        "SELECT * FROM scheduled_call_approvals WHERE principal_id = ? AND task_id = ?",
        (principal_id, task_id),
    )
    if row is None:
        return None
    return ScheduledCall(
        task_id=task_id,
        room_id=str(row["room_id"]),
        thread_id=str(row["thread_id"]),
        requester_id=str(row["requester_id"]),
        agent_name=str(row["agent_name"]),
        toolkit_name=str(row["toolkit_name"]),
        tool_name=str(row["tool_name"]),
        arguments_json=str(row["arguments_json"]),
        execute_at_ns=int(row["execute_at_ns"]),
        approved_scope=None if row["approved_scope"] is None else str(row["approved_scope"]),
    )


def claim(
    transaction: Transaction,
    principal_id: str,
    *,
    call: ScheduledCall,
    arguments_json: str,
    receipt: ApprovalCardReservation,
    now_ns: int,
) -> ScheduledCallClaim | ScheduledCallRefusal:
    """Spend an armed approval once and reserve its receipt in the same commit.

    ``call`` names who claims it: the binding's room, thread, requester, agent,
    and stored call must still match. ``arguments_json`` is the canonical
    arguments that will run, the stored ones unless any arguments were approved.
    """
    # Serialize with withdrawal, then lock the card's row before the binding, as a card decision does.
    approval_grants.lock(transaction, principal_id)
    row = transaction.fetchone(
        """
        SELECT scheduled.*, background.decision
        FROM scheduled_call_approvals AS scheduled
        JOIN background_approval_calls AS background
          ON background.principal_id = scheduled.principal_id AND background.delivery_id = scheduled.delivery_id
        WHERE scheduled.principal_id = ? AND scheduled.task_id = ?
        """,
        (principal_id, call.task_id),
    )
    if row is None:
        return "missing"
    refusal = _refusal(row, call, arguments_json, now_ns)
    if refusal is not None:
        return refusal
    if receipt.tool_call_id != call.task_id or receipt.payload.get("tool_name") != call.tool_name:
        msg = f"Scheduled call receipt {receipt.delivery_id!r} names another call"
        raise ValueError(msg)
    if not membership_state.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=call.room_id,
        expected_membership_epoch=int(row["membership_epoch"]),
    ):
        return "left_room"
    approved_at = None if row["decided_at_ns"] is None else approval_timestamp(int(row["decided_at_ns"]))
    # An approved card always records its approver, which may be the canonical account behind an aliased requester.
    approved_by = str(row["decided_by"])
    provenance: dict[str, object] = {
        "kind": "scheduled_approval",
        "task_id": call.task_id,
        "approval_card_event_id": row["card_event_id"],
        "approved_by": approved_by,
        "approved_at": approved_at,
        "scheduled_for": approval_timestamp(int(row["execute_at_ns"])),
        "scope": str(row["approved_scope"]),
        # Canonical JSON, so this equals the approval digest of the same arguments.
        "arguments_digest": hashlib.sha256(arguments_json.encode()).hexdigest(),
    }
    transaction.execute(
        """
        UPDATE scheduled_call_approvals SET consumed_at_ns = ?, consumed_delivery_id = ?
        WHERE principal_id = ? AND task_id = ?
        """,
        (now_ns, receipt.delivery_id, principal_id, call.task_id),
    )
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=receipt.delivery_id,
        stage=DeliveryStage.INITIAL,
        event_type=receipt.event_type,
        room_id=call.room_id,
        thread_id=call.thread_id,
        membership_epoch=int(row["membership_epoch"]),
        payload=approval_card_state.terminal_content(
            receipt.payload,
            status="approved",
            reason=None,
            metadata=approval_card_state.ApprovalDecisionMetadata(
                resolved_by=approved_by,
                resolved_at=approved_at,
                provenance=provenance,
            ),
            publication="receipt",
        ),
        edits_event_id=None,
    )
    logger.info("scheduled_tool_call_approval_consumed", tool_name=call.tool_name, **provenance)
    return ScheduledCallClaim(
        toolkit_name=call.toolkit_name,
        tool_name=call.tool_name,
        arguments_json=arguments_json,
        provenance=provenance,
    )


def record_outcome(transaction: Transaction, principal_id: str, *, task_id: str, outcome: ScheduledCallOutcome) -> None:
    """Record how a claimed call ended; a claim never returns to unspent."""
    transaction.execute(
        """
        UPDATE scheduled_call_approvals SET outcome = ?
        WHERE principal_id = ? AND task_id = ? AND consumed_at_ns IS NOT NULL AND outcome IS NULL
        """,
        (outcome, principal_id, task_id),
    )


def _refusal(  # noqa: PLR0911 - one refusal per broken condition
    row: Row,
    call: ScheduledCall,
    arguments_json: str,
    now_ns: int,
) -> ScheduledCallRefusal | None:
    claimant = (call.room_id, call.thread_id, call.requester_id, call.agent_name, call.toolkit_name, call.tool_name)
    stored = tuple(
        str(row[column])
        for column in ("room_id", "thread_id", "requester_id", "agent_name", "toolkit_name", "tool_name")
    )
    if stored != claimant:
        return "elsewhere"
    if row["revoked_at_ns"] is not None:
        return "withdrawn"
    if row["decision"] != "approved":
        return "not_approved"
    if row["consumed_at_ns"] is not None:
        return "used"
    if row["armed_at_ns"] is None:
        return "not_armed"
    if abs(now_ns - int(row["execute_at_ns"])) > SCHEDULED_APPROVAL_WINDOW_NS:
        return "late"
    if arguments_json != str(row["arguments_json"]) and row["approved_scope"] != ANY_ARGUMENTS:
        return "arguments"
    return None
