"""Paused runs owned by their exact pending journal events."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.history.types import HistoryScope
from mindroom.redaction import redact_sensitive_text
from mindroom.reply_lifecycle import ApprovalFailureKind, ReplyState
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload, serialize_tool_execution_identity
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

from . import approval_calls, approval_signals, membership_state, outbox, reply_messages, reply_spans
from .models import DeliveryStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.response_sources import ResponseSources
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from .approval_calls import ApprovalCall
    from .backend import Row, Transaction

# ``claimed`` is never stored: a ready continuation is claimed by the span its
# ``claim_span_id`` names, which the span's records own.
type ApprovalContinuationState = Literal["waiting", "ready", "claimed", "failing"]

_CONTINUATION_COLUMNS = """
    approval_id, span_id, claim_span_id, state, generation,
    runtime_generation, failure_kind, failure_detail, context_json
"""

# A continuation holds the pending sources of the span whose pause created it.
# Join on ``held_source.event_id``; ``held_source.ordinal = 0`` is the source
# recovery dispatches.
HELD_SOURCES = """
    reply_span_sources AS held_source
    JOIN approval_continuations AS continuations
      ON continuations.principal_id = held_source.principal_id
     AND continuations.span_id = held_source.span_id
     AND held_source.role = 'pending'
"""


def holds_source_in_room(alias: str) -> str:
    """Return SQL that is true when continuation ``alias`` holds a source admitted in the room its parameter binds."""
    return f"""
        EXISTS (
            SELECT 1 FROM reply_span_sources AS room_source
            JOIN journal_events AS room_event
              ON room_event.principal_id = room_source.principal_id
             AND room_event.event_id = room_source.event_id
            WHERE room_source.principal_id = {alias}.principal_id
              AND room_source.span_id = {alias}.span_id
              AND room_source.role = 'pending'
              AND room_event.room_id = ?
        )
    """  # noqa: S608 - a fixed alias, not input


# What a failure with no detail of its own says, as an approval's cards and its reply's error note show it.
_FAILURE_REASONS = {
    ApprovalFailureKind.ERROR: "Tool approval continuation failed safely.",
    ApprovalFailureKind.USER_STOP: "Tool approval request ended because the reply was stopped.",
    ApprovalFailureKind.RESTART: "Tool approval request ended because MindRoom restarted.",
    ApprovalFailureKind.INTERRUPTED: "Tool approval request ended because the reply was interrupted.",
    ApprovalFailureKind.SHUTDOWN: "Tool approval continuation was interrupted before final delivery and denied safely.",
}


@dataclass(frozen=True, slots=True)
class ApprovalFailure:
    """Why a paused reply's approval failed: its kind, and what failed for an ``ERROR``."""

    kind: ApprovalFailureKind
    detail: str | None = None

    @property
    def reason(self) -> str:
        """Return the redacted text that explains the failure: its cards' denial reason, and an error note's text."""
        return redact_sensitive_text(self.detail) if self.detail else _FAILURE_REASONS[self.kind]


def failure_of_row(row: Row) -> ApprovalFailure | None:
    """Decode the failure an ``approval_continuations`` row stores."""
    kind = cast("str | None", row["failure_kind"])
    return (
        None if kind is None else ApprovalFailure(ApprovalFailureKind(kind), cast("str | None", row["failure_detail"]))
    )


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
        "acts_for_requester": origin.acts_for_requester,
    }


def _origin_from_dict(stored: Mapping[str, object]) -> TurnOrigin:
    """Restore one exact hook origin from the durable snapshot."""
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
        # LEGACY_COMPAT: Approval continuation origins without the acts-for-requester flag.
        # Legacy format: the stored origin of an approval continuation had no acts_for_requester key.
        # Last legacy release: v2026.11.42; replacement: the next release stores the flag in every origin.
        # Handling: absence reads as False, so the speaker comes from the origin's intent alone, as it did for every
        #   continuation paused before contact turns let another sender's message run for an agent's owner.
        # Coverage: tests/test_event_journal_store.py::TestApprovalContinuations::test_continuation_origin_without_the_acts_for_requester_flag_loads_with_it_unset.
        acts_for_requester=stored.get("acts_for_requester") is True,
    )


@dataclass(frozen=True, slots=True)
class ApprovalContinuation:
    """The MindRoom context required to continue one persisted pause."""

    approval_id: str
    run_id: str
    session_id: str
    entity_kind: Literal["agent", "team"]
    entity_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    response_event_id: str
    sources: ResponseSources
    calls: tuple[ApprovalCall, ...]
    state: ApprovalContinuationState
    # Every earlier generation's calls, each decided before the continuation advanced past it, in pause order.
    earlier_calls: tuple[ApprovalCall, ...] = ()
    delegation_storage_bindings: dict[str, dict[str, object]] = field(default_factory=dict)
    show_tool_calls: bool = True
    # The identity the paused run executed under; the resumed run and its tools execute under it again.
    execution_identity: ToolExecutionIdentity = field(kw_only=True)
    runtime_model_name: str | None = None
    team_member_names: tuple[str, ...] = ()
    team_member_model_names: tuple[tuple[str, str], ...] = ()
    team_mode: str | None = None
    request_body: str = ""
    attachment_ids: tuple[str, ...] = ()
    mentioned_agents: tuple[str, ...] = ()
    hook_source: str | None = None
    message_received_depth: int = 0
    dispatch_policy_source_kind: str | None = None
    correlation_id: str | None = None
    history_scope: HistoryScope | None = None
    # Who sent and who requested the paused turn, restored for the resumed hooks.
    origin: TurnOrigin = field(kw_only=True)
    memory_prompt: str | None = None
    memory_thread_history: tuple[ApprovalMemoryTurn, ...] = ()
    thread_summary_message_count_hint: int | None = None
    # The bot instance publishing a waiting generation's cards, or the one
    # whose span runs a claimed continuation.
    runtime_generation: str | None = None
    # Why a failing continuation failed; every failing continuation has one.
    failure: ApprovalFailure | None = None
    generation: int = 0
    cli_call: dict[str, object] | None = None
    continuation_count: int = 0
    # The span whose pause created this continuation; it names the reply, and
    # the reply's records hold the room, thread, event, and sources read here.
    # A pointer to where those facts live rather than a fact of the run, so it
    # takes no part in comparing two continuations.
    span_id: str | None = field(default=None, compare=False)
    # The span that runs a claimed continuation: its reply's resume, or the
    # response that waited in place. A pointer to the claim, like ``span_id``.
    claim_span_id: str | None = field(default=None, compare=False)

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        """Return the captured pending sources owned by this continuation."""
        return self.sources.pending_event_ids

    @property
    def final_delivery_id(self) -> str:
        """Return the delivery of the run's final answer, which its resume or its settlement writes."""
        return self.source_event_ids[0]


def _context(continuation: ApprovalContinuation) -> dict[str, object]:
    """Return the opaque response snapshot stored beside normalized routing facts."""
    return {
        "cli_call": continuation.cli_call,
        "run_id": continuation.run_id,
        "continuation_count": continuation.continuation_count,
        "session_id": continuation.session_id,
        "entity_kind": continuation.entity_kind,
        "requester_id": continuation.requester_id,
        "delegation_storage_bindings": continuation.delegation_storage_bindings,
        "execution_identity": serialize_tool_execution_identity(continuation.execution_identity),
        "runtime_model_name": continuation.runtime_model_name,
        "team_member_names": list(continuation.team_member_names),
        "team_member_model_names": [list(item) for item in continuation.team_member_model_names],
        "team_mode": continuation.team_mode,
        "request_body": continuation.request_body,
        "attachment_ids": list(continuation.attachment_ids),
        "mentioned_agents": list(continuation.mentioned_agents),
        "hook_source": continuation.hook_source,
        "message_received_depth": continuation.message_received_depth,
        "dispatch_policy_source_kind": continuation.dispatch_policy_source_kind,
        "correlation_id": continuation.correlation_id,
        "history_scope": continuation.history_scope.to_metadata() if continuation.history_scope is not None else None,
        "origin": _origin_to_dict(continuation.origin),
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
    """Load one continuation and its exact ordered sources and every generation's calls."""
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
    return _from_rows(transaction, principal_id, row, approval_calls.reply_call_rows(transaction, (approval_id,)))


def _shows_tool_calls(presentation: str) -> bool:
    """Return the tool-call visibility a reply froze when it started.

    The reply layer's presentation codec owns this key; the journal reads only it so that it stays free of rendering
    imports.
    """
    return json.loads(presentation)["show_tool_calls"] is True


def _from_rows(
    transaction: Transaction,
    principal_id: str,
    row: Row,
    call_rows: tuple[Row, ...],
) -> ApprovalContinuation:
    """Decode one normalized continuation aggregate from its row and the calls of each generation up to its own."""
    context = json.loads(str(row["context_json"]))
    if not isinstance(context, dict):
        msg = f"Approval continuation {row['approval_id']!r} has a non-object context"
        raise TypeError(msg)
    stored = cast("dict[str, Any]", context)
    # The paused span and its reply own the run's room, thread, event, entity, sources, and visibility.
    span_id = cast("str | None", row["span_id"])
    span = None if span_id is None else reply_spans.load(transaction, principal_id, span_id)
    reply = None if span is None else reply_messages.load(transaction, principal_id, span.reply_id)
    if span is None or reply is None or reply.event_id is None:
        message = f"Approval continuation {row['approval_id']!r} lost the reply it paused"
        raise ValueError(message)
    claim_span_id = cast("str | None", row["claim_span_id"])
    claimed = row["state"] == "ready" and claim_span_id is not None
    claim_span = None if claim_span_id is None else reply_spans.load(transaction, principal_id, claim_span_id)
    generation = int(row["generation"])
    return ApprovalContinuation(
        cli_call=cast("dict[str, object] | None", stored["cli_call"]),
        approval_id=str(row["approval_id"]),
        run_id=cast("str", stored["run_id"]),
        continuation_count=int(stored["continuation_count"]),
        session_id=cast("str", stored["session_id"]),
        entity_kind=cast("Literal['agent', 'team']", stored["entity_kind"]),
        entity_name=reply.entity_name,
        room_id=reply.room_id,
        thread_id=reply.thread_id,
        requester_id=cast("str", stored["requester_id"]),
        response_event_id=reply.event_id,
        sources=span.sources,
        calls=tuple(approval_calls.reply_call(call) for call in call_rows if int(call["generation"]) == generation),
        earlier_calls=tuple(
            approval_calls.reply_call(call) for call in call_rows if int(call["generation"]) < generation
        ),
        state="claimed" if claimed else cast("ApprovalContinuationState", row["state"]),
        delegation_storage_bindings=cast(
            "dict[str, dict[str, object]]",
            stored["delegation_storage_bindings"],
        ),
        show_tool_calls=_shows_tool_calls(reply.presentation),
        execution_identity=parse_tool_execution_identity_payload(
            stored["execution_identity"],
            error_prefix="Approval continuation execution_identity",
        ),
        runtime_model_name=cast("str | None", stored["runtime_model_name"]),
        team_member_names=tuple(cast("list[str]", stored["team_member_names"])),
        team_member_model_names=tuple(
            (str(item[0]), str(item[1])) for item in cast("list[list[str]]", stored["team_member_model_names"])
        ),
        team_mode=cast("str | None", stored["team_mode"]),
        request_body=cast("str", stored["request_body"]),
        attachment_ids=tuple(cast("list[str]", stored["attachment_ids"])),
        mentioned_agents=tuple(cast("list[str]", stored["mentioned_agents"])),
        hook_source=cast("str | None", stored["hook_source"]),
        message_received_depth=int(stored["message_received_depth"]),
        dispatch_policy_source_kind=cast("str | None", stored["dispatch_policy_source_kind"]),
        correlation_id=cast("str | None", stored["correlation_id"]),
        history_scope=HistoryScope.from_metadata(stored["history_scope"]),
        origin=_origin_from_dict(cast("dict[str, object]", stored["origin"])),
        memory_prompt=cast("str | None", stored["memory_prompt"]),
        memory_thread_history=tuple(
            ApprovalMemoryTurn(
                sender=cast("str", turn["sender"]),
                body=cast("str", turn["body"]),
            )
            for turn in cast("list[dict[str, object]]", stored["memory_thread_history"])
        ),
        thread_summary_message_count_hint=cast("int | None", stored["thread_summary_message_count_hint"]),
        runtime_generation=(
            (None if claim_span is None else claim_span.bot_generation)
            if claimed
            else cast("str | None", row["runtime_generation"])
        ),
        failure=failure_of_row(row),
        generation=generation,
        span_id=cast("str | None", row["span_id"]),
        claim_span_id=claim_span_id,
    )


def create(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
) -> ApprovalContinuation | None:
    """Create one paused-run owner only while all of its sources remain pending.

    The continuation names the span whose pause creates it, in the same
    transaction as that pause.
    """
    assert continuation.span_id is not None, "every continuation pauses a reply span"
    span = reply_spans.load(transaction, principal_id, continuation.span_id)
    assert span is not None
    assert span.sources.pending_event_ids == continuation.source_event_ids, (
        "a continuation holds its paused span's sources"
    )
    # Admission and approval creation must agree which owner receives a
    # concurrent source redaction, on PostgreSQL as well as SQLite.
    membership_epoch = membership_state.claim_active_membership_epoch(
        transaction,
        principal_id,
        room_id=continuation.room_id,
    )
    if membership_epoch is None:
        return None
    for event_id in continuation.source_event_ids:
        row = transaction.fetchone(
            """
            SELECT 1 AS present FROM journal_events
            WHERE principal_id = ? AND event_id = ? AND room_id = ? AND state = 'pending'
            """,
            (principal_id, event_id, continuation.room_id),
        )
        if row is None or _holder(transaction, principal_id, event_id) not in {None, continuation.approval_id}:
            return None
    inserted = transaction.fetchone(
        """
        INSERT INTO approval_continuations (
            principal_id, approval_id, span_id, state, generation,
            runtime_generation, failure_kind, failure_detail, context_json, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (approval_id) DO NOTHING
        RETURNING approval_id
        """,
        (
            principal_id,
            continuation.approval_id,
            continuation.span_id,
            continuation.state,
            continuation.generation,
            continuation.runtime_generation,
            None if continuation.failure is None else continuation.failure.kind.value,
            None if continuation.failure is None else continuation.failure.detail,
            _json(_context(continuation)),
            time.time_ns(),
        ),
    )
    if inserted is None:
        existing = get(transaction, principal_id, approval_id=continuation.approval_id)
        return existing if existing == continuation else None
    approval_calls.insert_reply_generation(
        transaction,
        principal_id,
        approval_id=continuation.approval_id,
        generation=continuation.generation,
        calls=continuation.calls,
    )
    return get(transaction, principal_id, approval_id=continuation.approval_id)


def for_source(
    transaction: Transaction,
    principal_id: str,
    *,
    event_id: str,
) -> ApprovalContinuation | None:
    """Return the paused run that owns one exact source event."""
    approval_id = _holder(transaction, principal_id, event_id)
    return None if approval_id is None else get(transaction, principal_id, approval_id=approval_id)


def _holder(transaction: Transaction, principal_id: str, event_id: str) -> str | None:
    """Return the continuation that holds one source event, if any."""
    row = transaction.fetchone(
        f"""
        SELECT continuations.approval_id FROM {HELD_SOURCES}
        WHERE held_source.principal_id = ? AND held_source.event_id = ?
        """,  # noqa: S608 - a fixed join, not input
        (principal_id, event_id),
    )
    return None if row is None else str(row["approval_id"])


def _load_owners(transaction: Transaction, rows: tuple[Row, ...]) -> tuple[tuple[str, ApprovalContinuation], ...]:
    """Load one page's normalized children without one query per owner."""
    if not rows:
        return ()
    approval_ids = tuple(str(row["approval_id"]) for row in rows)
    calls_by_approval: dict[str, list[Row]] = {approval_id: [] for approval_id in approval_ids}
    for call in approval_calls.reply_call_rows(transaction, approval_ids):
        calls_by_approval[str(call["owner_id"])].append(call)
    owners = []
    for row in rows:
        principal_id = str(row["principal_id"])
        approval_id = str(row["approval_id"])
        continuation = _from_rows(transaction, principal_id, row, tuple(calls_by_approval[approval_id]))
        owners.append((principal_id, continuation))
    return tuple(owners)


def all_owners(
    transaction: Transaction,
    *,
    limit: int,
    after: str | None = None,
) -> tuple[tuple[str, ApprovalContinuation], ...]:
    """Return one bounded page of every principal's continuations, ordered by approval id after ``after``."""
    cursor_clause = "" if after is None else " WHERE approval_id/*bytes*/ > ?"
    cursor_params: tuple[object, ...] = () if after is None else (after,)
    rows = transaction.fetchall(
        f"""
        SELECT principal_id, {_CONTINUATION_COLUMNS} FROM approval_continuations
        {cursor_clause}
        ORDER BY approval_id/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - a fixed cursor clause, not input
        (*cursor_params, limit),
    )
    return _load_owners(transaction, rows)


def claim(transaction: Transaction, principal_id: str, *, approval_id: str, span_id: str) -> bool:
    """Name the span that runs a ready continuation; only one span claims each generation."""
    claimed = transaction.fetchone(
        """
        UPDATE approval_continuations SET claim_span_id = ?
        WHERE principal_id = ? AND approval_id = ? AND state = 'ready' AND claim_span_id IS NULL
        RETURNING approval_id
        """,
        (span_id, principal_id, approval_id),
    )
    return claimed is not None


@dataclass(frozen=True, slots=True)
class ApprovalAdvance:
    """One claimed generation's next exact pause."""

    approval_id: str
    claimant_generation: int
    run_id: str
    session_id: str
    calls: tuple[ApprovalCall, ...]
    runtime_model_name: str | None = None
    delegation_storage_bindings: dict[str, dict[str, object]] | None = None
    cli_call: dict[str, object] | None = None
    continuation_count: int | None = None

    def apply(self, transaction: Transaction, principal_id: str) -> ApprovalContinuation | None:
        """Replace the claimed generation with this next exact pause."""
        current = get(transaction, principal_id, approval_id=self.approval_id)
        if current is None:
            return None
        next_generation = self.claimant_generation + 1
        # Every chained generation stays fenced until its ordered Matrix edit and
        # any cards are published. Even an automatically decided generation must
        # not become executable in the persist-before-ack crash window.
        state: ApprovalContinuationState = "waiting"
        publication_owner = current.runtime_generation
        advanced = replace(
            current,
            run_id=self.run_id,
            session_id=self.session_id,
            calls=self.calls,
            runtime_model_name=self.runtime_model_name or current.runtime_model_name,
            continuation_count=current.continuation_count
            if self.continuation_count is None
            else self.continuation_count,
            delegation_storage_bindings=(
                current.delegation_storage_bindings
                if self.delegation_storage_bindings is None
                else self.delegation_storage_bindings
            ),
            state=state,
            runtime_generation=publication_owner,
            failure=None,
            generation=next_generation,
            cli_call=self.cli_call,
        )
        updated = transaction.fetchone(
            """
            UPDATE approval_continuations
            SET state = ?, generation = ?, runtime_generation = ?, claim_span_id = NULL,
                failure_kind = NULL, failure_detail = NULL, context_json = ?
            WHERE principal_id = ? AND approval_id = ?
              AND state = 'ready' AND claim_span_id IS NOT NULL AND generation = ?
            RETURNING approval_id
            """,
            (
                state,
                next_generation,
                publication_owner,
                _json(_context(advanced)),
                principal_id,
                self.approval_id,
                self.claimant_generation,
            ),
        )
        if updated is None:
            return None
        approval_calls.insert_reply_generation(
            transaction,
            principal_id,
            approval_id=self.approval_id,
            generation=next_generation,
            calls=self.calls,
        )
        return get(transaction, principal_id, approval_id=self.approval_id)


def activate(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    expected_generation: int,
) -> ApprovalContinuation | None:
    """Release one publication lease and make its generation decidable or executable."""
    undecided = approval_calls.undecided_tool_call_ids(
        transaction,
        approval_id=approval_id,
        generation=expected_generation,
    )
    state: Literal["waiting", "ready"] = "waiting" if undecided else "ready"
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


def _answer_frozen(transaction: Transaction, principal_id: str, continuation: ApprovalContinuation) -> bool:
    """Return whether the run's answer is a FINAL the outbox still delivers, which a failure must not replace."""
    final = outbox.load(
        transaction,
        principal_id,
        delivery_id=continuation.final_delivery_id,
        stage=DeliveryStage.FINAL,
    )
    return final is not None and final.permanent_failure_reason is None


def request_failure(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    failure: ApprovalFailure,
    expected_state: ApprovalContinuationState,
    expected_generation: int,
    expected_runtime_generation: str | None,
) -> ApprovalContinuation | None:
    """Fence one observed continuation state against any later execution.

    A claim's bot instance is its span's, so the observed claim is compared
    here and the row's stored state in the update.
    """
    current = get(transaction, principal_id, approval_id=approval_id)
    if (
        current is None
        or (current.state, current.runtime_generation) != (expected_state, expected_runtime_generation)
        or _answer_frozen(transaction, principal_id, current)
    ):
        return None
    claimed = expected_state == "claimed"
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_kind = ?, failure_detail = ?
        WHERE principal_id = ? AND approval_id = ? AND state = ? AND generation = ?
          AND runtime_generation IS NOT DISTINCT FROM ? AND claim_span_id IS NOT DISTINCT FROM ?
        RETURNING approval_id
        """,
        (
            failure.kind.value,
            failure.detail,
            principal_id,
            approval_id,
            "ready" if claimed else expected_state,
            expected_generation,
            None if claimed else expected_runtime_generation,
            current.claim_span_id,
        ),
    )
    return None if updated is None else get(transaction, principal_id, approval_id=approval_id)


def fence(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    failure: ApprovalFailure,
) -> ApprovalContinuation | None:
    """Fence a continuation for failure on behalf of its reply, in whatever state it holds.

    A Stop on a paused reply fences the approval in its own transaction; a
    frozen successful FINAL still wins.
    """
    current = get(transaction, principal_id, approval_id=approval_id)
    if current is None or _answer_frozen(transaction, principal_id, current):
        return None
    updated = transaction.fetchone(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_kind = ?, failure_detail = ?
        WHERE principal_id = ? AND approval_id = ? AND state IN ('waiting', 'ready')
        RETURNING approval_id
        """,
        (failure.kind.value, failure.detail, principal_id, approval_id),
    )
    return None if updated is None else get(transaction, principal_id, approval_id=approval_id)


def may_finish(transaction: Transaction, principal_id: str, *, approval_id: str) -> ApprovalContinuation | None:
    """Lock a paused run that may end: its FINAL was acknowledged or refused for good.

    A failed run whose reply went with its deleted message ends without one,
    since nothing is left to show it. The caller applies the finish to the
    reply it paused while the run still holds that reply, then deletes the run
    with ``delete``.
    """
    continuation = get_locked(transaction, principal_id, approval_id=approval_id)
    if continuation is None:
        return None
    if continuation.state == "failing" and _paused_reply_gone(transaction, principal_id, continuation):
        return continuation
    delivered = transaction.fetchone(
        """
        SELECT 1 AS present FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
          AND (acknowledged_event_id IS NOT NULL OR permanent_failure_reason IS NOT NULL)
        """,
        (principal_id, continuation.final_delivery_id, DeliveryStage.FINAL.value),
    )
    return None if delivered is None else continuation


def _paused_reply_gone(transaction: Transaction, principal_id: str, continuation: ApprovalContinuation) -> bool:
    """Return whether the reply a continuation paused is gone."""
    assert continuation.span_id is not None, "a stored continuation names the span whose pause created it"
    span = reply_spans.load(transaction, principal_id, continuation.span_id)
    reply = None if span is None else reply_messages.load(transaction, principal_id, span.reply_id)
    return reply is not None and reply.state is ReplyState.GONE


def delete(transaction: Transaction, principal_id: str, *, approval_id: str) -> None:
    """Delete a paused run whose end the reply it paused already applied, with the calls no live card shows."""
    approval_calls.delete_reply_calls(transaction, approval_id=approval_id)
    transaction.execute(
        "DELETE FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    approval_signals.ended(transaction, principal_id, approval_id)


def may_release(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    expected_generation: int,
) -> ApprovalContinuation | None:
    """Lock an interrupted continuation that may be released before any FINAL.

    A restart cut the approved run short before any FINAL, so its reply is still
    the unfinished stream of one turn. Replay adopts and continues it like any
    reply a restart left streaming, unless a Stop the run left unapplied ends it
    (``rl.approval_released``). The failure fence has already stopped execution
    and the caller has ended the cards.
    """
    continuation = get_locked(transaction, principal_id, approval_id=approval_id)
    if continuation is None or continuation.state != "failing" or continuation.generation != expected_generation:
        return None
    if outbox.load(transaction, principal_id, delivery_id=continuation.final_delivery_id, stage=DeliveryStage.FINAL):
        return None
    return continuation


def may_discard_unavailable(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    notice_principal_id: str,
) -> ApprovalContinuation | None:
    """Lock a permanently unavailable owner's paused run whose visible card cleanup is done, so it may end."""
    observed = get(transaction, principal_id, approval_id=approval_id)
    if observed is None or observed.state != "failing":
        return None
    membership_epoch = membership_state.claim_active_membership_epoch(
        transaction,
        notice_principal_id,
        room_id=observed.room_id,
    )
    if membership_epoch is None:
        return None
    continuation = get_locked(transaction, principal_id, approval_id=approval_id)
    if continuation is None or continuation.state != "failing" or continuation.room_id != observed.room_id:
        return None
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
        return None
    delivered = transaction.fetchone(
        """
        SELECT 1 AS present FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
          AND acknowledged_event_id IS NOT NULL
        """,
        (notice_principal_id, delivery_id, DeliveryStage.FINAL.value),
    )
    return None if delivered is None else continuation


def get_locked(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
) -> ApprovalContinuation | None:
    """Lock one paused reply against concurrent writers and read it."""
    transaction.execute(
        """
        UPDATE approval_continuations SET state = state
        WHERE principal_id = ? AND approval_id = ?
        """,
        (principal_id, approval_id),
    )
    return get(transaction, principal_id, approval_id=approval_id)
