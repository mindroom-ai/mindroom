"""One row per gated tool call, whatever owns it, and the one writer of its decision.

A paused reply owns the calls of each of its generations; a detached run (a
background job, a background script, or a scheduled task) owns its calls at
generation 0. A policy decision is stored with the call when a paused generation
is inserted; every later decision goes through ``resolve_call``: card clicks,
timed grants, deadlines, failure settlement, detached settlement, scheduled
withdrawal, and both departure paths, so a card's terminal edit and its call's
decision cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

from mindroom.tool_approval_grants import EXACT_ARGUMENTS

from . import approval_cards, approval_continuations, approval_signals

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .approval_cards import ApprovalDecisionMetadata, RecordedApprovalDecision
    from .backend import Row, Transaction

type OwnerKind = Literal["reply", "job", "script", "scheduled"]
type _DecisionStatus = Literal["approved", "denied", "expired"]

# What a job's pending cards say once the job ends without a decision, whether it was stopped or a restart ended it.
JOB_ENDED_REASON = "The background job asking for this approval ended before a decision."
JOB_RUN_PREFIX = "tool-job:"


class ApprovalDecision(StrEnum):
    """One terminal decision for an exact gated tool call."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CallKey:
    """The identity of one gated call: its owner, generation, and tool call."""

    owner_id: str
    generation: int
    tool_call_id: str

    @classmethod
    def detached(cls, owner_id: str, tool_call_id: str) -> CallKey:
        """Name one call of a detached run, which owns its calls at generation 0."""
        return cls(owner_id, 0, tool_call_id)

    @classmethod
    def of_row(cls, row: Row) -> CallKey:
        """Name the call a row's ``owner_id``, ``generation``, and ``tool_call_id`` columns hold."""
        return cls(str(row["owner_id"]), int(row["generation"]), str(row["tool_call_id"]))


@dataclass(frozen=True, slots=True)
class ApprovalCall:
    """One exact tool call of one paused generation."""

    tool_call_id: str
    tool_name: str
    invoking_agent: str
    expires_at_ns: int
    decision: ApprovalDecision | None = None
    reason: str | None = None
    human_approval_required: bool | None = None
    toolkit_name: str | None = None
    arguments_digest: str | None = None

    def binds_arguments(self, tool_args: Mapping[str, object] | None) -> bool:
        """Return whether these are exactly the arguments this call paused with."""
        return self.arguments_digest is not None and self.arguments_digest == approval_arguments_digest(tool_args)


@dataclass(frozen=True, slots=True)
class BackgroundApprovalDecision:
    """One durable terminal decision for an exact detached call."""

    status: _DecisionStatus
    reason: str | None


def tool_job_run_id(job_id: str) -> str:
    """Return the detached run that owns the calls one background job asks about."""
    return JOB_RUN_PREFIX + job_id


def approval_arguments_digest(tool_args: Mapping[str, object] | None) -> str:
    """Return the canonical digest that binds one approval to its call's exact arguments."""
    encoded = json.dumps(dict(tool_args or {}), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def insert_reply_generation(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    generation: int,
    calls: tuple[ApprovalCall, ...],
) -> None:
    """Insert one paused generation's ordered calls."""
    for ordinal, call in enumerate(calls):
        transaction.execute(
            """
            INSERT INTO approval_calls (
                owner_id, generation, tool_call_id, principal_id, owner_kind, call_ordinal,
                tool_name, toolkit_name, invoking_agent, arguments_digest, human_approval_required,
                expires_at_ns, decision, reason
            ) VALUES (?, ?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                generation,
                call.tool_call_id,
                principal_id,
                ordinal,
                call.tool_name,
                call.toolkit_name,
                call.invoking_agent,
                call.arguments_digest,
                call.human_approval_required,
                call.expires_at_ns,
                None if call.decision is None else call.decision.value,
                call.reason,
            ),
        )


def reply_call_rows(transaction: Transaction, approval_ids: tuple[str, ...]) -> tuple[Row, ...]:
    """Return the calls of each paused reply's generations up to its current one, in pause order."""
    if not approval_ids:
        return ()
    placeholders = ", ".join("?" for _approval_id in approval_ids)
    return transaction.fetchall(
        f"""
        SELECT calls.owner_id, calls.generation, calls.tool_call_id, calls.tool_name, calls.invoking_agent,
               calls.expires_at_ns, calls.decision, calls.reason, calls.human_approval_required,
               calls.toolkit_name, calls.arguments_digest
        FROM approval_calls AS calls
        JOIN approval_continuations AS continuations
          ON continuations.approval_id = calls.owner_id
         AND calls.generation <= continuations.generation
        WHERE calls.owner_kind = 'reply' AND calls.owner_id IN ({placeholders})
        ORDER BY calls.owner_id/*bytes*/, calls.generation, calls.call_ordinal
        """,  # noqa: S608 - placeholders are fixed markers; values remain bound parameters
        approval_ids,
    )


def reply_call(row: Row) -> ApprovalCall:
    """Decode one stored paused-reply call."""
    return ApprovalCall(
        tool_call_id=str(row["tool_call_id"]),
        tool_name=str(row["tool_name"]),
        invoking_agent=str(row["invoking_agent"]),
        toolkit_name=cast("str | None", row["toolkit_name"]),
        arguments_digest=cast("str | None", row["arguments_digest"]),
        expires_at_ns=int(row["expires_at_ns"]),
        decision=None if row["decision"] is None else ApprovalDecision(str(row["decision"])),
        reason=cast("str | None", row["reason"]),
        human_approval_required=(
            None if row["human_approval_required"] is None else bool(row["human_approval_required"])
        ),
    )


def undecided_tool_call_ids(transaction: Transaction, *, approval_id: str, generation: int) -> tuple[str, ...]:
    """Return one paused generation's undecided calls in pause order."""
    rows = transaction.fetchall(
        """
        SELECT tool_call_id FROM approval_calls
        WHERE owner_id = ? AND generation = ? AND decision IS NULL
        ORDER BY call_ordinal
        """,
        (approval_id, generation),
    )
    return tuple(str(row["tool_call_id"]) for row in rows)


def set_approver(transaction: Transaction, call: CallKey, approver_user_id: str) -> None:
    """Name who may decide a paused call, as the request card reserved for it does."""
    transaction.execute(
        "UPDATE approval_calls SET approver_user_id = ? WHERE owner_id = ? AND generation = ? AND tool_call_id = ?",
        (approver_user_id, call.owner_id, call.generation, call.tool_call_id),
    )


def delete_reply_calls(transaction: Transaction, *, approval_id: str) -> None:
    """Delete a gone paused reply's calls that no live card shows; the rest go when their last card retires."""
    transaction.execute(
        f"""
        DELETE FROM approval_calls
        WHERE owner_kind = 'reply' AND owner_id = ?
          AND NOT {LIVE_CARD.format(calls="approval_calls")}
        """,  # noqa: S608 - a fixed predicate, not input
        (approval_id,),
    )


# A call some card that has not retired still shows, for the ``approval_calls`` row ``{calls}`` names.
LIVE_CARD = """EXISTS (
    SELECT 1 FROM approval_cards AS live
    WHERE live.owner_id = {calls}.owner_id AND live.generation = {calls}.generation
      AND live.tool_call_id = {calls}.tool_call_id AND live.retired = 0
)"""


def insert_detached(
    transaction: Transaction,
    principal_id: str,
    *,
    call: CallKey,
    owner_kind: OwnerKind,
    tool_name: str,
    invoking_agent: str,
    toolkit_name: str | None,
    approver_user_id: str,
    expires_at_ns: int,
) -> bool:
    """Insert one detached call after the run's earlier calls, returning whether it is new."""
    inserted = transaction.fetchone(
        """
        INSERT INTO approval_calls (
            owner_id, generation, tool_call_id, principal_id, owner_kind, call_ordinal,
            tool_name, toolkit_name, invoking_agent, approver_user_id, expires_at_ns
        )
        SELECT ?, ?, ?, ?, ?, COALESCE(MAX(call_ordinal) + 1, 0), ?, ?, ?, ?, ?
        FROM approval_calls WHERE owner_id = ? AND generation = ?
        ON CONFLICT (owner_id, generation, tool_call_id) DO NOTHING
        RETURNING tool_call_id
        """,
        (
            call.owner_id,
            call.generation,
            call.tool_call_id,
            principal_id,
            owner_kind,
            tool_name,
            toolkit_name,
            invoking_agent,
            approver_user_id,
            expires_at_ns,
            call.owner_id,
            call.generation,
        ),
    )
    return inserted is not None


def detached_decision(transaction: Transaction, principal_id: str, call: CallKey) -> BackgroundApprovalDecision | None:
    """Return one detached call's decision, if it has one."""
    row = transaction.fetchone(
        """
        SELECT decision, reason FROM approval_calls
        WHERE principal_id = ? AND owner_id = ? AND generation = ? AND tool_call_id = ?
        """,
        (principal_id, call.owner_id, call.generation, call.tool_call_id),
    )
    if row is None or row["decision"] is None:
        return None
    return BackgroundApprovalDecision(status=cast("_DecisionStatus", str(row["decision"])), reason=row["reason"])


def deny_pending(transaction: Transaction, *, owner_id: str, reason: str) -> tuple[RecordedApprovalDecision, ...]:
    """Deny every pending call of one detached run, returning the decisions this recorded."""
    rows = transaction.fetchall(
        """
        SELECT tool_call_id FROM approval_calls
        WHERE owner_id = ? AND generation = 0 AND decision IS NULL
        ORDER BY tool_call_id/*bytes*/
        """,
        (owner_id,),
    )
    decisions = [
        resolve_call(
            transaction,
            CallKey.detached(owner_id, str(row["tool_call_id"])),
            requested="denied",
            reason=reason,
            metadata=None,
        )
        for row in rows
    ]
    return tuple(decision for decision in decisions if decision.recorded)


def prune(transaction: Transaction, principal_id: str, *, owner_id: str) -> bool:
    """Delete a detached run's calls once every one is decided and none has a live card."""
    blocked = transaction.fetchone(
        f"""
        SELECT 1 AS present FROM approval_calls AS calls
        WHERE calls.principal_id = ? AND calls.owner_id = ?
          AND (calls.decision IS NULL OR {LIVE_CARD.format(calls="calls")})
        LIMIT 1
        """,  # noqa: S608 - a fixed predicate, not input
        (principal_id, owner_id),
    )
    if blocked is not None:
        return False
    transaction.execute(
        "DELETE FROM approval_calls WHERE principal_id = ? AND owner_id = ?",
        (principal_id, owner_id),
    )
    return True


def resolve_call(
    transaction: Transaction,
    call: CallKey,
    *,
    requested: _DecisionStatus,
    reason: str | None,
    metadata: ApprovalDecisionMetadata | None,
    grant_id: str | None = None,
    now_ns: int | None = None,
) -> RecordedApprovalDecision:
    """Decide one call unless it is decided already, and queue its live request card's terminal edit.

    A reply-owned call is decided only while its paused reply is ``waiting`` or
    ``failing`` at the call's generation, and a failing reply turns an approval
    into a denial. The deadline always wins, judged at ``now_ns`` when a caller
    already judged the call's eligibility at that instant. A paused reply whose
    calls are all decided becomes ready once its publication lease is released.
    """
    owner = transaction.fetchone(
        "SELECT owner_kind FROM approval_calls WHERE owner_id = ? AND generation = ? AND tool_call_id = ?",
        (call.owner_id, call.generation, call.tool_call_id),
    )
    if owner is None:
        return approval_cards.RecordedApprovalDecision(resolution=None, recorded=False)
    owner_kind = cast("OwnerKind", str(owner["owner_kind"]))
    if owner_kind == "reply":
        # A failure fence and a decision can arrive through different stores.
        # Lock their paused reply before trusting its state, then the call.
        transaction.execute(
            "UPDATE approval_continuations SET state = state WHERE approval_id = ?",
            (call.owner_id,),
        )
    transaction.execute(
        "UPDATE approval_calls SET decision = decision WHERE owner_id = ? AND generation = ? AND tool_call_id = ?",
        (call.owner_id, call.generation, call.tool_call_id),
    )
    row = transaction.fetchone(
        """
        SELECT calls.expires_at_ns, calls.decision,
               continuations.principal_id AS continuation_principal_id, continuations.state,
               continuations.generation AS continuation_generation,
               continuations.failure_kind, continuations.failure_detail
        FROM approval_calls AS calls
        LEFT JOIN approval_continuations AS continuations
          ON calls.owner_kind = 'reply' AND continuations.approval_id = calls.owner_id
        WHERE calls.owner_id = ? AND calls.generation = ? AND calls.tool_call_id = ?
        """,
        (call.owner_id, call.generation, call.tool_call_id),
    )
    assert row is not None, "a call locked in this transaction exists"
    card = approval_cards.request_card(transaction, call)
    delivery_id = None if card is None else card.delivery_id
    card_event_id = None if card is None else card.card_event_id
    if row["decision"] is not None:
        return approval_cards.RecordedApprovalDecision(
            resolution=None if card is None else approval_cards.terminal_resolution(transaction, card),
            recorded=False,
            delivery_id=delivery_id,
            card_event_id=card_event_id,
        )
    if owner_kind == "reply" and (
        row["state"] not in {"waiting", "failing"} or int(row["continuation_generation"]) != call.generation
    ):
        return approval_cards.RecordedApprovalDecision(resolution=None, recorded=False)
    # Only a paused reply joins a continuation, so only a reply-owned call can carry a failure.
    failure = approval_continuations.failure_of_row(row)
    now = time.time_ns() if now_ns is None else now_ns
    decision, decision_reason = _effective_decision(
        requested=requested,
        reason=reason,
        expired=now >= int(row["expires_at_ns"]),
        failure_reason=None if failure is None else failure.reason,
    )
    decided_by = metadata.resolved_by if metadata is not None and decision == requested else None
    approved_scope = (
        (None if metadata is None else metadata.scheduled_scope) or EXACT_ARGUMENTS
        if owner_kind == "scheduled" and decision == "approved" and decided_by is not None
        else None
    )
    transaction.execute(
        """
        UPDATE approval_calls
        SET decision = ?, reason = ?, resolved_by = ?, resolved_at_ns = ?, approved_scope = ?, grant_id = ?
        WHERE owner_id = ? AND generation = ? AND tool_call_id = ? AND decision IS NULL
        """,
        (
            decision,
            decision_reason,
            decided_by,
            now,
            approved_scope,
            grant_id if decision == "approved" else None,
            call.owner_id,
            call.generation,
            call.tool_call_id,
        ),
    )
    resolution = (
        None
        if card is None
        else approval_cards.queue_terminal_edit(
            transaction,
            card,
            status=decision,
            reason=decision_reason,
            metadata=metadata if decision == requested else None,
        )
    )
    # Its waiter wakes, and its card's terminal edit is sent, as this commits, whoever decided it.
    approval_signals.decided(
        transaction,
        call,
        None if card is None else approval_signals.DecidedCard(card.delivery_id, card_event_id, card.room_id),
    )
    if owner_kind != "reply":
        return approval_cards.RecordedApprovalDecision(
            resolution=resolution,
            recorded=True,
            delivery_id=delivery_id,
            card_event_id=card_event_id,
        )
    continuation_principal_id = str(row["continuation_principal_id"])
    ready = transaction.fetchone(
        """
        UPDATE approval_continuations SET state = 'ready'
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND state = 'waiting'
          AND runtime_generation IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM approval_calls
              WHERE owner_id = ? AND generation = ? AND decision IS NULL
          )
        RETURNING approval_id
        """,
        (continuation_principal_id, call.owner_id, call.generation, call.owner_id, call.generation),
    )
    continuation = approval_continuations.get(transaction, continuation_principal_id, approval_id=call.owner_id)
    assert continuation is not None, "a paused reply decided in this transaction exists"
    return approval_cards.RecordedApprovalDecision(
        resolution=resolution,
        recorded=True,
        delivery_id=delivery_id,
        card_event_id=card_event_id,
        continuation_ready=ready is not None,
        continuation_entity_name=continuation.entity_name,
        continuation_room_id=continuation.room_id,
        source_event_ids=continuation.source_event_ids,
    )


def _effective_decision(
    *,
    requested: _DecisionStatus,
    reason: str | None,
    expired: bool,
    failure_reason: str | None,
) -> tuple[_DecisionStatus, str | None]:
    """Apply the deadline and failure fences to one requested decision."""
    if expired:
        return "expired", approval_cards.TIMEOUT_REASON
    if requested == "expired":
        return "expired", reason or approval_cards.TIMEOUT_REASON
    if requested == "approved" and failure_reason is not None:
        return "denied", failure_reason
    return requested, reason
