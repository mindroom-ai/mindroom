"""Paused Agno runs owned by their original event-journal sources."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from . import journal
from .models import DeliveryStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

type ApprovalContinuationState = Literal["waiting", "ready", "claimed", "failing"]

_CONTINUATION_COLUMNS = """
    approval_id, primary_source_event_id, entity_name, state, generation,
    runtime_generation, failure_reason, context_json
"""


class ApprovalDecision(StrEnum):
    """One terminal decision for an exact paused tool call."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalCall:
    """One exact tool call in the current paused generation."""

    tool_call_id: str
    tool_name: str
    invoking_agent: str
    expires_at_ns: int
    decision: ApprovalDecision | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalContinuation:
    """The MindRoom context required to continue one persisted Agno pause."""

    approval_id: str
    run_id: str
    session_id: str
    entity_kind: Literal["agent", "team"]
    entity_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    response_event_id: str
    source_event_ids: tuple[str, ...]
    calls: tuple[ApprovalCall, ...]
    snapshot: Mapping[str, object]
    state: ApprovalContinuationState
    runtime_generation: str | None = None
    failure_reason: str | None = None
    generation: int = 0


def _context(continuation: ApprovalContinuation) -> dict[str, object]:
    """Return the opaque response snapshot stored beside normalized routing facts."""
    return {
        "run_id": continuation.run_id,
        "session_id": continuation.session_id,
        "entity_kind": continuation.entity_kind,
        "room_id": continuation.room_id,
        "thread_id": continuation.thread_id,
        "requester_id": continuation.requester_id,
        "response_event_id": continuation.response_event_id,
        "snapshot": dict(continuation.snapshot),
    }


def _json(value: Mapping[str, object]) -> str:
    """Encode one stable JSON object for both durable backends."""
    return json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _load(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
) -> ApprovalContinuation | None:
    """Load one continuation and its exact ordered sources and calls."""
    row = transaction.fetchone(
        f"""
        SELECT {_CONTINUATION_COLUMNS}
        FROM approval_continuations
        WHERE principal_id = ? AND approval_id = ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, approval_id),
    )
    if row is None:
        return None
    source_rows = transaction.fetchall(
        """
        SELECT event_id FROM approval_continuation_sources
        WHERE principal_id = ? AND approval_id = ?
        ORDER BY source_ordinal
        """,
        (principal_id, approval_id),
    )
    call_rows = transaction.fetchall(
        """
        SELECT tool_call_id, tool_name, invoking_agent, expires_at_ns, decision, reason
        FROM approval_continuation_calls
        WHERE principal_id = ? AND approval_id = ? AND generation = ?
        ORDER BY call_ordinal
        """,
        (principal_id, approval_id, int(row["generation"])),
    )
    return _from_rows(row, source_rows, call_rows)


def _from_rows(
    row: Row,
    source_rows: tuple[Row, ...],
    call_rows: tuple[Row, ...],
) -> ApprovalContinuation:
    """Decode one normalized continuation aggregate."""
    context = json.loads(str(row["context_json"]))
    if not isinstance(context, dict):
        msg = f"Approval continuation {row['approval_id']!r} has a non-object context"
        raise TypeError(msg)
    stored = cast("dict[str, Any]", context)
    raw_snapshot = stored.get("snapshot", {})
    if not isinstance(raw_snapshot, dict):
        msg = f"Approval continuation {row['approval_id']!r} has a non-object snapshot"
        raise TypeError(msg)
    calls = tuple(
        ApprovalCall(
            tool_call_id=str(call["tool_call_id"]),
            tool_name=str(call["tool_name"]),
            invoking_agent=str(call["invoking_agent"]),
            expires_at_ns=int(call["expires_at_ns"]),
            decision=(ApprovalDecision(str(call["decision"])) if call["decision"] is not None else None),
            reason=cast("str | None", call["reason"]),
        )
        for call in call_rows
    )
    return ApprovalContinuation(
        approval_id=str(row["approval_id"]),
        run_id=cast("str", stored["run_id"]),
        session_id=cast("str", stored["session_id"]),
        entity_kind=cast("Literal['agent', 'team']", stored["entity_kind"]),
        entity_name=str(row["entity_name"]),
        room_id=cast("str", stored["room_id"]),
        thread_id=cast("str | None", stored.get("thread_id")),
        requester_id=cast("str", stored["requester_id"]),
        response_event_id=cast("str", stored["response_event_id"]),
        source_event_ids=tuple(str(source["event_id"]) for source in source_rows),
        calls=calls,
        snapshot=cast("dict[str, object]", raw_snapshot),
        state=cast("ApprovalContinuationState", row["state"]),
        runtime_generation=cast("str | None", row["runtime_generation"]),
        failure_reason=cast("str | None", row["failure_reason"]),
        generation=int(row["generation"]),
    )


def create(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
) -> ApprovalContinuation | None:
    """Create one paused-run owner only while all of its sources remain pending."""
    if not continuation.source_event_ids:
        return None
    for event_id in continuation.source_event_ids:
        row = transaction.fetchone(
            """
            SELECT 1 AS present FROM journal_events
            WHERE principal_id = ? AND event_id = ? AND state = 'pending'
            """,
            (principal_id, event_id),
        )
        if row is None:
            return None
    inserted = transaction.fetchone(
        """
        INSERT INTO approval_continuations (
            principal_id, approval_id, primary_source_event_id, entity_name, state,
            generation, runtime_generation, failure_reason, context_json, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (approval_id) DO NOTHING
        RETURNING approval_id
        """,
        (
            principal_id,
            continuation.approval_id,
            continuation.source_event_ids[0],
            continuation.entity_name,
            continuation.state,
            continuation.generation,
            continuation.runtime_generation,
            continuation.failure_reason,
            _json(_context(continuation)),
            time.time_ns(),
        ),
    )
    if inserted is None:
        existing = _load(transaction, principal_id, approval_id=continuation.approval_id)
        return existing if existing == continuation else None
    for ordinal, event_id in enumerate(continuation.source_event_ids):
        transaction.execute(
            """
            INSERT INTO approval_continuation_sources (
                principal_id, approval_id, event_id, source_ordinal
            ) VALUES (?, ?, ?, ?)
            """,
            (principal_id, continuation.approval_id, event_id, ordinal),
        )
    for ordinal, call in enumerate(continuation.calls):
        transaction.execute(
            """
            INSERT INTO approval_continuation_calls (
                principal_id, approval_id, generation, tool_call_id, call_ordinal,
                tool_name, invoking_agent, expires_at_ns, decision, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                continuation.approval_id,
                continuation.generation,
                call.tool_call_id,
                ordinal,
                call.tool_name,
                call.invoking_agent,
                call.expires_at_ns,
                call.decision.value if call.decision is not None else None,
                call.reason,
            ),
        )
    return _load(transaction, principal_id, approval_id=continuation.approval_id)


def for_source(
    transaction: Transaction,
    principal_id: str,
    *,
    event_id: str,
) -> ApprovalContinuation | None:
    """Return the paused run that owns one exact source event."""
    row = transaction.fetchone(
        """
        SELECT approval_id FROM approval_continuation_sources
        WHERE principal_id = ? AND event_id = ?
        """,
        (principal_id, event_id),
    )
    return None if row is None else _load(transaction, principal_id, approval_id=str(row["approval_id"]))


def claim(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    runtime_generation: str,
) -> ApprovalContinuation | None:
    """Move one ready paused run into its single execution attempt."""
    claimed = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = 'claimed', runtime_generation = ?
        WHERE principal_id = ? AND approval_id = ? AND state = 'ready'
        RETURNING approval_id
        """,
        (runtime_generation, principal_id, approval_id),
    )
    return None if claimed is None else _load(transaction, principal_id, approval_id=approval_id)


def advance(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    claimant_generation: int,
    run_id: str,
    session_id: str,
    calls: tuple[ApprovalCall, ...],
) -> ApprovalContinuation | None:
    """Replace one claimed generation with the next exact Agno pause."""
    current = _load(transaction, principal_id, approval_id=approval_id)
    if current is None:
        return None
    next_generation = claimant_generation + 1
    state: ApprovalContinuationState = "ready" if all(call.decision is not None for call in calls) else "waiting"
    advanced = replace(
        current,
        run_id=run_id,
        session_id=session_id,
        calls=calls,
        state=state,
        runtime_generation=None,
        failure_reason=None,
        generation=next_generation,
    )
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = ?, generation = ?, runtime_generation = NULL,
            failure_reason = NULL, context_json = ?
        WHERE principal_id = ? AND approval_id = ?
          AND state = 'claimed' AND generation = ?
        RETURNING approval_id
        """,
        (
            state,
            next_generation,
            _json(_context(advanced)),
            principal_id,
            approval_id,
            claimant_generation,
        ),
    )
    if updated is None:
        return None
    for ordinal, call in enumerate(calls):
        transaction.execute(
            """
            INSERT INTO approval_continuation_calls (
                principal_id, approval_id, generation, tool_call_id, call_ordinal,
                tool_name, invoking_agent, expires_at_ns, decision, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                approval_id,
                next_generation,
                call.tool_call_id,
                ordinal,
                call.tool_name,
                call.invoking_agent,
                call.expires_at_ns,
                call.decision.value if call.decision is not None else None,
                call.reason,
            ),
        )
    return _load(transaction, principal_id, approval_id=approval_id)


def request_failure(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    reason: str,
    expected_state: ApprovalContinuationState,
) -> ApprovalContinuation | None:
    """Fence one observed continuation state against any later execution."""
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_reason = ?
        WHERE principal_id = ? AND approval_id = ? AND state = ?
        RETURNING approval_id
        """,
        (reason, principal_id, approval_id, expected_state),
    )
    return None if updated is None else _load(transaction, principal_id, approval_id=approval_id)


def finish(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
) -> bool:
    """Release sources only after the continuation's FINAL delivery is acknowledged."""
    continuation = _load(transaction, principal_id, approval_id=approval_id)
    if continuation is None:
        return False
    delivered = transaction.fetchone(
        """
        SELECT 1 AS present FROM response_outbox
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
          AND acknowledged_event_id IS NOT NULL
        """,
        (principal_id, continuation.source_event_ids[0], DeliveryStage.FINAL.value),
    )
    if delivered is None:
        return False
    journal.settle_many(transaction, principal_id, continuation.source_event_ids)
    transaction.execute(
        "DELETE FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    return True
