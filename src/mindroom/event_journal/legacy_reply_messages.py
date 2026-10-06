"""Replies main started before durable reply records, adopted once per principal (DESIGN.md §14.5).

Main kept what an in-flight reply was in four places: its turn record, its
``INITIAL`` and ``FINAL`` outbox rows, its approval continuation, and the
Matrix event itself. The first start with reply records reads the first three
here, from the database only, and gives every reply that still has work a
record; what only Matrix knows is read after the room syncs
(``legacy_pending``) by ``mindroom.legacy_reply_messages``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.handled_turns import TurnRecordCodec

from . import approval_continuations, journal, outbox, reply_messages, reply_spans, turn_records
from .models import DeliveryStage
from .replies import AppliedTransition, apply, row_facts

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.turn_record import TurnRecord

    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction
    from .models import MatrixDelivery

# The generation of spans main ran: never active, so nothing they left is live.
LEGACY_GENERATION = "legacy"

# What the wire status of a main-era answer says its reply ended as.
_STATE_BY_STATUS = {
    "completed": rl.ReplyState.COMPLETED,
    "cancelled": rl.ReplyState.CANCELLED,
    "error": rl.ReplyState.FAILED,
    "interrupted": rl.ReplyState.FAILED,
}
_OUTCOME_BY_STATE = {
    rl.ReplyState.COMPLETED: rl.SpanOutcome.COMPLETED,
    rl.ReplyState.CANCELLED: rl.SpanOutcome.CANCELLED,
    rl.ReplyState.FAILED: rl.SpanOutcome.FAILED,
}


@dataclass(frozen=True, slots=True)
class LegacyPresentations:
    """Encoders of what main showed, owned by the reply layer that defines presentations."""

    # The placeholder alone, for a team or an agent reply.
    empty: Callable[[bool], str]
    # What a paused approval showed, from its continuation, as the named span's answer.
    paused: Callable[[ApprovalContinuation, str], str]
    # What a frozen main-era answer row shows, as the named span's answer.
    answered: Callable[[MatrixDelivery, str], str]


@dataclass(frozen=True, slots=True)
class _Adoption:
    """The reply records one main-era reply gets."""

    reply: rl.Reply
    spans: tuple[rl.Span, ...]
    # Main's row the reply now owns, given reply identity so its acknowledgement binds the reply.
    row: MatrixDelivery | None = None
    row_placeholder_only: bool = False


def classified(transaction: Transaction, principal_id: str) -> bool:
    """Return whether this principal's main-era replies were adopted already."""
    row = transaction.fetchone(
        "SELECT 1 AS present FROM reply_legacy_classifications WHERE principal_id = ?",
        (principal_id,),
    )
    return row is not None


# LEGACY_COMPAT: In-flight agent and team replies without reply records.
# Legacy format: approval continuations, INITIAL and FINAL outbox rows without reply_id, and pending turn records that
# main wrote for replies before reply_messages existed; the selected principal has no reply_legacy_classifications row.
# Last legacy release: v2026.10.162; replacement: the unreleased durable reply messages record every reply in
# reply_messages and reply_spans and give its outbox rows reply identity.
# Handling: once per principal at bot start, before owner_lost, records are created from the database only: a paused
# reply per newest continuation (older ones are superseded), the state a frozen unacknowledged FINAL implies, a lost
# span for an INITIAL whose sources are pending or whose stream may need a restart note, and an adoption scan for a
# pending turn whose stream created its reply directly; an unsettled Stop is applied to the reply it names. What only
# Matrix knows is marked legacy_pending and read after the room syncs.
# Coverage: tests/test_legacy_reply_messages.py.
def classify(
    transaction: Transaction,
    principal_id: str,
    *,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> tuple[AppliedTransition, ...]:
    """Give each reply main left in flight a record, once per principal; return what to run after commit."""
    if classified(transaction, principal_id):
        return ()
    adoptions: list[_Adoption] = []
    adopted: set[str] = set()
    for continuation in _newest_continuations(transaction, principal_id, entity_name):
        adoption = _paused_reply(transaction, principal_id, continuation, entity_name, presentations, now_ns)
        adoptions.append(adoption)
        adopted.update(span.delivery_id for span in adoption.spans)
    for delivery_id in _unowned_row_delivery_ids(transaction, principal_id):
        if delivery_id in adopted:
            continue
        adoption = _reply_of_rows(transaction, principal_id, delivery_id, entity_name, presentations, now_ns)
        if adoption is not None:
            adoptions.append(adoption)
            adopted.add(delivery_id)
    adoptions.extend(
        _stream_created_reply(transaction, principal_id, record, entity_name, presentations, now_ns)
        for record in _pending_turns(transaction, principal_id, entity_name)
        if record.source_event_ids[0] not in adopted
    )
    applied = [_write(transaction, principal_id, adoption) for adoption in adoptions]
    applied.extend(_unsettled_stops(transaction, principal_id, entity_name, now_ns))
    transaction.execute(
        "INSERT INTO reply_legacy_classifications (principal_id, classified_at_ns) VALUES (?, ?)",
        (principal_id, now_ns),
    )
    return tuple(applied)


def _new_id() -> str:
    return uuid4().hex


def _write(transaction: Transaction, principal_id: str, adoption: _Adoption) -> AppliedTransition:
    applied = apply(
        transaction,
        principal_id,
        rl.Transition(outcome=rl.Outcome.APPLIED, reply=adoption.reply, spans=adoption.spans),
    )
    row = adoption.row
    if row is not None:
        # The reply's first write: its acknowledgement binds the reply, a permanent refusal fails it.
        transaction.execute(
            """
            UPDATE matrix_delivery_outbox SET reply_id = ?, span_id = ?, reply_sequence = 1, reply_row_json = ?
            WHERE principal_id = ? AND delivery_id = ? AND stage = ? AND reply_id IS NULL
            """,
            (
                adoption.reply.reply_id,
                adoption.reply.last_span_id,
                json.dumps(row_facts(placeholder_only=adoption.row_placeholder_only, new_text=None)),
                principal_id,
                row.delivery_id,
                row.stage.value,
            ),
        )
    return applied


def _newest_continuations(
    transaction: Transaction,
    principal_id: str,
    entity_name: str,
) -> tuple[ApprovalContinuation, ...]:
    """Return the newest continuation of each main-era reply; older ones are superseded (decision 1)."""
    newest: dict[str, ApprovalContinuation] = {}
    for continuation in approval_continuations.for_principal(transaction, principal_id):
        if continuation.entity_name != entity_name:
            continue
        if reply_messages.for_event(transaction, principal_id, continuation.response_event_id) is not None:
            continue
        older = newest.get(continuation.response_event_id)
        if older is not None:
            approval_continuations.fence(
                transaction,
                principal_id,
                approval_id=older.approval_id,
                reason=approval_continuations.SUPERSEDED_FAILURE_REASON,
            )
        newest[continuation.response_event_id] = continuation
    return tuple(newest.values())


def _reply(
    transaction: Transaction,
    principal_id: str,
    *,
    entity_name: str,
    room_id: str,
    thread_id: str | None,
    requester_id: str,
    state: rl.ReplyState,
    span: rl.Span,
    presentation: str,
    now_ns: int,
    membership_epoch: int | None = None,
    **changes: object,
) -> rl.Reply:
    reply = rl.Reply(
        reply_id=span.reply_id,
        entity_name=entity_name,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=(
            journal.current_membership_epoch(transaction, principal_id, room_id)
            if membership_epoch is None
            else membership_epoch
        ),
        requester_id=requester_id,
        visibility_policy=rl.VisibilityPolicy.NORMAL,
        state=state,
        last_span_id=span.span_id,
        presentation=presentation,
        revision=1,
        reply_sequence=0,
        created_at_ns=now_ns,
        updated_at_ns=now_ns,
    )
    return replace(reply, **changes)  # type: ignore[arg-type]


def _span(
    reply_id: str,
    *,
    kind: rl.SpanKind,
    delivery_id: str,
    sources: rl.SpanSources,
    now_ns: int,
    outcome: rl.SpanOutcome | None,
    approval_id: str | None = None,
    approval_generation: int | None = None,
) -> rl.Span:
    return rl.Span(
        span_id=_new_id(),
        reply_id=reply_id,
        kind=kind,
        delivery_id=delivery_id,
        sources=sources,
        bot_generation=LEGACY_GENERATION,
        claimed_at_ns=now_ns,
        base_sequence=0,
        approval_id=approval_id,
        approval_generation=approval_generation,
        outcome=outcome,
        ended_at_ns=None if outcome is None else now_ns,
    )


def _final_state(final: MatrixDelivery) -> rl.ReplyState:
    content = final.payload.get("m.new_content", final.payload)
    status = content.get("io.mindroom.stream_status") if isinstance(content, dict) else None
    return _STATE_BY_STATUS.get(str(status), rl.ReplyState.COMPLETED)


def _owed_final(final: MatrixDelivery | None) -> bool:
    """Return whether a main-era FINAL is still to be delivered."""
    return (
        final is not None
        and final.acknowledged_event_id is None
        and not final.retired
        and final.permanent_failure_reason is None
    )


def _paused_reply(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> _Adoption:
    """A continuation pauses its reply; a claimed one has its resume left running by the instance that stopped."""
    reply_id = _new_id()
    sources = continuation.sources
    span_sources = rl.SpanSources(
        pending=sources.pending_event_ids,
        logical=sources.logical_source_event_ids,
        discovery=sources.discovery_event_ids,
    )
    delivery_id = continuation.source_event_ids[0]
    paused = _span(
        reply_id,
        kind=rl.SpanKind.REGENERATION if continuation.prepared_edit_record is not None else rl.SpanKind.TURN,
        delivery_id=delivery_id,
        sources=span_sources,
        now_ns=now_ns,
        outcome=rl.SpanOutcome.PAUSED,
        approval_id=continuation.approval_id,
    )
    shown = presentations.paused(continuation, paused.span_id)
    reply = _reply(
        transaction,
        principal_id,
        entity_name=entity_name,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        requester_id=continuation.requester_id,
        state=rl.ReplyState.PAUSED,
        span=paused,
        presentation=shown,
        now_ns=now_ns,
        event_id=continuation.response_event_id,
        approval_id=continuation.approval_id,
        # A ready or claimed approval may have resumed past its pause, which only Matrix shows.
        legacy_pending=rl.LegacyPending.PRESENTATION_READ if continuation.state in {"ready", "claimed"} else None,
    )
    if continuation.state != "claimed":
        return _Adoption(reply=reply, spans=(paused,))
    final = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=DeliveryStage.FINAL)
    resume = _span(
        reply_id,
        kind=rl.SpanKind.APPROVAL_RESUME,
        delivery_id=delivery_id,
        sources=span_sources,
        # Claimed after the pause it resumes.
        now_ns=now_ns + 1,
        outcome=None,
        approval_id=continuation.approval_id,
        approval_generation=continuation.generation,
    )
    if final is not None and (final.acknowledged_event_id is not None or _owed_final(final)):
        # The resume froze its answer: the reply ends as that row says, and
        # main's frozen-final recovery still finishes the continuation.
        state = _final_state(final)
        ended = replace(resume, outcome=_OUTCOME_BY_STATE[state], ended_at_ns=now_ns)
        shown = presentations.answered(final, ended.span_id)
        owed = _owed_final(final)
        answered = replace(
            reply,
            state=state,
            last_span_id=ended.span_id,
            presentation=shown,
            possibly_shown=shown,
            possibly_shown_seq=1 if owed else None,
            reply_sequence=1 if owed else 0,
            approval_id=None,
            legacy_pending=None,
        )
        return _Adoption(reply=answered, spans=(paused, ended), row=final if owed else None)
    # Main's approval recovery owns a resume a stopped instance left running.
    running = replace(reply, state=rl.ReplyState.ACTIVE, current_span_id=resume.span_id, last_span_id=resume.span_id)
    return _Adoption(reply=running, spans=(paused, resume))


def _unowned_row_delivery_ids(transaction: Transaction, principal_id: str) -> tuple[str, ...]:
    rows = transaction.fetchall(
        """
        SELECT DISTINCT delivery_id FROM matrix_delivery_outbox
        WHERE principal_id = ? AND reply_id IS NULL AND stage IN ('initial', 'final')
        ORDER BY delivery_id
        """,
        (principal_id,),
    )
    return tuple(str(row["delivery_id"]) for row in rows)


def _turn(transaction: Transaction, entity_name: str, event_id: str) -> TurnRecord | None:
    """Return the agent or team turn an event belongs to; command and echo turns have no history scope."""
    record = turn_records.load_record(transaction, entity_name, event_id)
    return None if record is None or record.history_scope is None else record


def _any_pending(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> bool:
    return any(journal.is_pending(transaction, principal_id, event_id) for event_id in event_ids)


def _turn_sources(transaction: Transaction, principal_id: str, record: TurnRecord) -> rl.SpanSources:
    return rl.SpanSources(
        pending=tuple(
            event_id for event_id in record.source_event_ids if journal.is_pending(transaction, principal_id, event_id)
        ),
        logical=record.source_event_ids,
        discovery=record.discovery_event_ids,
    )


def _reply_of_rows(
    transaction: Transaction,
    principal_id: str,
    delivery_id: str,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> _Adoption | None:
    """The reply main's INITIAL and FINAL rows of one turn imply, first match wins (§14.5)."""
    record = _turn(transaction, entity_name, delivery_id)
    if record is None or record.conversation_target is None:
        return None
    initial = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=DeliveryStage.INITIAL)
    final = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=DeliveryStage.FINAL)
    reply_id = _new_id()
    sources = _turn_sources(transaction, principal_id, record)
    target = record.conversation_target
    team = record.history_scope is not None and record.history_scope.kind == "team"
    base = {
        "entity_name": entity_name,
        "room_id": target.room_id,
        "thread_id": target.resolved_thread_id,
        "requester_id": record.requester_id or "",
        "now_ns": now_ns,
    }
    if final is not None and _owed_final(final):
        state = _final_state(final)
        span = _span(
            reply_id,
            kind=rl.SpanKind.TURN,
            delivery_id=delivery_id,
            sources=sources,
            now_ns=now_ns,
            outcome=_OUTCOME_BY_STATE[state],
        )
        shown = presentations.answered(final, span.span_id)
        reply = _reply(
            transaction,
            principal_id,
            state=state,
            span=span,
            presentation=shown,
            membership_epoch=final.membership_epoch,
            event_id=final.edits_event_id,
            possibly_shown=shown,
            possibly_shown_seq=1,
            reply_sequence=1,
            **base,  # type: ignore[arg-type]
        )
        return _Adoption(reply=reply, spans=(span,), row=final)
    if initial is None:
        return None
    if final is not None or initial.retired:
        # Answered, or retired by a departure or a deleted source: main's own owners finish it.
        return None
    stopped = record.user_stop_receipt_order is not None
    if not sources.pending and (initial.acknowledged_event_id is None or stopped):
        return None
    span = _span(
        reply_id,
        kind=rl.SpanKind.TURN,
        delivery_id=delivery_id,
        sources=sources,
        now_ns=now_ns,
        outcome=rl.SpanOutcome.LOST,
    )
    acknowledged = initial.acknowledged_event_id
    owed = acknowledged is None and initial.permanent_failure_reason is None
    reply = _reply(
        transaction,
        principal_id,
        state=rl.ReplyState.ACTIVE,
        span=span,
        presentation=presentations.empty(team),
        membership_epoch=initial.membership_epoch,
        event_id=acknowledged,
        placeholder_only=acknowledged is not None,
        reply_sequence=1 if owed else 0,
        # Whether a replay continues below a shown attempt, or a stream that
        # settled needs main's restart note, only the event says.
        legacy_pending=rl.LegacyPending.PRESENTATION_READ if acknowledged is not None else None,
        **base,  # type: ignore[arg-type]
    )
    return _Adoption(reply=reply, spans=(span,), row=initial if owed else None, row_placeholder_only=True)


def _pending_turns(transaction: Transaction, principal_id: str, entity_name: str) -> tuple[TurnRecord, ...]:
    """Return incomplete agent and team turns with pending sources and no outbox row of their own."""
    turns: dict[str, TurnRecord] = {}
    for index_event_id, _anchor, record_json in turn_records.load_all(transaction, entity_name):
        record = TurnRecordCodec._from_ledger_record(index_event_id, json.loads(record_json))
        if (
            record is None
            or record.completed
            or record.history_scope is None
            or record.conversation_target is None
            or not record.source_event_ids
            or record.anchor_event_id in turns
        ):
            continue
        turn_id = record.source_event_ids[0]
        if not _any_pending(transaction, principal_id, record.source_event_ids):
            continue
        if outbox.load(transaction, principal_id, delivery_id=turn_id, stage=DeliveryStage.INITIAL) is not None:
            continue
        if outbox.load(transaction, principal_id, delivery_id=turn_id, stage=DeliveryStage.FINAL) is not None:
            continue
        if reply_spans.reply_ids_for_sources(transaction, principal_id, record.source_event_ids):
            continue
        assert record.anchor_event_id is not None
        turns[record.anchor_event_id] = record
    return tuple(turns.values())


def _stream_created_reply(
    transaction: Transaction,
    principal_id: str,
    record: TurnRecord,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> _Adoption:
    """A turn whose stream created its reply without a placeholder row: main's adoption scan finds the event."""
    assert record.conversation_target is not None
    reply_id = _new_id()
    span = _span(
        reply_id,
        kind=rl.SpanKind.TURN,
        delivery_id=record.source_event_ids[0],
        sources=_turn_sources(transaction, principal_id, record),
        now_ns=now_ns,
        outcome=rl.SpanOutcome.LOST,
    )
    team = record.history_scope is not None and record.history_scope.kind == "team"
    reply = _reply(
        transaction,
        principal_id,
        entity_name=entity_name,
        room_id=record.conversation_target.room_id,
        thread_id=record.conversation_target.resolved_thread_id,
        requester_id=record.requester_id or "",
        state=rl.ReplyState.ACTIVE,
        span=span,
        presentation=presentations.empty(team),
        now_ns=now_ns,
        legacy_pending=rl.LegacyPending.ADOPTION_SCAN,
    )
    return _Adoption(reply=reply, spans=(span,))


def _unsettled_stops(
    transaction: Transaction,
    principal_id: str,
    entity_name: str,
    now_ns: int,
) -> tuple[AppliedTransition, ...]:
    """Apply each Stop a turn recorded but main never settled to the reply it names."""
    applied: list[AppliedTransition] = []
    seen: set[str] = set()
    for index_event_id, _anchor, record_json in turn_records.load_all(transaction, entity_name):
        record = TurnRecordCodec._from_ledger_record(index_event_id, json.loads(record_json))
        if record is None or record.response_event_id is None or record.response_event_id in seen:
            continue
        stop_order = record.user_stop_receipt_order
        if stop_order is None or (record.user_stop_settled_receipt_order or 0) >= stop_order:
            continue
        seen.add(record.response_event_id)
        found = reply_messages.for_event(transaction, principal_id, record.response_event_id)
        if found is None or found.stop_receipt_order is not None:
            continue
        reply = reply_messages.lock(transaction, principal_id, found.reply_id)
        span = reply_spans.load(transaction, principal_id, found.current_span_id or found.last_span_id)
        assert reply is not None
        facts = rl.StopFacts(
            receipt_order=stop_order,
            newer_edit=(record.latest_edit_receipt_order or 0) > stop_order,
            span_live=False,
        )
        applied.append(apply(transaction, principal_id, rl.stop(reply, span, facts, now_ns=now_ns)))
    return tuple(applied)


def pending_reads(transaction: Transaction, principal_id: str) -> tuple[tuple[rl.Reply, rl.Span], ...]:
    """Return main-era replies still waiting for their legacy read, with their last span."""
    rows = transaction.fetchall(
        "SELECT reply_id FROM reply_messages WHERE principal_id = ? AND legacy_pending IS NOT NULL ORDER BY reply_id",
        (principal_id,),
    )
    pending = []
    for row in rows:
        reply = reply_messages.load(transaction, principal_id, str(row["reply_id"]))
        assert reply is not None
        last = reply_spans.load(transaction, principal_id, reply.last_span_id)
        assert last is not None
        pending.append((reply, last))
    return tuple(pending)


def read_done(
    transaction: Transaction,
    principal_id: str,
    *,
    reply_id: str,
    read: rl.LegacyRead,
    now_ns: int,
) -> AppliedTransition | None:
    """Record what one main-era reply's legacy read found (DESIGN.md §14.5)."""
    reply = reply_messages.lock(transaction, principal_id, reply_id)
    if reply is None:
        return None
    last = reply_spans.load(transaction, principal_id, reply.last_span_id)
    assert last is not None
    if read.event_id is not None:
        owner = reply_messages.for_event(transaction, principal_id, read.event_id)
        if owner is not None and owner.reply_id != reply_id:
            # Another reply already owns that event; this one creates its own.
            read = rl.LegacyRead()
    return apply(
        transaction,
        principal_id,
        rl.legacy_read_done(
            reply,
            last,
            read,
            sources_pending=_any_pending(transaction, principal_id, last.sources.pending),
            now_ns=now_ns,
        ),
    )
