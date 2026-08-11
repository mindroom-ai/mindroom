"""Compact durable ownership for Agno runs suspended on tool approval."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.agent_storage import create_state_storage

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb


class ApprovalDecision(StrEnum):
    """One terminal decision for an exact paused tool call."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


type ApprovalContinuationState = Literal["pending", "ready", "claimed", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class ApprovalCall:
    """Routing and decision state for one tool execution in a paused run."""

    tool_call_id: str
    tool_name: str
    invoking_agent: str
    expires_at: str
    card_event_id: str | None = None
    decision: ApprovalDecision | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize this call into Agno approval context."""
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "invoking_agent": self.invoking_agent,
            "expires_at": self.expires_at,
            "card_event_id": self.card_event_id,
            "decision": self.decision.value if self.decision is not None else None,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ApprovalCall:
        """Restore a call from Agno approval context."""
        decision = value.get("decision")
        return cls(
            tool_call_id=cast("str", value["tool_call_id"]),
            tool_name=cast("str", value["tool_name"]),
            invoking_agent=cast("str", value["invoking_agent"]),
            expires_at=cast("str", value["expires_at"]),
            card_event_id=cast("str | None", value.get("card_event_id")),
            decision=ApprovalDecision(cast("str", decision)) if decision is not None else None,
            reason=cast("str | None", value.get("reason")),
        )


@dataclass(frozen=True, slots=True)
class ApprovalContinuation:
    """The small MindRoom-owned reference to one persisted paused Agno run."""

    approval_id: str
    run_id: str
    session_id: str
    entity_kind: Literal["agent", "team"]
    entity_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    response_event_id: str
    calls: tuple[ApprovalCall, ...]
    execution_identity: dict[str, object]
    source_event_ids: tuple[str, ...]
    state: ApprovalContinuationState = "pending"
    claimant_id: str | None = None
    failure_reason: str | None = None
    team_member_names: tuple[str, ...] = ()
    team_mode: str | None = None

    def to_context(self) -> dict[str, object]:
        """Serialize the continuation into Agno approval context."""
        return {
            "version": 1,
            "entity_kind": self.entity_kind,
            "entity_name": self.entity_name,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "requester_id": self.requester_id,
            "response_event_id": self.response_event_id,
            "calls": [call.to_dict() for call in self.calls],
            "execution_identity": self.execution_identity,
            "source_event_ids": list(self.source_event_ids),
            "claimant_id": self.claimant_id,
            "failure_reason": self.failure_reason,
            "team_member_names": list(self.team_member_names),
            "team_mode": self.team_mode,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ApprovalContinuation:
        """Restore one continuation from an Agno approval row."""
        context = cast("dict[str, object]", row["context"])
        raw_calls = cast("list[dict[str, object]]", context["calls"])
        return cls(
            approval_id=cast("str", row["id"]),
            run_id=cast("str", row["run_id"]),
            session_id=cast("str", row["session_id"]),
            entity_kind=cast("Literal['agent', 'team']", context["entity_kind"]),
            entity_name=cast("str", context["entity_name"]),
            room_id=cast("str", context["room_id"]),
            thread_id=cast("str | None", context.get("thread_id")),
            requester_id=cast("str", context["requester_id"]),
            response_event_id=cast("str", context["response_event_id"]),
            calls=tuple(ApprovalCall.from_dict(call) for call in raw_calls),
            execution_identity=cast("dict[str, object]", context["execution_identity"]),
            source_event_ids=tuple(cast("list[str]", context["source_event_ids"])),
            state=cast("ApprovalContinuationState", row["status"]),
            claimant_id=cast("str | None", context.get("claimant_id")),
            failure_reason=cast("str | None", context.get("failure_reason")),
            team_member_names=tuple(cast("list[str]", context.get("team_member_names", []))),
            team_mode=cast("str | None", context.get("team_mode")),
        )


class ApprovalContinuationStore:
    """Use Agno's approval table as a guarded continuation coordinator."""

    def __init__(self, storage_root: Path) -> None:
        self._db: BaseDb = create_state_storage(
            "tool_approval_continuations",
            storage_root,
            subdir="tracking",
            session_table="tool_approval_continuation_sessions",
        )
        self._lock = threading.RLock()

    def create(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Create one durable continuation."""
        first_call = continuation.calls[0]
        self._db.create_approval(
            {
                "id": continuation.approval_id,
                "run_id": continuation.run_id,
                "session_id": continuation.session_id,
                "status": continuation.state,
                "source_type": continuation.entity_kind,
                "approval_type": "mindroom",
                "pause_type": "confirmation",
                "tool_name": first_call.tool_name,
                "source_name": continuation.entity_name,
                "user_id": continuation.requester_id,
                "context": continuation.to_context(),
                "run_status": "PAUSED",
            },
        )
        return continuation

    def get(self, approval_id: str) -> ApprovalContinuation | None:
        """Return one continuation by ID."""
        row = self._db.get_approval(approval_id)
        return None if row is None else ApprovalContinuation.from_row(row)

    def resolve_call(
        self,
        approval_id: str,
        tool_call_id: str,
        decision: ApprovalDecision,
        *,
        reason: str | None = None,
    ) -> ApprovalContinuation | None:
        """Commit the first decision for one exact call."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "pending":
                return current
            calls: list[ApprovalCall] = []
            matched = False
            for call in current.calls:
                if call.tool_call_id == tool_call_id:
                    matched = True
                    calls.append(call if call.decision is not None else replace(call, decision=decision, reason=reason))
                else:
                    calls.append(call)
            if not matched:
                return current
            state: ApprovalContinuationState = (
                "ready" if all(call.decision is not None for call in calls) else "pending"
            )
            updated = replace(current, calls=tuple(calls), state=state)
            row = self._db.update_approval(
                approval_id,
                expected_status="pending",
                status=state,
                context=updated.to_context(),
            )
            return self.get(approval_id) if row is None else ApprovalContinuation.from_row(row)

    def attach_card(self, approval_id: str, tool_call_id: str, card_event_id: str) -> ApprovalContinuation | None:
        """Record the Matrix card that owns one still-pending call."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "pending":
                return current
            calls = tuple(
                replace(call, card_event_id=card_event_id) if call.tool_call_id == tool_call_id else call
                for call in current.calls
            )
            updated = replace(current, calls=calls)
            row = self._db.update_approval(
                approval_id,
                expected_status="pending",
                status="pending",
                context=updated.to_context(),
            )
            return self.get(approval_id) if row is None else ApprovalContinuation.from_row(row)

    def claim(self, approval_id: str, claimant_id: str) -> ApprovalContinuation | None:
        """Claim one ready continuation exactly once."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "ready":
                return None
            claimed = replace(current, state="claimed", claimant_id=claimant_id)
            row = self._db.update_approval(
                approval_id,
                expected_status="ready",
                status="claimed",
                context=claimed.to_context(),
            )
            return None if row is None else ApprovalContinuation.from_row(row)

    def complete(self, approval_id: str, claimant_id: str) -> ApprovalContinuation | None:
        """Mark the claimant's continuation complete."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "claimed" or current.claimant_id != claimant_id:
                return None
            completed = replace(current, state="completed")
            row = self._db.update_approval(
                approval_id,
                expected_status="claimed",
                status="completed",
                context=completed.to_context(),
                run_status="COMPLETED",
            )
            return None if row is None else ApprovalContinuation.from_row(row)

    def fail(self, approval_id: str, reason: str) -> ApprovalContinuation | None:
        """Make a nonterminal continuation permanently failed."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state in {"completed", "failed"}:
                return current
            failed = replace(current, state="failed", failure_reason=reason)
            row = self._db.update_approval(
                approval_id,
                expected_status=current.state,
                status="failed",
                context=failed.to_context(),
                run_status="ERROR",
            )
            return None if row is None else ApprovalContinuation.from_row(row)

    def recoverable(self) -> tuple[ApprovalContinuation, ...]:
        """Return continuations that startup must recover or settle."""
        records: list[ApprovalContinuation] = []
        for state in ("pending", "ready", "claimed"):
            rows, _total = self._db.get_approvals(status=state, approval_type="mindroom", limit=1000)
            records.extend(ApprovalContinuation.from_row(row) for row in rows)
        return tuple(records)
