"""Transactional application of reply lifecycle transitions.

The pure rules in ``mindroom.reply_lifecycle`` decide; this module reads the
facts a rule needs inside the transaction that also performs the journal's coupled
durable step, writes what the rule decided, and runs the in-transaction
effects. Post-commit effects are returned to the caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom import reply_lifecycle as rl
from mindroom.logging_config import get_logger
from mindroom.reply_lifecycle import (
    CancelSpan,
    Effect,
    FenceApproval,
    Reply,
    SettleSources,
    Span,
    Transition,
    WakeApproval,
)

from . import approval_continuations, journal, outbox, reply_messages, reply_spans, turn_records
from .membership_state import claim_membership_epoch
from .projection import is_tombstoned

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.turn_record import TurnRecord

    from .backend import Backend, Transaction
    from .models import MatrixDelivery


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """After commit: the turn ledger learns a turn a reply's settlement recorded answered."""

    record: TurnRecord


# Effects the caller runs after the transaction commits.
type PostCommitEffect = CancelSpan | WakeApproval | TurnCompleted


@dataclass(frozen=True, slots=True)
class EndedApproval:
    """An approval continuation that finished or was released, and the work its commit left for afterwards."""

    post_commit: tuple[PostCommitEffect, ...]


type Decide = Callable[[Reply, Span], Transition]


@dataclass(frozen=True, slots=True)
class AppliedTransition:
    """A committed transition and the work left for after the commit."""

    transition: Transition
    post_commit: tuple[PostCommitEffect, ...]


def _span_for(transaction: Transaction, principal_id: str, transition: Transition, span_id: str) -> Span:
    """Return a span as the transition left it, falling back to the stored row."""
    for span in transition.spans:
        if span.span_id == span_id:
            return span
    stored = reply_spans.load(transaction, principal_id, span_id)
    if stored is None:
        msg = f"Reply span {span_id} does not exist"
        raise RuntimeError(msg)
    return stored


def settled_event_ids(transaction: Transaction, principal_id: str, transition: Transition) -> tuple[str, ...]:
    """Return the journal sources a transition's effects settle."""
    settled: list[str] = []
    for effect in transition.effects:
        if isinstance(effect, SettleSources):
            settled.extend(_span_for(transaction, principal_id, transition, effect.span_id).sources.pending)
    return tuple(dict.fromkeys(settled))


def apply(transaction: Transaction, principal_id: str, transition: Transition) -> AppliedTransition:
    """Write one transition and run its in-transaction effects; return the post-commit ones.

    The reply comes back with the approval that holds it after those effects,
    read from the continuations, so a caller never caches a hold the
    transaction removed.
    """
    if transition.unmodeled is not None:
        logger.warning(
            "reply_unmodeled",
            principal_id=principal_id,
            reply_id=None if transition.reply is None else transition.reply.reply_id,
            reason=transition.unmodeled,
        )
    reply_messages.persist(transaction, principal_id, transition)
    post_commit: list[PostCommitEffect] = []
    for effect in transition.effects:
        _run(transaction, principal_id, transition, effect, post_commit)
    if transition.reply is not None:
        held = reply_messages.held_by(transaction, principal_id, transition.reply.reply_id)
        if held != transition.reply.approval_id:
            transition = replace(transition, reply=replace(transition.reply, approval_id=held))
    return AppliedTransition(transition=transition, post_commit=tuple(post_commit))


def _run(
    transaction: Transaction,
    principal_id: str,
    transition: Transition,
    effect: Effect,
    post_commit: list[PostCommitEffect],
) -> None:
    match effect:
        case SettleSources(span_id=span_id, answered=answered):
            span = _span_for(transaction, principal_id, transition, span_id)
            reply = transition.reply
            assert reply is not None, "a settlement belongs to a reply's transition"
            # Nothing answers a turn whose every message the user deleted, whichever rule settles it.
            if not answered or all(
                is_tombstoned(transaction, principal_id, reply.room_id, source) for source in span.sources.logical
            ):
                journal.settle_many(transaction, principal_id, span.sources.pending)
                return
            completed = turn_records.settle_turn(
                transaction,
                principal_id,
                reply.entity_name,
                pending=span.sources.pending,
                logical=span.sources.logical,
            )
            if completed is not None:
                # The ledger's write ordering and cache learn it after the commit.
                post_commit.append(TurnCompleted(completed))
        case FenceApproval(approval_id=approval_id, disposition=disposition):
            approval_continuations.fence(transaction, principal_id, approval_id=approval_id, reason=disposition)
        case CancelSpan() | WakeApproval():
            post_commit.append(effect)
        case _:
            msg = f"Reply effect {effect!r} has no transactional owner yet"
            raise NotImplementedError(msg)


def retired(transaction: Transaction, principal_id: str, span: Span, *, author_generation: str | None = None) -> bool:
    """Return whether a write on a running span comes from a bot instance that no longer owns the principal's replies.

    Such a span writes nothing more: the instance that took over replays its
    sources, and the retired one shuts down once it notices. A write the
    span's own task makes names its instance as ``author_generation``, so a
    retired instance's resume is refused too. A resume an older instance left
    running stays open to the owner's approval recovery, which ends it.
    """
    if span.ended:
        return False
    active = reply_messages.active_generation(transaction, principal_id)
    if active is None:
        return False
    if author_generation is not None and author_generation != active:
        return True
    return span.kind is not rl.SpanKind.APPROVAL_RESUME and span.bot_generation != active


def decide_on_span(
    transaction: Transaction,
    principal_id: str,
    *,
    reply_id: str,
    span_id: str,
    decide: Decide,
    author_generation: str | None = None,
) -> AppliedTransition:
    """Lock one reply, read the named span, apply the rule, and write what it decided.

    ``author_generation`` names the bot instance of the span's own task, when it writes.
    """
    reply = reply_messages.lock(transaction, principal_id, reply_id)
    span = reply_spans.load(transaction, principal_id, span_id)
    if reply is None or span is None:
        msg = f"Reply {reply_id} or span {span_id} does not exist"
        raise RuntimeError(msg)
    if retired(transaction, principal_id, span, author_generation=author_generation):
        return AppliedTransition(transition=rl.Transition(outcome=rl.Outcome.STALE, reply=reply), post_commit=())
    return with_ended_span(apply(transaction, principal_id, decide(reply, span)), span)


def with_ended_span(applied: AppliedTransition, span: Span) -> AppliedTransition:
    """Return a refused decision carrying its span when that span already ended, so the task running it learns so.

    A deletion or departure ends a running span from outside; the span's own
    exits are then refused, and must stop trying.
    """
    if applied.transition.applied or not span.ended or applied.transition.spans:
        return applied
    return replace(applied, transition=replace(applied.transition, spans=(span,)))


def lock_paused_reply(
    transaction: Transaction,
    principal_id: str,
    continuation: approval_continuations.ApprovalContinuation,
) -> Reply | None:
    """Lock the reply a continuation paused, before the continuation itself.

    A departure locks a room's replies before their continuations, and so
    does every path that reads a continuation to change its reply. A pause or
    an advance writes its continuation first and then locks the reply, as a
    source deletion it races locks the source and then the reply; one writer
    per journal runs them one at a time.
    """
    # LEGACY_COMPAT: Continuations adopted from an earlier release before reply classification names their span.
    # Legacy format: an approval_continuations row with no span_id, as the schema upgrade leaves every continuation
    # until its entity's first start classifies it, and for good when that entity never starts again.
    # Last legacy release: v2026.10.208; replacement: the unreleased durable reply messages name the paused span on
    # every continuation they create.
    # Handling: no reply exists for it, so nothing is locked and its settlement reads the adopted identity instead.
    # Coverage: tests/test_legacy_continuation_identity.py::test_an_unclassified_continuation_settles_its_adopted_sources.
    if continuation.span_id is None:
        return None
    span = reply_spans.load(transaction, principal_id, continuation.span_id)
    return None if span is None else reply_messages.lock(transaction, principal_id, span.reply_id)


def end_replies_of_deleted_source(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    event_id: str,
    deleted: Callable[[str], bool],
    now_ns: int,
) -> None:
    """End the replies whose current work lost every logical source to this deletion, settling their sources.

    A paused reply, an approval resume, and an answer already written are
    kept. The bot cancels the spans it runs and redacts what they showed after
    the deletion commits, and the turn ledger loads the turns this answered.
    """
    for reply_id in reply_messages.naming_logical_source(transaction, principal_id, event_id):
        reply = reply_messages.lock(transaction, principal_id, reply_id)
        if reply is None or reply.terminal or reply.room_id != room_id:
            continue
        span = reply_spans.load(transaction, principal_id, reply.current_span_id or reply.last_span_id)
        if span is None or event_id not in span.sources.logical or not all(map(deleted, span.sources.logical)):
            continue
        transition = rl.sources_deleted(reply, span, now_ns=now_ns)
        if transition.applied:
            # The projection cannot run post-commit effects; the bot takes the span to cancel from this record.
            applied = apply(transaction, principal_id, transition)
            cancelled = next((effect.span_id for effect in applied.post_commit if isinstance(effect, CancelSpan)), None)
            reply_messages.record_deletion_ending(
                transaction,
                principal_id,
                reply_messages.DeletionEnding(reply.reply_id, cancelled),
            )


def approval_finished(
    transaction: Transaction,
    principal_id: str,
    continuation: approval_continuations.ApprovalContinuation,
    *,
    owner_available: bool,
) -> AppliedTransition | None:
    """Apply a finished continuation to the reply it paused, settling the sources its pause held.

    Its turn stays unanswered when no owner is left to answer it.
    """
    reply = lock_paused_reply(transaction, principal_id, continuation)
    if reply is None:
        return None
    assert continuation.span_id is not None, "a continuation with a reply names the span that paused it"
    failed = continuation.state == "failing"
    reason = continuation.failure_reason
    disposition: rl.FailureDisposition | None = None
    if failed:
        disposition = "cancelled_by_user" if reason == "cancelled_by_user" else "failed"
    return apply(
        transaction,
        principal_id,
        rl.approval_settled(
            reply,
            reply_spans.load(transaction, principal_id, reply.last_span_id),
            approval_id=continuation.approval_id,
            paused_span_id=continuation.span_id,
            result="failed" if failed else "finished",
            disposition=disposition,
            answers_turn=owner_available,
            now_ns=time.time_ns(),
        ),
    )


def approval_released(
    transaction: Transaction,
    principal_id: str,
    continuation: approval_continuations.ApprovalContinuation,
) -> AppliedTransition | None:
    """End the span running for a continuation handed back to replay; see ``rl.approval_released``.

    That is its resume span, or a span it was approved in place in; a reply
    whose span a restart already ended only loses the approval's hold.
    """
    reply = lock_paused_reply(transaction, principal_id, continuation)
    if reply is None:
        return None
    holds = reply.approval_id == continuation.approval_id
    if reply.current_span_id is None:
        if not holds:
            return None
        return apply(transaction, principal_id, rl.approval_released(reply, None, now_ns=time.time_ns()))
    span = reply_spans.load(transaction, principal_id, reply.current_span_id)
    resumes = (
        span is not None and span.kind is rl.SpanKind.APPROVAL_RESUME and span.approval_id == continuation.approval_id
    )
    if span is None or not (resumes or holds):
        return None
    return apply(transaction, principal_id, rl.approval_released(reply, span, now_ns=time.time_ns()))


# ---------------------------------------------------------------------------
# Claims


@dataclass(frozen=True, slots=True)
class ClaimLookup:
    """Where a claim looks for the reply it continues."""

    interactive_span_id: str | None = None
    existing_event_id: str | None = None


def claim(
    transaction: Transaction,
    principal_id: str,
    request: rl.ClaimRequest,
    lookup: ClaimLookup,
) -> AppliedTransition:
    """Find the reply a span continues and claim it, in one transaction."""
    interactive = (
        None
        if lookup.interactive_span_id is None
        else reply_spans.load(transaction, principal_id, lookup.interactive_span_id)
    )
    reply: Reply | None = None
    if interactive is not None:
        reply = reply_messages.lock(transaction, principal_id, interactive.reply_id)
    if reply is None and lookup.existing_event_id is not None:
        found = reply_messages.for_event(transaction, principal_id, lookup.existing_event_id)
        reply = None if found is None else reply_messages.lock(transaction, principal_id, found.reply_id)
    if reply is None:
        found = reply_messages.for_sources(
            transaction,
            principal_id,
            (*request.sources.pending, *request.sources.logical),
        )
        reply = None if found is None else reply_messages.lock(transaction, principal_id, found.reply_id)
    if reply is not None and reply.state is rl.ReplyState.GONE and request.driving_edit_id is None:
        # A removed reply is never continued; the turn answers again in a new one.
        reply = None
    active_generation = reply_messages.active_generation(transaction, principal_id) or request.bot_generation
    context = rl.ClaimContext(
        reply=reply,
        last_span=None if reply is None else reply_spans.load(transaction, principal_id, reply.last_span_id),
        current_span=(
            None
            if reply is None or reply.current_span_id is None
            else reply_spans.load(transaction, principal_id, reply.current_span_id)
        ),
        interactive_span=interactive,
        durable_write_debt=reply is not None and has_unresolved_rows(transaction, principal_id, reply.reply_id),
        active_generation=active_generation,
    )
    if reply is None or request.driving_edit_id is not None:
        # A new reply, or a regeneration of one, writes in the membership its delivery was admitted in.
        admitted = journal.admitted_membership_owner(transaction, principal_id, request.delivery_id)
        if admitted is not None:
            request = replace(request, membership_epoch=admitted[1])
    return apply(transaction, principal_id, rl.claim(request, context))


# ---------------------------------------------------------------------------
# Stop


def _record_stop(
    transaction: Transaction,
    principal_id: str,
    *,
    event_id: str,
    receipt_order: int,
) -> AppliedTransition | None:
    """Record a Stop on the reply bound to one event; ``None`` when no reply is bound to it."""
    found = reply_messages.for_event(transaction, principal_id, event_id)
    if found is None:
        return None
    reply = reply_messages.lock(transaction, principal_id, found.reply_id)
    assert reply is not None
    span_id = reply.current_span_id or reply.last_span_id
    span = reply_spans.load(transaction, principal_id, span_id)
    return apply(
        transaction,
        principal_id,
        rl.stop(
            reply,
            span,
            rl.StopFacts(
                receipt_order=receipt_order,
                span_live=_runs_here(transaction, principal_id, reply, span),
            ),
            now_ns=time.time_ns(),
        ),
    )


def _runs_here(transaction: Transaction, principal_id: str, reply: Reply, span: Span | None) -> bool:
    """Return whether this bot instance runs the reply's current span, so a Stop cancels it instead of ending it."""
    return (
        span is not None
        and not span.ended
        and span.span_id == reply.current_span_id
        and span.bot_generation == reply_messages.active_generation(transaction, principal_id)
    )


def _supersede_replay(
    transaction: Transaction,
    principal_id: str,
    source_event_ids: tuple[str, ...],
    *,
    now_ns: int,
) -> AppliedTransition | None:
    """Settle a superseded replay's sources with the reply they left; ``None`` when no reply has them."""
    found = reply_messages.for_sources(transaction, principal_id, source_event_ids)
    if found is None:
        return None
    reply = reply_messages.lock(transaction, principal_id, found.reply_id)
    last = reply_spans.load(transaction, principal_id, found.last_span_id)
    assert reply is not None
    assert last is not None
    return apply(
        transaction,
        principal_id,
        rl.replay_superseded(
            reply,
            last,
            durable_write_debt=has_unresolved_rows(transaction, principal_id, reply.reply_id),
            now_ns=now_ns,
        ),
    )


def drop_replays(
    transaction: Transaction,
    principal_id: str,
    event_ids: tuple[str, ...],
    *,
    now_ns: int,
) -> tuple[str, ...]:
    """End the replies waiting to replay these sources, which just settled without a turn; return their ids."""
    if not event_ids:
        return ()
    ended: list[str] = []
    for reply_id in reply_messages.waiting_to_replay(transaction, principal_id, event_ids):
        reply = reply_messages.lock(transaction, principal_id, reply_id)
        assert reply is not None
        last = reply_spans.load(transaction, principal_id, reply.last_span_id)
        assert last is not None
        sources_pending = any(journal.is_pending(transaction, principal_id, source) for source in last.sources.pending)
        applied = apply(
            transaction,
            principal_id,
            rl.replay_dropped(reply, last, sources_pending=sources_pending, now_ns=now_ns),
        )
        if applied.transition.applied:
            ended.append(reply_id)
    return tuple(ended)


def end_entity_replies(transaction: Transaction, ends: Callable[[str], bool], *, now_ns: int) -> int:
    """End the open replies of entities with no bot any more, without writing to Matrix."""
    ended = 0
    for principal_id, found in reply_messages.open_replies(transaction):
        if not ends(found.entity_name):
            continue
        reply = reply_messages.lock(transaction, principal_id, found.reply_id)
        assert reply is not None
        span = reply_spans.load(transaction, principal_id, reply.current_span_id or reply.last_span_id)
        assert span is not None, "a reply's last span exists"
        if apply(transaction, principal_id, rl.removed_entity(reply, span, now_ns=now_ns)).transition.applied:
            ended += 1
    return ended


def _owner_lost(
    transaction: Transaction,
    principal_id: str,
    *,
    active_generation: str,
    now_ns: int,
) -> tuple[AppliedTransition, ...]:
    """End the work an older bot instance left on this principal's replies.

    Runs once per bot instance at start, after its generation is written and
    before journal replay: records and rows only, nothing is sent here.
    """
    applied: list[AppliedTransition] = []
    for found in reply_messages.in_states(transaction, principal_id, (rl.ReplyState.ACTIVE, rl.ReplyState.PAUSED)):
        reply = reply_messages.lock(transaction, principal_id, found.reply_id)
        last = reply_spans.load(transaction, principal_id, found.last_span_id)
        assert reply is not None
        assert last is not None
        facts = rl.OwnerLostFacts(
            active_generation=active_generation,
            sources_pending=any(
                journal.is_pending(transaction, principal_id, event_id) for event_id in last.sources.pending
            ),
        )
        transition = rl.owner_lost(reply, last, facts, now_ns=now_ns)
        if transition.applied:
            applied.append(apply(transaction, principal_id, transition))
    return tuple(applied)


@dataclass(frozen=True, slots=True)
class StopTarget:
    """What a Stop on one event reaches among the reply records."""

    # The reply bound to the event, in the Stop's room.
    reply: Reply | None = None
    # No reply is bound to the event yet; the Stop waits for its create.
    pending: bool = False


def _stop_target(
    transaction: Transaction,
    principal_id: str,
    *,
    event_id: str,
    room_id: str,
    receipt_order: int,
    may_wait: bool,
    now_ns: int,
) -> StopTarget:
    """Find the reply a Stop reaches, or store the Stop for a create still unresolved in its room."""
    found = reply_messages.for_event(transaction, principal_id, event_id)
    if found is not None:
        if found.room_id != room_id:
            return StopTarget()
        return StopTarget(reply=found)
    if not may_wait or not reply_messages.has_unresolved_create(transaction, principal_id, room_id):
        return StopTarget()
    reply_messages.record_pending_stop(
        transaction,
        principal_id,
        target_event_id=event_id,
        receipt_order=receipt_order,
        room_id=room_id,
        now_ns=now_ns,
    )
    return StopTarget(pending=True)


# ---------------------------------------------------------------------------
# Rows


def edit_delivery_id(span_delivery_id: str, sequence: int) -> str:
    """Return the derived delivery id of a reply's non-terminal durable write."""
    return f"{span_delivery_id}:edit:{sequence}"


@dataclass(frozen=True, slots=True)
class ReplyCreation:
    """A reply whose first row creates it: an interactive selection's acknowledgement."""

    claim: rl.ClaimRequest
    # The encoded presentation the acknowledgement shows.
    shown: str


@dataclass(frozen=True, slots=True)
class ReplyRowRequest:
    """A durable write of a reply, decided by a lifecycle rule inside its enqueue transaction.

    A row that creates its reply carries ``create`` instead of ``decide``.
    """

    reply_id: str
    span_id: str
    decide: Decide | None
    # Whether the row shows only the reply's placeholder.
    placeholder_only: bool = False
    create: ReplyCreation | None = None
    # The stage the caller rendered for; a span's INITIAL or FINAL is written
    # once, and a retry of either resolves to the row already recorded.
    stage: rl.WriteStage | None = None
    # The bot instance of the span's own task, when it writes the row.
    author_generation: str | None = None

    def __post_init__(self) -> None:
        """Require exactly one of a rule for an existing reply and a reply to create."""
        if (self.decide is None) == (self.create is None):
            msg = "A reply row either decides on an existing reply or creates one"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ReplyRowEnqueue:
    """What one reply-row enqueue committed."""

    applied: AppliedTransition
    # Set when the rule chose a row and the outbox recorded it.
    delivery_id: str | None = None
    stage: rl.WriteStage | None = None
    transaction_id: str | None = None
    settled_event_ids: tuple[str, ...] = ()
    # The row's place in the reply's write sequence.
    sequence: int | None = None

    @property
    def transition(self) -> Transition:
        """Return the transition the rule decided."""
        return self.applied.transition


def _row_placeholder_only(delivery: MatrixDelivery) -> bool:
    """Return whether one reply row shows only the reply's placeholder."""
    return (delivery.reply_row or {}).get("placeholder_only") is True


def row_new_text(delivery: MatrixDelivery) -> str | None:
    """Return the fallback body a reply row's edit carries when its target was bound after it was prepared."""
    new_text = (delivery.reply_row or {}).get("new_text")
    return new_text if isinstance(new_text, str) else None


def row_facts(*, placeholder_only: bool, new_text: str | None) -> dict[str, object]:
    """Return what a reply row's acknowledgement and late edit target need."""
    facts: dict[str, object] = {"placeholder_only": placeholder_only}
    if new_text is not None:
        facts["new_text"] = new_text
    return facts


def has_unresolved_rows(transaction: Transaction, principal_id: str, reply_id: str) -> bool:
    """Return whether any durable write of the reply has an unknown Matrix outcome."""
    return bool(outbox.unresolved_reply_rows(transaction, principal_id, reply_id))


def _write_facts(delivery: MatrixDelivery) -> rl.WriteFacts:
    assert delivery.span_id is not None
    assert delivery.reply_sequence is not None
    return rl.WriteFacts(
        stage=rl.WriteStage(delivery.stage.value),
        sequence=delivery.reply_sequence,
        span_id=delivery.span_id,
        creates_event=delivery.edits_event_id is None,
        placeholder_only=_row_placeholder_only(delivery),
    )


def acknowledge_row(
    transaction: Transaction,
    principal_id: str,
    delivery: MatrixDelivery,
    *,
    event_id: str,
    membership_current: bool,
) -> AppliedTransition | None:
    """Apply Matrix's acknowledgement of one reply row, then any Stop that waited for its event."""
    if delivery.reply_id is None or delivery.span_id is None or delivery.reply_sequence is None:
        return None
    reply = reply_messages.lock(transaction, principal_id, delivery.reply_id)
    if reply is None:
        return None
    now_ns = time.time_ns()
    applied = apply(
        transaction,
        principal_id,
        rl.write_acknowledged(
            reply,
            _write_facts(delivery),
            event_id=event_id,
            membership_current=membership_current,
            now_ns=now_ns,
        ),
    )
    bound = applied.transition.reply or reply
    if delivery.edits_event_id is not None:
        return applied
    # A Stop reaction in another room never reaches this reply, whatever event it names.
    receipt_order = (
        reply_messages.take_pending_stop(transaction, principal_id, event_id, bound.room_id)
        if bound.event_id == event_id
        else None
    )
    reply_messages.drop_unbindable_stops(transaction, principal_id, bound.room_id)
    if receipt_order is None:
        return applied
    # An interactive selection's acknowledgement span is not current yet, but the Stop ends it too.
    current = reply_spans.load(transaction, principal_id, bound.current_span_id or bound.last_span_id)
    if current is not None and current.ended:
        current = None
    stopped = apply(
        transaction,
        principal_id,
        rl.stop(
            bound,
            current,
            rl.StopFacts(
                receipt_order=receipt_order,
                span_live=_runs_here(transaction, principal_id, bound, current),
            ),
            now_ns=now_ns,
        ),
    )
    return AppliedTransition(transition=stopped.transition, post_commit=(*applied.post_commit, *stopped.post_commit))


def fail_row(
    transaction: Transaction,
    principal_id: str,
    delivery: MatrixDelivery,
    *,
    reason: str,
) -> AppliedTransition | None:
    """Apply a permanent refusal of one reply row."""
    if delivery.reply_id is None or delivery.span_id is None or delivery.reply_sequence is None:
        return None
    reply = reply_messages.lock(transaction, principal_id, delivery.reply_id)
    span = reply_spans.load(transaction, principal_id, delivery.span_id)
    if reply is None or span is None:
        return None
    applied = apply(
        transaction,
        principal_id,
        rl.write_failed(reply, span, rl.FailedWrite(_write_facts(delivery), reason), now_ns=time.time_ns()),
    )
    if delivery.edits_event_id is None:
        reply_messages.drop_unbindable_stops(transaction, principal_id, reply.room_id)
    return applied


# ---------------------------------------------------------------------------
# Store view


@dataclass(frozen=True, slots=True)
class ReplyStore:
    """One principal's reply records."""

    _backend: Backend
    _principal_id: str

    async def load(self, reply_id: str) -> Reply | None:
        """Return one reply."""
        return await self._backend.read(
            lambda transaction: reply_messages.load(transaction, self._principal_id, reply_id),
        )

    async def for_event(self, event_id: str) -> Reply | None:
        """Return the reply bound to one Matrix event."""
        return await self._backend.read(
            lambda transaction: reply_messages.for_event(transaction, self._principal_id, event_id),
        )

    async def for_sources(self, event_ids: tuple[str, ...]) -> Reply | None:
        """Return the newest reply answering any of these sources."""
        return await self._backend.read(
            lambda transaction: reply_messages.for_sources(transaction, self._principal_id, event_ids),
        )

    async def span(self, span_id: str) -> Span | None:
        """Return one span."""
        return await self._backend.read(
            lambda transaction: reply_spans.load(transaction, self._principal_id, span_id),
        )

    async def spans(self, reply_id: str) -> tuple[Span, ...]:
        """Return every span of one reply, oldest first."""
        return await self._backend.read(
            lambda transaction: reply_spans.for_reply(transaction, self._principal_id, reply_id),
        )

    async def record_tool_call(self, *, span_id: str, call_id: str, entry_json: str, now_ns: int) -> None:
        """Record a tool call a span made, before the tool runs and again once it returned."""
        await self._backend.write(
            lambda transaction: reply_spans.record_tool_call(
                transaction,
                self._principal_id,
                span_id=span_id,
                call_id=call_id,
                entry_json=entry_json,
                now_ns=now_ns,
            ),
        )

    async def tool_calls(self, span_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Return the recorded tool calls of these spans, in the order they started."""
        return await self._backend.read(
            lambda transaction: reply_spans.tool_calls(transaction, self._principal_id, span_ids),
        )

    async def write_generation(self, generation: str, *, now_ns: int) -> None:
        """Make one bot instance the owner of this principal's replies."""
        await self._backend.write(
            lambda transaction: reply_messages.write_generation(
                transaction,
                self._principal_id,
                generation=generation,
                now_ns=now_ns,
            ),
        )

    async def active_generation(self) -> str | None:
        """Return the bot instance that owns this principal's replies now."""
        return await self._backend.read(
            lambda transaction: reply_messages.active_generation(transaction, self._principal_id),
        )

    async def claim(self, request: rl.ClaimRequest, lookup: ClaimLookup) -> AppliedTransition:
        """Find and claim the reply one span continues."""
        return await self._backend.write(
            lambda transaction: claim(transaction, self._principal_id, request, lookup),
        )

    async def decide(
        self,
        *,
        reply_id: str,
        span_id: str,
        decide: Decide,
        author_generation: str | None = None,
    ) -> AppliedTransition:
        """Apply one rule to a locked reply and its span, in a transaction of its own."""
        return await self._backend.write(
            lambda transaction: decide_on_span(
                transaction,
                self._principal_id,
                reply_id=reply_id,
                span_id=span_id,
                decide=decide,
                author_generation=author_generation,
            ),
        )

    async def write_ahead(
        self,
        *,
        reply_id: str,
        span_id: str,
        shown: str,
        previous: rl.ProgressConfirmation | None,
        active_generation: str,
        now_ns: int,
    ) -> AppliedTransition:
        """Record a span's next direct progress edit, deferring it while earlier durable writes are unresolved."""
        return await self._backend.write(
            lambda transaction: decide_on_span(
                transaction,
                self._principal_id,
                reply_id=reply_id,
                span_id=span_id,
                author_generation=active_generation,
                decide=lambda reply, span: rl.write_ahead(
                    reply,
                    span,
                    shown=shown,
                    previous=previous,
                    active_generation=active_generation,
                    durable_write_debt=has_unresolved_rows(transaction, self._principal_id, reply_id),
                    now_ns=now_ns,
                ),
            ),
        )

    async def update(self, reply_id: str, decide: Callable[[Reply], Transition]) -> AppliedTransition | None:
        """Apply one reply-only rule to the locked reply, in a transaction of its own."""

        def write(transaction: Transaction) -> AppliedTransition | None:
            reply = reply_messages.lock(transaction, self._principal_id, reply_id)
            return None if reply is None else apply(transaction, self._principal_id, decide(reply))

        return await self._backend.write(write)

    async def record_stop_button(self, reply_id: str, event_id: str, *, now_ns: int) -> AppliedTransition | None:
        """Record the Stop button sent for a reply, in the membership the reply was written in."""

        def write(transaction: Transaction) -> AppliedTransition | None:
            reply = reply_messages.lock(transaction, self._principal_id, reply_id)
            if reply is None:
                return None
            membership_current = claim_membership_epoch(
                transaction,
                self._principal_id,
                room_id=reply.room_id,
                expected_membership_epoch=reply.membership_epoch,
            )
            return apply(
                transaction,
                self._principal_id,
                rl.record_stop_button(reply, event_id=event_id, membership_current=membership_current, now_ns=now_ns),
            )

        return await self._backend.write(write)

    async def owner_lost(self, active_generation: str, *, now_ns: int) -> tuple[AppliedTransition, ...]:
        """End what an older bot instance left running on these replies; see ``_owner_lost``."""
        return await self._backend.write(
            lambda transaction: _owner_lost(
                transaction,
                self._principal_id,
                active_generation=active_generation,
                now_ns=now_ns,
            ),
        )

    async def supersede_replay(self, source_event_ids: tuple[str, ...], *, now_ns: int) -> AppliedTransition | None:
        """Settle a superseded replay's sources with the reply they left; see ``_supersede_replay``."""
        return await self._backend.write(
            lambda transaction: _supersede_replay(transaction, self._principal_id, source_event_ids, now_ns=now_ns),
        )

    async def record_stop(self, event_id: str, receipt_order: int) -> AppliedTransition | None:
        """Record a Stop on the reply bound to one event; ``None`` when no reply is bound to it."""
        return await self._backend.write(
            lambda transaction: _record_stop(
                transaction,
                self._principal_id,
                event_id=event_id,
                receipt_order=receipt_order,
            ),
        )

    async def accepts_stop(self, event_id: str, room_id: str) -> bool:
        """Return whether a Stop on this event reaches a reply: a running one bound to it, or a create in its room."""

        def read(transaction: Transaction) -> bool:
            found = reply_messages.for_event(transaction, self._principal_id, event_id)
            if found is not None:
                return found.room_id == room_id and not found.terminal
            return reply_messages.has_unresolved_create(transaction, self._principal_id, room_id)

        return await self._backend.read(read)

    async def stop_target(
        self,
        event_id: str,
        *,
        room_id: str,
        receipt_order: int,
        may_wait: bool,
        now_ns: int,
    ) -> StopTarget:
        """Find the reply a Stop reaches, storing the Stop when the event's create is unresolved."""
        return await self._backend.write(
            lambda transaction: _stop_target(
                transaction,
                self._principal_id,
                event_id=event_id,
                room_id=room_id,
                receipt_order=receipt_order,
                may_wait=may_wait,
                now_ns=now_ns,
            ),
        )

    async def event_ids_of_spans(self, room_id: str, span_ids: frozenset[str]) -> frozenset[str]:
        """Return the events of a room's replies whose current span is one of these."""
        if not span_ids:
            return frozenset()
        return await self._backend.read(
            lambda transaction: reply_messages.event_ids_of_spans(
                transaction,
                self._principal_id,
                room_id,
                tuple(sorted(span_ids)),
            ),
        )

    async def forget_finished(self, *, before_ns: int, limit: int) -> int:
        """Delete up to ``limit`` finished replies that owe nothing and are older than ``before_ns``."""
        return await self._backend.write(
            lambda transaction: reply_messages.forget_finished(
                transaction,
                self._principal_id,
                before_ns=before_ns,
                limit=limit,
            ),
        )

    async def take_deletion_endings(self) -> tuple[reply_messages.DeletionEnding, ...]:
        """Remove and return the replies source deletions ended, with the spans they cancelled."""
        return await self._backend.write(
            lambda transaction: reply_messages.take_deletion_endings(transaction, self._principal_id),
        )

    async def spans_in_room(self, room_id: str, span_ids: frozenset[str]) -> frozenset[str]:
        """Return which of these spans belong to the room's replies."""
        if not span_ids:
            return frozenset()
        return await self._backend.read(
            lambda transaction: reply_messages.spans_in_room(
                transaction,
                self._principal_id,
                room_id,
                tuple(sorted(span_ids)),
            ),
        )

    async def with_pending_work(self) -> tuple[Reply, ...]:
        """Return replies owing a redaction or a note not yet enqueued."""
        return await self._backend.read(
            lambda transaction: reply_messages.with_pending_work(transaction, self._principal_id),
        )

    async def has_unresolved_rows(self, reply_id: str) -> bool:
        """Return whether any durable write of the reply has an unknown Matrix outcome."""
        return await self._backend.read(
            lambda transaction: has_unresolved_rows(transaction, self._principal_id, reply_id),
        )
