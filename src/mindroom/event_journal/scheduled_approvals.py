"""One-shot approvals for tool calls a requester approved while scheduling them.

A scheduled call is the single detached call of the run its task names: its
decision is the call's, and its card and receipt are the call's cards. This module
owns the binding that stores the call and its one-shot lifecycle: the scheduled
task fires, arms the binding only if the task is unchanged and on time, and the
scheduling agent claims it once by task ID, which spends the approval and
publishes its receipt. The requester approves either the stored arguments or any
arguments for that tool. It is the only module that reads or writes the binding table.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_approval_grants import ANY_ARGUMENTS, EXACT_ARGUMENTS, ScheduledCallBinding, approval_timestamp

from . import approval_calls, approval_cards, approval_grants, membership_state
from .approval_calls import CallKey

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .approval_cards import ApprovalCardReservation, RecordedApprovalDecision
    from .backend import Row, Transaction

__all__ = [
    "SCHEDULED_APPROVAL_WINDOW_NS",
    "ScheduledApprovalArmState",
    "ScheduledCall",
    "ScheduledCallBinding",
    "ScheduledCallClaim",
    "ScheduledCallOutcome",
    "ScheduledCallRefusal",
    "arm",
    "claim",
    "prune",
    "record_outcome",
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
# Completed means the call ran and returned; failed means it raised or the tool-call policy declined it before it ran.
# A claimed binding with no outcome ended in an unknown state.
ScheduledCallOutcome = Literal["completed", "failed"]
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


def _call(task_id: str) -> CallKey:
    """Name the single detached call of one scheduled task."""
    return CallKey.detached(scheduled_call_run_id(task_id), task_id)


def _binding(transaction: Transaction, principal_id: str, task_id: str) -> Row | None:
    """Return one binding with its call's decision and the event of its request card."""
    return transaction.fetchone(
        """
        SELECT scheduled.*, calls.decision, calls.resolved_by, calls.resolved_at_ns, calls.approved_scope,
               (SELECT MIN(events.card_event_id)
                FROM approval_cards AS cards
                JOIN approval_card_events AS events
                  ON events.principal_id = cards.principal_id AND events.delivery_id = cards.delivery_id
                WHERE cards.principal_id = calls.principal_id AND cards.owner_id = calls.owner_id
                  AND cards.generation = calls.generation AND cards.tool_call_id = calls.tool_call_id
                  AND cards.kind = 'request'
               ) AS card_event_id
        FROM scheduled_call_approvals AS scheduled
        JOIN approval_calls AS calls
          ON calls.principal_id = scheduled.principal_id
         AND calls.owner_id = ? AND calls.generation = 0 AND calls.tool_call_id = scheduled.task_id
        WHERE scheduled.principal_id = ? AND scheduled.task_id = ?
        """,
        (scheduled_call_run_id(task_id), principal_id, task_id),
    )


def prune(transaction: Transaction, principal_id: str, now_ns: int) -> None:
    """Forget bindings past their send time or withdrawal once no card of their call is live.

    The call's retired cards stay, so a click on one is still recognized as terminal.
    """
    cutoff_ns = now_ns - _RETENTION_NS
    rows = transaction.fetchall(
        """
        SELECT task_id FROM scheduled_call_approvals
        WHERE principal_id = ? AND (execute_at_ns < ? OR revoked_at_ns < ?)
        """,
        (principal_id, cutoff_ns, cutoff_ns),
    )
    for row in rows:
        task_id = str(row["task_id"])
        if not approval_calls.prune(transaction, principal_id, owner_id=scheduled_call_run_id(task_id)):
            continue
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
    any_arguments_offered: bool,
) -> bool:
    """Reserve the scheduling-time card and its binding in one commit; a task ID already in use reserves nothing."""
    if transaction.fetchone(
        "SELECT 1 AS present FROM scheduled_call_approvals WHERE principal_id = ? AND task_id = ?",
        (principal_id, binding.task_id),
    ):
        return False
    if _card_task_id(card.payload) != binding.task_id:
        msg = f"Scheduled approval delivery {card.delivery_id!r} changed exact-call identity"
        raise ValueError(msg)
    if not approval_cards.reserve_detached_card(
        transaction,
        principal_id,
        call=_call(binding.task_id),
        owner_kind="scheduled",
        tool_name=binding.tool_name,
        toolkit_name=binding.toolkit_name,
        invoking_agent=binding.agent_name,
        room_id=binding.room_id,
        thread_id=binding.thread_id,
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
            principal_id, task_id, room_id, thread_id, requester_id, agent_name, toolkit_name, tool_name,
            arguments_json, workflow_digest, execute_at_ns, membership_epoch, any_arguments_offered
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, task_id) DO NOTHING
        """,
        (
            principal_id,
            binding.task_id,
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
            int(any_arguments_offered),
        ),
    )
    return True


def _card_task_id(payload: Mapping[str, object]) -> object:
    """Return the scheduled task a scheduling-time card's payload names, which its reservation checks."""
    return payload.get("scheduled_task_id") if payload.get("approval_target") == "scheduled_call" else None


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
    row = _binding(transaction, principal_id, task_id)
    if row is None:
        return "none"
    if row["revoked_at_ns"] is not None or str(row["workflow_digest"]) != workflow_digest:
        return "unarmed"
    if row["decision"] == "denied" and row["resolved_by"] is not None:
        return "denied"
    if row["decision"] != "approved" or abs(now_ns - int(row["execute_at_ns"])) > SCHEDULED_APPROVAL_WINDOW_NS:
        return "unarmed"
    transaction.execute(
        """
        UPDATE scheduled_call_approvals SET armed_at_ns = ?, armed_scope = ?
        WHERE principal_id = ? AND task_id = ? AND consumed_at_ns IS NULL
        """,
        (
            now_ns,
            str(row["approved_scope"]) if any_arguments_allowed else EXACT_ARGUMENTS,
            principal_id,
            task_id,
        ),
    )
    return "armed"


def withdraw(transaction: Transaction, principal_id: str, *, task_id: str, reason: str) -> RecordedApprovalDecision:
    """Withdraw a cancelled task's approval for good and deny its card if still pending."""
    # Serialize with a claim, which runs under this lock, then lock the call's row
    # before the binding, in the same order as a card decision.
    approval_grants.lock(transaction, principal_id)
    recorded = approval_calls.resolve_call(
        transaction,
        _call(task_id),
        requested="denied",
        reason=reason,
        metadata=None,
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
    row = _binding(transaction, principal_id, task_id)
    if row is None:
        return None
    approved_scope = row["approved_scope"] if row["armed_scope"] is None else row["armed_scope"]
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
        approved_scope=None if approved_scope is None else str(approved_scope),
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
    # Serialize with withdrawal, then read the call before the binding, as a card decision does.
    approval_grants.lock(transaction, principal_id)
    row = _binding(transaction, principal_id, call.task_id)
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
    approved_at = None if row["resolved_at_ns"] is None else approval_timestamp(int(row["resolved_at_ns"]))
    # An approved card always records its approver, which may be the canonical account behind an aliased requester.
    approved_by = str(row["resolved_by"])
    provenance: dict[str, object] = {
        "kind": "scheduled_approval",
        "task_id": call.task_id,
        "approval_card_event_id": row["card_event_id"],
        "approved_by": approved_by,
        "approved_at": approved_at,
        "scheduled_for": approval_timestamp(int(row["execute_at_ns"])),
        "scope": str(row["armed_scope"]),
        # Canonical JSON, so this equals the approval digest of the same arguments.
        "arguments_digest": hashlib.sha256(arguments_json.encode()).hexdigest(),
    }
    transaction.execute(
        "UPDATE scheduled_call_approvals SET consumed_at_ns = ? WHERE principal_id = ? AND task_id = ?",
        (now_ns, principal_id, call.task_id),
    )
    approval_cards.reserve_receipt(
        transaction,
        principal_id,
        kind="scheduled_receipt",
        call=_call(call.task_id),
        delivery_id=receipt.delivery_id,
        event_type=receipt.event_type,
        room_id=call.room_id,
        thread_id=call.thread_id,
        membership_epoch=int(row["membership_epoch"]),
        payload=approval_cards.terminal_content(
            receipt.payload,
            status="approved",
            reason=None,
            metadata=approval_cards.ApprovalDecisionMetadata(
                resolved_by=approved_by,
                resolved_at=approved_at,
                provenance=provenance,
            ),
            publication="receipt",
        ),
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
    if arguments_json != str(row["arguments_json"]) and row["armed_scope"] != ANY_ARGUMENTS:
        return "arguments"
    return None
