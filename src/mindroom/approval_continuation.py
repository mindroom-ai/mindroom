"""Compact durable ownership for Agno runs suspended on tool approval."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import Table, select, tuple_

from mindroom.agent_storage import create_state_storage
from mindroom.history.types import HistoryScope

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.sqlite import SqliteDb


class ApprovalDecision(StrEnum):
    """One terminal decision for an exact paused tool call."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


type _ApprovalContinuationState = Literal[
    "publishing",
    "pending",
    "ready",
    "claimed",
    "settling",
    "completed",
    "failed",
]
type _PublishedContinuationState = Literal["pending", "ready"]
_PAGE_SIZE = 100
_RECOVERABLE_STATES = ("publishing", "pending", "ready", "claimed", "settling")
_TIMEOUT_REASON = "Tool approval request timed out."

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
    request_body: str = ""
    transport_sender_id: str | None = None
    source_kind: str = "message"
    attachment_ids: tuple[str, ...] = ()
    message_received_depth: int = 0
    history_scope: HistoryScope | None = None
    delivery_principal_id: str | None = None
    runtime_generation: str | None = None
    settlement_id: str | None = None
    _stored_context: dict[str, object] | None = field(default=None, compare=False, repr=False)

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
            "request_body": self.request_body,
            "transport_sender_id": self.transport_sender_id,
            "source_kind": self.source_kind,
            "attachment_ids": list(self.attachment_ids),
            "message_received_depth": self.message_received_depth,
            "history_scope": self.history_scope.to_metadata() if self.history_scope is not None else None,
            "delivery_principal_id": self.delivery_principal_id,
            "runtime_generation": self.runtime_generation,
            "settlement_id": self.settlement_id,
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
            request_body=cast("str", context.get("request_body", "")),
            transport_sender_id=cast("str | None", context.get("transport_sender_id")),
            source_kind=cast("str", context.get("source_kind", "message")),
            attachment_ids=tuple(cast("list[str]", context.get("attachment_ids", []))),
            message_received_depth=cast("int", context.get("message_received_depth", 0)),
            history_scope=HistoryScope.from_metadata(context.get("history_scope")),
            delivery_principal_id=cast("str | None", context.get("delivery_principal_id")),
            runtime_generation=cast("str | None", context.get("runtime_generation")),
            settlement_id=cast("str | None", context.get("settlement_id")),
            _stored_context=context,
        )


class ApprovalContinuationStore:
    """Use Agno's approval table as a guarded continuation coordinator."""

    def __init__(self, storage_root: Path) -> None:
        self._db = cast(
            "SqliteDb",
            create_state_storage(
                "tool_approval_continuations",
                storage_root,
                subdir="tracking",
                session_table="tool_approval_continuation_sessions",
            ),
        )
        self._lock = _store_lock(storage_root)

    def _approval_table(self, *, create: bool = False) -> Table | None:
        """Return Agno's approval table while preserving database read failures."""
        return self._db._get_table(table_type="approvals", create_table_if_not_found=create)

    def create(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Create one durable continuation."""
        if self._approval_table(create=True) is None:
            msg = "Tool approval continuation table is unavailable"
            raise RuntimeError(msg)
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
        created = self.get(continuation.approval_id)
        if created is None:
            msg = "Tool approval continuation disappeared immediately after creation"
            raise RuntimeError(msg)
        return created

    def get(self, approval_id: str) -> ApprovalContinuation | None:
        """Return one continuation by ID."""
        table = self._approval_table()
        if table is None:
            return None
        with self._db.Session() as session:
            result = session.execute(select(table).where(table.c.id == approval_id)).fetchone()
        return None if result is None else ApprovalContinuation._from_row(dict(result._mapping))

    def _persist(
        self,
        current: ApprovalContinuation,
        updated: ApprovalContinuation,
        *,
        run_status: str | None = None,
    ) -> ApprovalContinuation | None:
        """Commit one state transition guarded by the current durable state."""
        values: dict[str, object] = {
            "status": updated.state,
            "run_id": updated.run_id,
            "session_id": updated.session_id,
            "context": updated._to_context(),
        }
        if run_status is not None:
            values["run_status"] = run_status
        values["updated_at"] = int(time.time())
        table = self._approval_table()
        if table is None:
            msg = "Tool approval continuation table disappeared before its guarded update"
            raise RuntimeError(msg)
        with self._db.Session() as session, session.begin():
            result = session.execute(
                table.update()
                .where(
                    table.c.id == current.approval_id,
                    table.c.status == current.state,
                    table.c.context == (current._stored_context or current._to_context()),
                )
                .values(**values),
            )
            if result.rowcount == 0:
                return None
            row = session.execute(select(table).where(table.c.id == current.approval_id)).fetchone()
        if row is None:
            msg = "Tool approval continuation disappeared after its guarded update"
            raise RuntimeError(msg)
        return ApprovalContinuation._from_row(dict(row._mapping))

    def _records(self, states: tuple[str, ...]) -> tuple[ApprovalContinuation, ...]:
        records: list[ApprovalContinuation] = []
        table = self._approval_table()
        if table is None:
            return ()
        for state in states:
            cursor: tuple[int, str] | None = None
            while True:
                statement = (
                    select(table)
                    .where(table.c.status == state, table.c.approval_type == "mindroom")
                    .order_by(table.c.created_at.desc(), table.c.id.desc())
                    .limit(_PAGE_SIZE)
                )
                if cursor is not None:
                    statement = statement.where(tuple_(table.c.created_at, table.c.id) < cursor)
                with self._db.Session() as session:
                    rows = session.execute(statement).fetchall()
                records.extend(ApprovalContinuation._from_row(dict(row._mapping)) for row in rows)
                if len(rows) < _PAGE_SIZE:
                    break
                last = rows[-1]._mapping
                cursor = (int(last["created_at"]), str(last["id"]))
        return tuple(records)

    def bind_response_event(
        self,
        approval_id: str,
        response_event_id: str,
        *,
        state: Literal["publishing", "settling"] | _PublishedContinuationState,
        calls: tuple[ApprovalCall, ...] | None = None,
    ) -> ApprovalContinuation | None:
        """Atomically bind visible delivery and optionally publish the continuation."""
        with self._lock:
            current = self.get(approval_id)
            if current is None:
                return None
            publishing_transition = current.state == "publishing" and state in {"publishing", "pending", "ready"}
            settling_binding = current.state == state == "settling"
            if not publishing_transition and not settling_binding:
                return current
            published = replace(
                current,
                response_event_id=response_event_id,
                calls=current.calls if calls is None else calls,
                state=state,
            )
            return self._persist(current, published)

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
                    if call.decision is not None:
                        calls.append(call)
                        continue
                    committed_decision = decision
                    committed_reason = reason
                    if decision is ApprovalDecision.APPROVED and _call_expired(call):
                        committed_decision = ApprovalDecision.EXPIRED
                        committed_reason = _TIMEOUT_REASON
                    calls.append(replace(call, decision=committed_decision, reason=committed_reason))
                else:
                    calls.append(call)
            if not matched:
                return current
            updated = replace(current, calls=tuple(calls))
            return self._persist(current, updated)

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
            return self._persist(current, updated)

    def for_source_event(self, source_event_id: str) -> ApprovalContinuation | None:
        """Return the continuation that durably owns one inbound journal source."""
        return next(
            (
                continuation
                for continuation in self._records(_RECOVERABLE_STATES)
                if source_event_id in continuation.source_event_ids
            ),
            None,
        )

    def for_card_event(self, card_event_id: str) -> ApprovalContinuation | None:
        """Return pending continuation ownership for one attached Matrix card."""
        return next(
            (
                continuation
                for continuation in self._records(("pending",))
                if any(call.card_event_id == card_event_id for call in continuation.calls)
            ),
            None,
        )

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
            return self._persist(current, updated)

    def claim(
        self,
        approval_id: str,
        claimant_id: str,
        *,
        runtime_generation: str | None = None,
    ) -> ApprovalContinuation | None:
        """Claim one ready continuation exactly once."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "ready":
                return None
            claimed = replace(
                current,
                state="claimed",
                claimant_id=claimant_id,
                runtime_generation=runtime_generation or current.runtime_generation,
            )
            return self._persist(current, claimed)

    def complete(self, approval_id: str, claimant_id: str) -> ApprovalContinuation | None:
        """Mark the claimant's continuation complete."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "claimed" or current.claimant_id != claimant_id:
                return None
            completed = replace(current, state="completed")
            return self._persist(current, completed, run_status="COMPLETED")

    def begin_failure(
        self,
        approval_id: str,
        reason: str,
        *,
        claimant_id: str | None,
        settlement_id: str,
        runtime_generation: str,
    ) -> ApprovalContinuation | None:
        """Acquire exclusive durable ownership of terminal failure settlement."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state in {"completed", "failed"}:
                return current
            if current.state == "claimed" and current.claimant_id != claimant_id:
                return current
            if (
                current.state == "settling"
                and current.settlement_id is not None
                and current.runtime_generation == runtime_generation
            ):
                return current
            settling = replace(
                current,
                state="settling",
                failure_reason=current.failure_reason or reason,
                runtime_generation=runtime_generation,
                settlement_id=settlement_id,
            )
            return self._persist(current, settling)

    def release_failure(self, approval_id: str, settlement_id: str) -> ApprovalContinuation | None:
        """Release an incomplete settlement so a later retry can own it."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "settling" or current.settlement_id != settlement_id:
                return current
            return self._persist(current, replace(current, settlement_id=None))

    def finish_failure(
        self,
        approval_id: str,
        settlement_id: str,
        reason: str,
    ) -> ApprovalContinuation | None:
        """Make the exclusively owned failure settlement terminal."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "settling" or current.settlement_id != settlement_id:
                return current
            failed = replace(
                current,
                state="failed",
                failure_reason=current.failure_reason or reason,
                settlement_id=None,
            )
            return self._persist(current, failed, run_status="ERROR")

    def fail_claimed(
        self,
        approval_id: str,
        claimant_id: str,
        reason: str,
    ) -> ApprovalContinuation | None:
        """Fail a claimed run after its normal lifecycle already settled visibly."""
        with self._lock:
            current = self.get(approval_id)
            if current is None or current.state != "claimed" or current.claimant_id != claimant_id:
                return current
            failed = replace(current, state="failed", failure_reason=reason)
            return self._persist(current, failed, run_status="ERROR")

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
            return self._persist(current, advanced)

    def recoverable(self) -> tuple[ApprovalContinuation, ...]:
        """Return continuations that startup must recover or settle."""
        return self._records(_RECOVERABLE_STATES)

    def close(self) -> None:
        """Close this store handle."""
        self._db.close()


def _call_expired(call: ApprovalCall) -> bool:
    """Fail closed when an exact paused call is past or lacks a valid deadline."""
    try:
        expires_at = datetime.fromisoformat(call.expires_at)
    except ValueError:
        return True
    return expires_at.tzinfo is None or expires_at <= datetime.now(UTC)
