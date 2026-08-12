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


type _ApprovalContinuationState = Literal["publishing", "pending", "ready", "claimed", "completed", "failed"]
type _PublishedContinuationState = Literal["pending", "ready"]
_PAGE_SIZE = 100

_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.RLock] = {}


def _store_lock(storage_root: Path) -> threading.RLock:
    """Return the process-wide coordinator for one continuation database."""
    key = str((storage_root / "tracking").resolve())
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


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
    decision_recorded: bool = False

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
            "decision_recorded": self.decision_recorded,
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
            decision_recorded=bool(value.get("decision_recorded", False)),
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
    response_event_id: str | None
    calls: tuple[ApprovalCall, ...]
    execution_identity: dict[str, object]
    source_event_ids: tuple[str, ...]
    runtime_model_name: str | None = None
    state: _ApprovalContinuationState = "pending"
    claimant_id: str | None = None
    failure_reason: str | None = None
    team_member_names: tuple[str, ...] = ()
    team_mode: str | None = None
    generation: int = 0

    def _to_context(self) -> dict[str, object]:
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
            "runtime_model_name": self.runtime_model_name,
            "claimant_id": self.claimant_id,
            "failure_reason": self.failure_reason,
            "team_member_names": list(self.team_member_names),
            "team_mode": self.team_mode,
            "generation": self.generation,
        }

    @classmethod
    def _from_row(cls, row: dict[str, Any]) -> ApprovalContinuation:
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
            response_event_id=cast("str | None", context["response_event_id"]),
            calls=tuple(ApprovalCall.from_dict(call) for call in raw_calls),
            execution_identity=cast("dict[str, object]", context["execution_identity"]),
            source_event_ids=tuple(cast("list[str]", context["source_event_ids"])),
            runtime_model_name=cast("str | None", context.get("runtime_model_name")),
            state=cast("_ApprovalContinuationState", row["status"]),
            claimant_id=cast("str | None", context.get("claimant_id")),
            failure_reason=cast("str | None", context.get("failure_reason")),
            team_member_names=tuple(cast("list[str]", context.get("team_member_names", []))),
            team_mode=cast("str | None", context.get("team_mode")),
            generation=cast("int", context.get("generation", 0)),
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
        self._lock = _store_lock(storage_root)

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
                "context": continuation._to_context(),
                "run_status": "PAUSED",
            },
        )
        return continuation

    def get(self, approval_id: str) -> ApprovalContinuation | None:
        """Return one continuation by ID."""
        row = self._db.get_approval(approval_id)
        return None if row is None else ApprovalContinuation._from_row(row)

    def bind_response_event(
        self,
        approval_id: str,
        response_event_id: str,
        *,
        state: Literal["publishing"] | _PublishedContinuationState,
        calls: tuple[ApprovalCall, ...] | None = None,
    ) -> ApprovalContinuation | None:
        """Atomically bind visible delivery and optionally publish the continuation."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "publishing":
                return current
            published = replace(
                current,
                response_event_id=response_event_id,
                calls=current.calls if calls is None else calls,
                state=state,
            )
            row = self._db.update_approval(
                approval_id,
                expected_status="publishing",
                status=state,
                context=published._to_context(),
            )
            return self.get(approval_id) if row is None else ApprovalContinuation._from_row(row)

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
            state: _ApprovalContinuationState = "pending"
            updated = replace(current, calls=tuple(calls), state=state)
            row = self._db.update_approval(
                approval_id,
                expected_status="pending",
                status=state,
                context=updated._to_context(),
            )
            return self.get(approval_id) if row is None else ApprovalContinuation._from_row(row)

    def acknowledge_call(self, approval_id: str, tool_call_id: str) -> ApprovalContinuation | None:
        """Record that the winning decision is durable in the approval-card journal."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "pending":
                return current
            calls = tuple(
                replace(call, decision_recorded=True)
                if call.tool_call_id == tool_call_id and call.decision is not None
                else call
                for call in current.calls
            )
            state: _ApprovalContinuationState = (
                "ready" if all(call.decision is not None and call.decision_recorded for call in calls) else "pending"
            )
            updated = replace(current, calls=calls, state=state)
            row = self._db.update_approval(
                approval_id,
                expected_status="pending",
                status=state,
                context=updated._to_context(),
            )
            return self.get(approval_id) if row is None else ApprovalContinuation._from_row(row)

    def for_source_event(self, source_event_id: str) -> ApprovalContinuation | None:
        """Return the continuation that durably owns one inbound journal source."""
        for state in ("publishing", "pending", "ready", "claimed"):
            page = 1
            while True:
                rows, total = self._db.get_approvals(
                    status=state,
                    approval_type="mindroom",
                    limit=_PAGE_SIZE,
                    page=page,
                )
                for row in rows:
                    continuation = ApprovalContinuation._from_row(row)
                    if source_event_id in continuation.source_event_ids:
                        return continuation
                if page * _PAGE_SIZE >= total:
                    break
                page += 1
        return None

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
                context=updated._to_context(),
            )
            return self.get(approval_id) if row is None else ApprovalContinuation._from_row(row)

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
                context=claimed._to_context(),
            )
            return None if row is None else ApprovalContinuation._from_row(row)

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
                context=completed._to_context(),
                run_status="COMPLETED",
            )
            return None if row is None else ApprovalContinuation._from_row(row)

    def advance_pause(
        self,
        approval_id: str,
        claimant_id: str,
        *,
        run_id: str,
        session_id: str,
        calls: tuple[ApprovalCall, ...],
    ) -> ApprovalContinuation | None:
        """Atomically replace one claimed run with its next approval pause."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "claimed" or current.claimant_id != claimant_id:
                return None
            state: _ApprovalContinuationState = (
                "ready" if all(call.decision is not None and call.decision_recorded for call in calls) else "pending"
            )
            advanced = replace(
                current,
                run_id=run_id,
                session_id=session_id,
                calls=calls,
                state=state,
                claimant_id=None,
                generation=current.generation + 1,
            )
            row = self._db.update_approval(
                approval_id,
                expected_status="claimed",
                status=state,
                run_id=run_id,
                session_id=session_id,
                context=advanced._to_context(),
            )
            return None if row is None else ApprovalContinuation._from_row(row)

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
                context=failed._to_context(),
                run_status="ERROR",
            )
            return None if row is None else ApprovalContinuation._from_row(row)

    def recoverable(self) -> tuple[ApprovalContinuation, ...]:
        """Return continuations that startup must recover or settle."""
        records: list[ApprovalContinuation] = []
        for state in ("publishing", "pending", "ready", "claimed"):
            page = 1
            while True:
                rows, total = self._db.get_approvals(
                    status=state,
                    approval_type="mindroom",
                    limit=_PAGE_SIZE,
                    page=page,
                )
                records.extend(ApprovalContinuation._from_row(row) for row in rows)
                if page * _PAGE_SIZE >= total:
                    break
                page += 1
        return tuple(records)

    def close(self) -> None:
        """Close this store handle."""
        self._db.close()
