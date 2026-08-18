"""Paused Agno runs owned by their original event-journal sources."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.history.types import HistoryScope
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

from . import journal, membership_state, outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

type ApprovalContinuationState = Literal["waiting", "ready", "claimed", "failing"]

_CONTINUATION_COLUMNS = """
    approval_id, entity_name, state, generation,
    runtime_generation, failure_reason, context_json
"""


def _unavailable_notice_delivery_id(approval_id: str, membership_epoch: int) -> str:
    """Return one membership's delivery identity for an unavailable-owner notice."""
    return f"approval-unavailable:{approval_id}:{membership_epoch}"


def enqueue_unavailable_notice(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
) -> str | None:
    """Enqueue the current membership's physical attempt for one logical notice."""
    membership_epoch = membership_state.claim_active_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
    )
    if membership_epoch is None:
        return None
    delivery_id = _unavailable_notice_delivery_id(approval_id, membership_epoch)
    transaction_id = outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=DeliveryStage.FINAL,
        event_type="m.room.message",
        room_id=room_id,
        membership_epoch=membership_epoch,
        thread_id=thread_id,
        payload=payload,
        edits_event_id=None,
    )
    return delivery_id if transaction_id is not None else None


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
    human_approval_required: bool | None = None


@dataclass(frozen=True, slots=True)
class ApprovalMemoryTurn:
    """The original history fields consumed by conversation memory."""

    sender: str
    body: str


def _origin_to_dict(origin: TurnOrigin) -> dict[str, object]:
    """Serialize exact hook origin without coupling the store to responses."""
    return {
        "transport_sender_id": origin.transport_sender_id,
        "requester_id": origin.requester_id,
        "sender_entity_name": origin.sender_entity_name,
        "requester_entity_name": origin.requester_entity_name,
        "sender_kind": origin.sender_kind.value,
        "requester_kind": origin.requester_kind.value,
        "intent": origin.intent.value,
        "source_kind": origin.source_kind,
        "trust": origin.trust.value,
    }


def _origin_from_dict(value: object) -> TurnOrigin | None:
    """Restore one exact hook origin from the durable snapshot."""
    if not isinstance(value, dict):
        return None
    stored = cast("dict[str, object]", value)
    return TurnOrigin(
        transport_sender_id=cast("str", stored["transport_sender_id"]),
        requester_id=cast("str", stored["requester_id"]),
        sender_entity_name=cast("str | None", stored.get("sender_entity_name")),
        requester_entity_name=cast("str | None", stored.get("requester_entity_name")),
        sender_kind=SenderKind(cast("str", stored["sender_kind"])),
        requester_kind=SenderKind(cast("str", stored["requester_kind"])),
        intent=TurnIntent(cast("str", stored["intent"])),
        source_kind=cast("str", stored["source_kind"]),
        trust=TurnTrust(cast("str", stored["trust"])),
    )


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
    state: ApprovalContinuationState
    response_text: str = ""
    response_tool_trace: tuple[dict[str, object], ...] = ()
    response_presentation_state: dict[str, object] = field(default_factory=dict)
    show_tool_calls: bool = True
    execution_identity: dict[str, object] = field(default_factory=dict)
    runtime_model_name: str | None = None
    team_member_names: tuple[str, ...] = ()
    team_member_model_names: tuple[tuple[str, str], ...] = ()
    team_mode: str | None = None
    request_body: str = ""
    transport_sender_id: str | None = None
    source_kind: str = "message"
    attachment_ids: tuple[str, ...] = ()
    mentioned_agents: tuple[str, ...] = ()
    hook_source: str | None = None
    message_received_depth: int = 0
    dispatch_policy_source_kind: str | None = None
    correlation_id: str | None = None
    history_scope: HistoryScope | None = None
    origin: TurnOrigin | None = None
    memory_prompt: str | None = None
    memory_thread_history: tuple[ApprovalMemoryTurn, ...] = ()
    thread_summary_message_count_hint: int | None = None
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
        "response_text": continuation.response_text,
        "response_tool_trace": [dict(event) for event in continuation.response_tool_trace],
        "response_presentation_state": continuation.response_presentation_state,
        "show_tool_calls": continuation.show_tool_calls,
        "execution_identity": continuation.execution_identity,
        "runtime_model_name": continuation.runtime_model_name,
        "team_member_names": list(continuation.team_member_names),
        "team_member_model_names": [list(item) for item in continuation.team_member_model_names],
        "team_mode": continuation.team_mode,
        "request_body": continuation.request_body,
        "transport_sender_id": continuation.transport_sender_id,
        "source_kind": continuation.source_kind,
        "attachment_ids": list(continuation.attachment_ids),
        "mentioned_agents": list(continuation.mentioned_agents),
        "hook_source": continuation.hook_source,
        "message_received_depth": continuation.message_received_depth,
        "dispatch_policy_source_kind": continuation.dispatch_policy_source_kind,
        "correlation_id": continuation.correlation_id,
        "history_scope": continuation.history_scope.to_metadata() if continuation.history_scope is not None else None,
        "origin": _origin_to_dict(continuation.origin) if continuation.origin is not None else None,
        "memory_prompt": continuation.memory_prompt,
        "memory_thread_history": [
            {"sender": turn.sender, "body": turn.body} for turn in continuation.memory_thread_history
        ],
        "thread_summary_message_count_hint": continuation.thread_summary_message_count_hint,
    }


def _json(value: Mapping[str, object]) -> str:
    """Encode one stable JSON object for both durable backends."""
    return json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def get(
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
        SELECT tool_call_id, tool_name, invoking_agent, expires_at_ns, decision, reason,
               human_approval_required
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
    calls = tuple(
        ApprovalCall(
            tool_call_id=str(call["tool_call_id"]),
            tool_name=str(call["tool_name"]),
            invoking_agent=str(call["invoking_agent"]),
            expires_at_ns=int(call["expires_at_ns"]),
            decision=(ApprovalDecision(str(call["decision"])) if call["decision"] is not None else None),
            reason=cast("str | None", call["reason"]),
            human_approval_required=(
                bool(call["human_approval_required"]) if call["human_approval_required"] is not None else None
            ),
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
        state=cast("ApprovalContinuationState", row["state"]),
        response_text=cast("str", stored.get("response_text", "")),
        response_tool_trace=tuple(
            dict(event) for event in cast("list[dict[str, object]]", stored.get("response_tool_trace", []))
        ),
        response_presentation_state=cast(
            "dict[str, object]",
            stored.get("response_presentation_state", {}),
        ),
        show_tool_calls=stored.get("show_tool_calls", True) is not False,
        execution_identity=cast("dict[str, object]", stored.get("execution_identity", {})),
        runtime_model_name=cast("str | None", stored.get("runtime_model_name")),
        team_member_names=tuple(cast("list[str]", stored.get("team_member_names", []))),
        team_member_model_names=tuple(
            (str(item[0]), str(item[1]))
            for item in cast("list[list[str]]", stored.get("team_member_model_names", []))
            if len(item) == 2
        ),
        team_mode=cast("str | None", stored.get("team_mode")),
        request_body=cast("str", stored.get("request_body", "")),
        transport_sender_id=cast("str | None", stored.get("transport_sender_id")),
        source_kind=cast("str", stored.get("source_kind", "message")),
        attachment_ids=tuple(cast("list[str]", stored.get("attachment_ids", []))),
        mentioned_agents=tuple(cast("list[str]", stored.get("mentioned_agents", []))),
        hook_source=cast("str | None", stored.get("hook_source")),
        message_received_depth=int(stored.get("message_received_depth", 0)),
        dispatch_policy_source_kind=cast("str | None", stored.get("dispatch_policy_source_kind")),
        correlation_id=cast("str | None", stored.get("correlation_id")),
        history_scope=HistoryScope.from_metadata(stored.get("history_scope")),
        origin=_origin_from_dict(stored.get("origin")),
        memory_prompt=cast("str | None", stored.get("memory_prompt")),
        memory_thread_history=tuple(
            ApprovalMemoryTurn(
                sender=cast("str", turn["sender"]),
                body=cast("str", turn["body"]),
            )
            for turn in cast("list[dict[str, object]]", stored.get("memory_thread_history", []))
        ),
        thread_summary_message_count_hint=cast("int | None", stored.get("thread_summary_message_count_hint")),
        runtime_generation=cast("str | None", row["runtime_generation"]),
        failure_reason=cast("str | None", row["failure_reason"]),
        generation=int(row["generation"]),
    )


def _insert_calls(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
    generation: int,
    calls: tuple[ApprovalCall, ...],
) -> None:
    """Insert one ordered exact-call generation."""
    for ordinal, call in enumerate(calls):
        transaction.execute(
            """
            INSERT INTO approval_continuation_calls (
                principal_id, approval_id, generation, tool_call_id, call_ordinal,
                tool_name, invoking_agent, expires_at_ns, decision, reason,
                human_approval_required
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                approval_id,
                generation,
                call.tool_call_id,
                ordinal,
                call.tool_name,
                call.invoking_agent,
                call.expires_at_ns,
                call.decision.value if call.decision is not None else None,
                call.reason,
                call.human_approval_required,
            ),
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
            principal_id, approval_id, entity_name, state,
            generation, runtime_generation, failure_reason, context_json, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (approval_id) DO NOTHING
        RETURNING approval_id
        """,
        (
            principal_id,
            continuation.approval_id,
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
        existing = get(transaction, principal_id, approval_id=continuation.approval_id)
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
    _insert_calls(
        transaction,
        principal_id,
        continuation.approval_id,
        continuation.generation,
        continuation.calls,
    )
    return get(transaction, principal_id, approval_id=continuation.approval_id)


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
    return None if row is None else get(transaction, principal_id, approval_id=str(row["approval_id"]))


def for_entities(
    transaction: Transaction,
    entity_names: set[str],
    *,
    limit: int,
    after: tuple[str, str] | None = None,
) -> tuple[tuple[str, ApprovalContinuation], ...]:
    """Return one bounded page owned by exact managed entities."""
    if not entity_names:
        return ()
    ordered_names = sorted(entity_names)
    placeholders = ", ".join("?" for _name in ordered_names)
    cursor_clause = "" if after is None else " AND (entity_name/*bytes*/, approval_id/*bytes*/) > (?, ?)"
    cursor_params: tuple[object, ...] = () if after is None else after
    rows = transaction.fetchall(
        f"""
        SELECT principal_id, {_CONTINUATION_COLUMNS} FROM approval_continuations
        WHERE entity_name IN ({placeholders}){cursor_clause}
        ORDER BY entity_name/*bytes*/, approval_id/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - placeholders are fixed markers; values remain bound parameters
        (*ordered_names, *cursor_params, limit),
    )
    return _load_owners(transaction, rows)


def _load_owners(transaction: Transaction, rows: tuple[Row, ...]) -> tuple[tuple[str, ApprovalContinuation], ...]:
    """Load one page's normalized children without one query per owner."""
    if not rows:
        return ()
    approval_ids = tuple(str(row["approval_id"]) for row in rows)
    placeholders = ", ".join("?" for _approval_id in approval_ids)
    source_rows = transaction.fetchall(
        f"""
        SELECT approval_id, event_id FROM approval_continuation_sources
        WHERE approval_id IN ({placeholders})
        ORDER BY approval_id/*bytes*/, source_ordinal
        """,  # noqa: S608 - placeholders are fixed markers; values remain bound parameters
        approval_ids,
    )
    call_rows = transaction.fetchall(
        f"""
        SELECT calls.approval_id, calls.tool_call_id, calls.tool_name,
               calls.invoking_agent, calls.expires_at_ns, calls.decision, calls.reason,
               calls.human_approval_required
        FROM approval_continuation_calls AS calls
        JOIN approval_continuations AS continuations
          ON continuations.principal_id = calls.principal_id
         AND continuations.approval_id = calls.approval_id
         AND continuations.generation = calls.generation
        WHERE calls.approval_id IN ({placeholders})
        ORDER BY calls.approval_id/*bytes*/, calls.call_ordinal
        """,  # noqa: S608 - placeholders are fixed markers; values remain bound parameters
        approval_ids,
    )
    sources_by_approval: dict[str, list[Row]] = {approval_id: [] for approval_id in approval_ids}
    for source in source_rows:
        sources_by_approval[str(source["approval_id"])].append(source)
    calls_by_approval: dict[str, list[Row]] = {approval_id: [] for approval_id in approval_ids}
    for call in call_rows:
        calls_by_approval[str(call["approval_id"])].append(call)
    return tuple(
        (
            str(row["principal_id"]),
            _from_rows(
                row,
                tuple(sources_by_approval[str(row["approval_id"])]),
                tuple(calls_by_approval[str(row["approval_id"])]),
            ),
        )
        for row in rows
    )


def all_owners(
    transaction: Transaction,
    *,
    limit: int,
    after: tuple[str, str] | None = None,
) -> tuple[tuple[str, ApprovalContinuation], ...]:
    """Return one bounded owner page with its journal principals."""
    cursor_clause = "" if after is None else " WHERE (entity_name/*bytes*/, approval_id/*bytes*/) > (?, ?)"
    cursor_params: tuple[object, ...] = () if after is None else after
    rows = transaction.fetchall(
        f"""
        SELECT principal_id, {_CONTINUATION_COLUMNS} FROM approval_continuations
        {cursor_clause}
        ORDER BY entity_name/*bytes*/, approval_id/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - a fixed cursor clause, not input
        (*cursor_params, limit),
    )
    return _load_owners(transaction, rows)


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
    return None if claimed is None else get(transaction, principal_id, approval_id=approval_id)


def advance(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    claimant_generation: int,
    run_id: str,
    session_id: str,
    calls: tuple[ApprovalCall, ...],
    response_text: str | None = None,
    response_tool_trace: tuple[dict[str, object], ...] | None = None,
    response_presentation_state: dict[str, object] | None = None,
) -> ApprovalContinuation | None:
    """Replace one claimed generation with the next exact Agno pause."""
    current = get(transaction, principal_id, approval_id=approval_id)
    if current is None:
        return None
    next_generation = claimant_generation + 1
    # Every chained generation stays fenced until its ordered Matrix edit and
    # any cards are published. Even an automatically decided generation must
    # not become executable in the persist-before-ack crash window.
    state: ApprovalContinuationState = "waiting"
    publication_owner = current.runtime_generation
    advanced = replace(
        current,
        run_id=run_id,
        session_id=session_id,
        calls=calls,
        response_text=current.response_text if response_text is None else response_text,
        response_tool_trace=current.response_tool_trace if response_tool_trace is None else response_tool_trace,
        response_presentation_state=(
            current.response_presentation_state if response_presentation_state is None else response_presentation_state
        ),
        state=state,
        runtime_generation=publication_owner,
        failure_reason=None,
        generation=next_generation,
    )
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = ?, generation = ?, runtime_generation = ?,
            failure_reason = NULL, context_json = ?
        WHERE principal_id = ? AND approval_id = ?
          AND state = 'claimed' AND generation = ?
        RETURNING approval_id
        """,
        (
            state,
            next_generation,
            publication_owner,
            _json(_context(advanced)),
            principal_id,
            approval_id,
            claimant_generation,
        ),
    )
    if updated is None:
        return None
    _insert_calls(transaction, principal_id, approval_id, next_generation, calls)
    return get(transaction, principal_id, approval_id=approval_id)


def activate(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    expected_generation: int,
) -> ApprovalContinuation | None:
    """Release one publication lease and make its generation decidable or executable."""
    undecided = transaction.fetchone(
        """
        SELECT 1 AS present FROM approval_continuation_calls
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND decision IS NULL
        LIMIT 1
        """,
        (principal_id, approval_id, expected_generation),
    )
    state: Literal["waiting", "ready"] = "waiting" if undecided is not None else "ready"
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations SET state = ?, runtime_generation = NULL
        WHERE principal_id = ? AND approval_id = ? AND state = 'waiting'
          AND generation = ? AND runtime_generation IS NOT NULL
        RETURNING approval_id
        """,
        (state, principal_id, approval_id, expected_generation),
    )
    return None if updated is None else get(transaction, principal_id, approval_id=approval_id)


def request_failure(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    reason: str,
    expected_state: ApprovalContinuationState,
    expected_generation: int,
    expected_runtime_generation: str | None,
) -> ApprovalContinuation | None:
    """Fence one observed continuation state against any later execution."""
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_reason = ?
        WHERE principal_id = ? AND approval_id = ? AND state = ? AND generation = ?
          AND runtime_generation IS NOT DISTINCT FROM ?
          AND NOT EXISTS (
            SELECT 1 FROM matrix_delivery_outbox AS final
            WHERE final.principal_id = approval_continuations.principal_id
              AND final.delivery_id = (
                SELECT source.event_id FROM approval_continuation_sources AS source
                WHERE source.principal_id = approval_continuations.principal_id
                  AND source.approval_id = approval_continuations.approval_id
                  AND source.source_ordinal = 0
              )
              AND final.stage = 'final'
          )
        RETURNING approval_id
        """,
        (
            reason,
            principal_id,
            approval_id,
            expected_state,
            expected_generation,
            expected_runtime_generation,
        ),
    )
    return None if updated is None else get(transaction, principal_id, approval_id=approval_id)


def finish(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
) -> bool:
    """Release sources only after the continuation's FINAL delivery is acknowledged."""
    continuation = _get_locked(transaction, principal_id, approval_id=approval_id)
    if continuation is None:
        return False
    delivered = transaction.fetchone(
        """
        SELECT 1 AS present FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
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


def discard_unavailable(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    notice_principal_id: str,
) -> bool:
    """Release a permanently unavailable owner's sources after visible card cleanup."""
    observed = get(transaction, principal_id, approval_id=approval_id)
    if observed is None or observed.state != "failing":
        return False
    membership_epoch = membership_state.claim_active_membership_epoch(
        transaction,
        notice_principal_id,
        room_id=observed.room_id,
    )
    if membership_epoch is None:
        return False
    continuation = _get_locked(transaction, principal_id, approval_id=approval_id)
    if continuation is None or continuation.state != "failing" or continuation.room_id != observed.room_id:
        return False
    delivery_id = _unavailable_notice_delivery_id(approval_id, membership_epoch)
    # The membership row is already held before the cross-principal
    # continuation lock. The helper's membership claim is therefore reentrant;
    # its new lock is only the outbox row, preserving membership ->
    # continuation -> delivery order against router departure.
    ownership = outbox.claim_active_delivery_ownership(
        transaction,
        notice_principal_id,
        delivery_id=delivery_id,
        stage=DeliveryStage.FINAL,
        expected_room_id=continuation.room_id,
    )
    if ownership is None:
        return False
    delivered = transaction.fetchone(
        """
        SELECT 1 AS present FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
          AND acknowledged_event_id IS NOT NULL
        """,
        (notice_principal_id, delivery_id, DeliveryStage.FINAL.value),
    )
    if delivered is None:
        return False
    journal.settle_many(transaction, principal_id, continuation.source_event_ids)
    transaction.execute(
        "DELETE FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    return True


def _get_locked(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
) -> ApprovalContinuation | None:
    """Lock one aggregate before terminal paths settle its journal sources."""
    transaction.execute(
        """
        UPDATE approval_continuations SET state = state
        WHERE principal_id = ? AND approval_id = ?
        """,
        (principal_id, approval_id),
    )
    return get(transaction, principal_id, approval_id=approval_id)
