"""Replies an earlier release left paused for approval, adopted once per principal.

An upgrade runs while no reply is in flight, so what an earlier release can
leave for reply records is an approval continuation waiting for its decision,
or one a newer edit's delivered answer replaced. The first start with reply
records gives each such reply a record, from the database only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom import reply_lifecycle as rl

from . import approval_continuations, journal, outbox, reply_messages, turn_records
from .models import SUPERSEDED_FAILURE_REASON, DeliveryStage
from .replies import AppliedTransition, apply

if TYPE_CHECKING:
    from collections.abc import Callable

    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction

# The generation of spans an earlier release ran: never active, so nothing they left is live.
_LEGACY_GENERATION = "legacy"


@dataclass(frozen=True, slots=True)
class LegacyPausedAnswer:
    """What an earlier release's approval continuation stored of the answer its pause showed."""

    text: str = ""
    tool_trace: tuple[dict[str, object], ...] = ()
    team_state: dict[str, object] | None = None


# LEGACY_COMPAT: Paused answers stored in approval continuation context.
# Legacy format: approval_continuations.context_json carrying response_text, response_tool_trace, and
# response_presentation_state, which earlier releases wrote at every pause before replies held what a pause shows.
# Last legacy release: v2026.10.201; replacement: the unreleased durable reply messages keep the paused answer as
# the reply's answer segment and no longer write these keys.
# Handling: adoption reads them once, from the stored context of a continuation it adopts, to build the paused
# reply's presentation; nothing else reads them, and an advance rewrites the context without them.
# Coverage: tests/test_legacy_reply_messages.py::test_a_waiting_approval_pauses_its_reply_with_what_it_showed.
def _legacy_paused_answer(transaction: Transaction, principal_id: str, approval_id: str) -> LegacyPausedAnswer:
    row = transaction.fetchone(
        "SELECT context_json FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    stored = {} if row is None else json.loads(str(row["context_json"]))
    text = stored.get("response_text")
    trace = stored.get("response_tool_trace")
    state = stored.get("response_presentation_state")
    return LegacyPausedAnswer(
        text=text if isinstance(text, str) else "",
        tool_trace=tuple(dict(entry) for entry in trace if isinstance(entry, dict)) if isinstance(trace, list) else (),
        team_state=dict(state) if isinstance(state, dict) and state else None,
    )


@dataclass(frozen=True, slots=True)
class LegacyPresentations:
    """Encoders of what an earlier release showed, owned by the reply layer that defines presentations."""

    # The placeholder alone, for a team or an agent reply.
    empty: Callable[[bool], str]
    # What a paused approval showed, from what its continuation stored, as the named span's answer.
    paused: Callable[[ApprovalContinuation, LegacyPausedAnswer, str], str]


@dataclass(frozen=True, slots=True)
class _Adoption:
    """The reply records one earlier-release reply gets."""

    reply: rl.Reply
    spans: tuple[rl.Span, ...]
    # The continuation whose pause the reply's first span is.
    approval_id: str | None = None
    # The pauses of older continuations of the same reply, which a newer one superseded.
    superseded: tuple[rl.Span, ...] = ()
    # What the adoption decided in its transaction: the cleanup of approvals a newer answer replaced.
    effects: tuple[rl.Effect, ...] = ()


def _classified(transaction: Transaction, principal_id: str) -> bool:
    """Return whether this principal's earlier-release replies were adopted already."""
    row = transaction.fetchone(
        "SELECT 1 AS present FROM reply_legacy_classifications WHERE principal_id = ?",
        (principal_id,),
    )
    return row is not None


# LEGACY_COMPAT: Approval-paused agent and team replies without reply records.
# Legacy format: approval continuations an earlier release left before reply_messages existed, for a principal with no
# reply_legacy_classifications row.
# Last legacy release: v2026.10.201; replacement: the unreleased durable reply messages record every reply in
# reply_messages and reply_spans.
# Handling: once per principal at bot start, before owner_lost, the newest continuation of each reply pauses it; older
# ones are superseded and keep their pauses on that reply to hold their sources until their cleanup settles them.
# A failing newest one whose failure note Matrix already took or refused ends its reply as its cleanup will, and holds
# it until that cleanup settles its sources, so an edit waits for the cleanup as one after a current failure note does.
# Approvals a newer edit's delivered answer replaced keep their pauses on that answer's reply, which is finished, and
# their cleanup runs. An upgrade runs while no reply is in flight, so nothing else is adopted.
# Coverage: tests/test_legacy_reply_messages.py.
def classify(
    transaction: Transaction,
    principal_id: str,
    *,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> tuple[AppliedTransition, ...]:
    """Give each reply an earlier release left paused a record, once per principal; return what to run after commit."""
    if _classified(transaction, principal_id):
        return ()
    adoptions: list[_Adoption] = []
    for event_id, continuation, superseded in _newest_continuations(transaction, principal_id, entity_name):
        if continuation is None:
            adoptions.append(
                _replaced_reply(transaction, principal_id, event_id, superseded, entity_name, presentations, now_ns),
            )
            continue
        adoption = _paused_reply(transaction, principal_id, continuation, entity_name, presentations, now_ns)
        adoptions.append(replace(adoption, superseded=_superseded_pauses(adoption.reply.reply_id, superseded, now_ns)))
    applied = [_write(transaction, principal_id, adoption) for adoption in adoptions]
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
        rl.Transition(
            outcome=rl.Outcome.APPLIED,
            reply=adoption.reply,
            spans=(*adoption.superseded, *adoption.spans),
            effects=adoption.effects,
        ),
    )
    for span in adoption.superseded:
        # A superseded continuation holds its sources through its pause, until its cleanup settles them.
        transaction.execute(
            "UPDATE approval_continuations SET span_id = ? WHERE principal_id = ? AND approval_id = ? AND span_id IS NULL",
            (span.span_id, principal_id, span.approval_id),
        )
    if adoption.approval_id is not None:
        # The continuation names the span that paused its reply, as one paused now does.
        transaction.execute(
            "UPDATE approval_continuations SET span_id = ? WHERE principal_id = ? AND approval_id = ? AND span_id IS NULL",
            (adoption.spans[0].span_id, principal_id, adoption.approval_id),
        )
    return applied


def _newest_continuations(
    transaction: Transaction,
    principal_id: str,
    entity_name: str,
) -> tuple[tuple[str, ApprovalContinuation | None, tuple[ApprovalContinuation, ...]], ...]:
    """Return each earlier-release reply's event, its newest live continuation, and the ones superseded on it.

    A newer continuation supersedes older ones, as an edit does. A reply
    whose newest answer replaced every continuation has no live one.
    """
    newest: dict[str, ApprovalContinuation] = {}
    superseded: dict[str, list[ApprovalContinuation]] = {}
    for continuation in approval_continuations.for_principal(transaction, principal_id):
        if continuation.entity_name != entity_name:
            continue
        event_id = continuation.response_event_id
        if reply_messages.for_event(transaction, principal_id, event_id) is not None:
            continue
        if continuation.state == "failing" and continuation.failure_reason == SUPERSEDED_FAILURE_REASON:
            superseded.setdefault(event_id, []).append(continuation)
            continue
        older = newest.get(event_id)
        if older is not None:
            # The newer pause is what the reply shows, even over an answer the older one delivered.
            transaction.execute(
                "UPDATE approval_continuations SET state = 'failing', failure_reason = ? "
                "WHERE principal_id = ? AND approval_id = ?",
                (SUPERSEDED_FAILURE_REASON, principal_id, older.approval_id),
            )
            superseded.setdefault(event_id, []).append(older)
        newest[event_id] = continuation
    return tuple(
        (event_id, newest.get(event_id), tuple(superseded.get(event_id, ())))
        for event_id in dict.fromkeys((*newest, *superseded))
    )


def _superseded_pauses(
    reply_id: str,
    superseded: tuple[ApprovalContinuation, ...],
    now_ns: int,
) -> tuple[rl.Span, ...]:
    # Paused before whatever superseded them.
    return tuple(_pause_span(reply_id, older, rl.SpanOutcome.SUPERSEDED, now_ns - 1) for older in superseded)


def _with_replaced_approvals(
    adoption: _Adoption,
    superseded: tuple[ApprovalContinuation, ...],
    pauses: tuple[rl.Span, ...],
) -> _Adoption:
    """Keep the pauses of approvals a newer answer replaced on that answer's reply, and run their cleanup."""
    return replace(
        adoption,
        superseded=pauses,
        effects=(*adoption.effects, *(rl.WakeApproval(older.approval_id) for older in superseded)),
    )


def _replaced_reply(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
    superseded: tuple[ApprovalContinuation, ...],
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> _Adoption:
    """The answer a newer edit delivered stands: a finished reply, keeping the replaced approvals' pauses.

    Its own span is that answer's, as for any answer older than the reply
    records, so a later edit regenerates it with a rollback to it.
    """
    newest = superseded[-1]
    reply_id = _new_id()
    sources = newest.sources
    answer = _span(
        reply_id,
        kind=rl.SpanKind.TURN,
        delivery_id=event_id,
        sources=rl.SpanSources(
            pending=(),
            logical=sources.logical_source_event_ids,
            discovery=sources.discovery_event_ids,
        ),
        now_ns=now_ns,
        outcome=rl.SpanOutcome.COMPLETED,
    )
    reply = _reply(
        transaction,
        principal_id,
        entity_name=entity_name,
        room_id=newest.room_id,
        thread_id=newest.thread_id,
        state=rl.ReplyState.COMPLETED,
        span=answer,
        presentation=presentations.empty(newest.entity_kind == "team"),
        now_ns=now_ns,
        event_id=event_id,
    )
    pauses = _superseded_pauses(reply_id, superseded, now_ns)
    return _with_replaced_approvals(_Adoption(reply=reply, spans=(answer,)), superseded, pauses)


def _reply(
    transaction: Transaction,
    principal_id: str,
    *,
    entity_name: str,
    room_id: str,
    thread_id: str | None,
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
    prepared_edit: str | None = None,
) -> rl.Span:
    return rl.Span(
        span_id=_new_id(),
        reply_id=reply_id,
        kind=kind,
        delivery_id=delivery_id,
        sources=sources,
        bot_generation=_LEGACY_GENERATION,
        claimed_at_ns=now_ns,
        base_sequence=0,
        approval_id=approval_id,
        outcome=outcome,
        ended_at_ns=None if outcome is None else now_ns,
        prepared_edit=prepared_edit,
    )


def _pause_span(reply_id: str, continuation: ApprovalContinuation, outcome: rl.SpanOutcome, now_ns: int) -> rl.Span:
    """Return the span whose pause created ``continuation``, holding its sources as one paused now does."""
    sources = continuation.sources
    return _span(
        reply_id,
        kind=rl.SpanKind.REGENERATION if continuation.prepared_edit_record is not None else rl.SpanKind.TURN,
        delivery_id=continuation.source_event_ids[0],
        sources=rl.SpanSources(
            pending=sources.pending_event_ids,
            logical=sources.logical_source_event_ids,
            discovery=sources.discovery_event_ids,
        ),
        now_ns=now_ns,
        outcome=outcome,
        approval_id=continuation.approval_id,
        # A regeneration's paused span carries the edit it selected.
        prepared_edit=(
            None
            if continuation.prepared_edit_record is None
            else turn_records.encode_prepared_edit(continuation.prepared_edit_record)
        ),
    )


def _paused_reply(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> _Adoption:
    """A continuation pauses its reply, which its approval runtime then resumes or settles.

    A failing one whose failure note's FINAL Matrix took or refused for good
    already ended the reply in the room: the reply ends as the cleanup still
    owed will end it, and the continuation holds it until that cleanup runs.
    """
    reply_id = _new_id()
    paused = _pause_span(reply_id, continuation, rl.SpanOutcome.PAUSED, now_ns)
    shown = presentations.paused(
        continuation,
        _legacy_paused_answer(transaction, principal_id, continuation.approval_id),
        paused.span_id,
    )
    reply = _reply(
        transaction,
        principal_id,
        entity_name=entity_name,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        state=_ended_by_failure(transaction, principal_id, continuation) or rl.ReplyState.PAUSED,
        span=paused,
        presentation=shown,
        now_ns=now_ns,
        event_id=continuation.response_event_id,
        # A regeneration's reply keeps the order of the edit it answers, which an older Stop then misses.
        edit_receipt_order=(
            continuation.sources.edit_receipt_order if paused.kind is rl.SpanKind.REGENERATION else None
        ),
    )
    return _Adoption(approval_id=continuation.approval_id, reply=reply, spans=(paused,))


def _ended_by_failure(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
) -> rl.ReplyState | None:
    """Return the state a failing continuation's cleanup ends its reply in, once its note's FINAL resolved."""
    if continuation.state != "failing":
        return None
    final = outbox.load(
        transaction,
        principal_id,
        delivery_id=continuation.source_event_ids[0],
        stage=DeliveryStage.FINAL,
    )
    if final is None or (final.acknowledged_event_id is None and final.permanent_failure_reason is None):
        return None
    return rl.ReplyState.CANCELLED if continuation.failure_reason == "cancelled_by_user" else rl.ReplyState.FAILED
