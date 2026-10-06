"""Transactional application of reply lifecycle transitions.

The pure rules in ``mindroom.reply_lifecycle`` decide; this module reads the
facts a rule needs inside the transaction that also performs main's coupled
durable step, writes what the rule decided, and runs the in-transaction
effects. Post-commit effects are returned to the caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom import reply_lifecycle as rl
from mindroom.reply_lifecycle import (
    CancelSpan,
    Effect,
    Reply,
    SettleSources,
    Span,
    TransferStop,
    Transition,
    WakeApproval,
)

from . import journal, outbox, reply_messages, reply_spans

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .backend import Backend, Transaction
    from .models import MatrixDelivery

# Effects the caller runs after the transaction commits.
type PostCommitEffect = CancelSpan | WakeApproval
type Decide = Callable[[Reply, Span], Transition]

# Local facts a reply row needs after Matrix answers it; never sent.
REPLY_ROW_RESULT_KEY = "io.mindroom.reply_row"


@dataclass(frozen=True, slots=True)
class AppliedTransition:
    """A committed transition and the work left for after the commit."""

    transition: Transition
    post_commit: tuple[PostCommitEffect, ...]


def span_for(transaction: Transaction, principal_id: str, transition: Transition, span_id: str) -> Span:
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
            settled.extend(span_for(transaction, principal_id, transition, effect.span_id).sources.pending)
    return tuple(dict.fromkeys(settled))


def apply(transaction: Transaction, principal_id: str, transition: Transition) -> AppliedTransition:
    """Write one transition and run its in-transaction effects; return the post-commit ones."""
    reply_messages.persist(transaction, principal_id, transition)
    post_commit: list[PostCommitEffect] = []
    for effect in transition.effects:
        _run(transaction, principal_id, transition, effect, post_commit)
    return AppliedTransition(transition=transition, post_commit=tuple(post_commit))


def _run(
    transaction: Transaction,
    principal_id: str,
    transition: Transition,
    effect: Effect,
    post_commit: list[PostCommitEffect],
) -> None:
    match effect:
        case SettleSources(span_id=span_id):
            span = span_for(transaction, principal_id, transition, span_id)
            journal.settle_many(transaction, principal_id, span.sources.pending)
        case CancelSpan() | WakeApproval():
            post_commit.append(effect)
        case TransferStop():
            # The turn-record copy is written by the caller that owns the ledger write.
            pass
        case _:
            msg = f"Reply effect {effect!r} has no transactional owner yet"
            raise NotImplementedError(msg)


def decide_on_span(
    transaction: Transaction,
    principal_id: str,
    *,
    reply_id: str,
    span_id: str,
    decide: Decide,
) -> AppliedTransition:
    """Lock one reply, read the named span, apply the rule, and write what it decided."""
    reply = reply_messages.lock(transaction, principal_id, reply_id)
    span = reply_spans.load(transaction, principal_id, span_id)
    if reply is None or span is None:
        msg = f"Reply {reply_id} or span {span_id} does not exist"
        raise RuntimeError(msg)
    return apply(transaction, principal_id, decide(reply, span))


# ---------------------------------------------------------------------------
# Claims


@dataclass(frozen=True, slots=True)
class ClaimLookup:
    """Where a claim looks for the reply it continues (PR-1.md §6.1)."""

    interactive_span_id: str | None = None
    existing_event_id: str | None = None
    edit_receipt_order: int | None = None


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
        # A removed reply is never continued; main answers again in a new one.
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
        edit_receipt_order=lookup.edit_receipt_order,
    )
    if reply is None:
        admitted = journal.admitted_membership_owner(transaction, principal_id, request.delivery_id)
        if admitted is not None:
            request = replace(request, membership_epoch=admitted[1])
    return apply(transaction, principal_id, rl.claim(request, context))


# ---------------------------------------------------------------------------
# Stop


def record_stop(
    transaction: Transaction,
    principal_id: str,
    *,
    event_id: str,
    receipt_order: int,
    newer_edit: bool,
) -> AppliedTransition | None:
    """Record a Stop on the reply bound to one event; ``None`` when no reply is bound to it."""
    found = reply_messages.for_event(transaction, principal_id, event_id)
    if found is None:
        return None
    reply = reply_messages.lock(transaction, principal_id, found.reply_id)
    assert reply is not None
    span_id = reply.current_span_id or reply.last_span_id
    span = reply_spans.load(transaction, principal_id, span_id)
    active_generation = reply_messages.active_generation(transaction, principal_id)
    span_live = (
        span is not None
        and not span.ended
        and span.span_id == reply.current_span_id
        and span.bot_generation == active_generation
    )
    return apply(
        transaction,
        principal_id,
        rl.stop(
            reply,
            span,
            rl.StopFacts(receipt_order=receipt_order, newer_edit=newer_edit, span_live=span_live),
            now_ns=time.time_ns(),
        ),
    )


# ---------------------------------------------------------------------------
# Rows


def edit_delivery_id(span_delivery_id: str, sequence: int) -> str:
    """Return the derived delivery id of a reply's non-terminal durable write (DESIGN.md §7.2)."""
    return f"{span_delivery_id}:edit:{sequence}"


@dataclass(frozen=True, slots=True)
class ReplyCreation:
    """A reply whose first row creates it: an interactive selection's acknowledgement (PR-1.md §6.1)."""

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

    @property
    def transition(self) -> Transition:
        """Return the transition the rule decided."""
        return self.applied.transition


def row_placeholder_only(delivery: MatrixDelivery) -> bool:
    """Return whether one reply row shows only the reply's placeholder."""
    facts = (delivery.result or {}).get(REPLY_ROW_RESULT_KEY)
    return isinstance(facts, dict) and facts.get("placeholder_only") is True


def row_result(result: Mapping[str, object] | None, *, placeholder_only: bool) -> dict[str, object]:
    """Return a reply row's local result, carrying what it shows."""
    return {**(result or {}), REPLY_ROW_RESULT_KEY: {"placeholder_only": placeholder_only}}


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
        placeholder_only=row_placeholder_only(delivery),
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
    if delivery.edits_event_id is not None or bound.event_id != event_id:
        return applied
    pending_stop = reply_messages.take_pending_stop(transaction, principal_id, event_id)
    if pending_stop is None:
        return applied
    receipt_order, _room_id = pending_stop
    current = (
        None if bound.current_span_id is None else reply_spans.load(transaction, principal_id, bound.current_span_id)
    )
    stopped = apply(
        transaction,
        principal_id,
        rl.apply_pending_stop(
            bound,
            current,
            rl.StopFacts(
                receipt_order=receipt_order,
                newer_edit=False,
                span_live=current is not None and not current.ended,
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
    return apply(
        transaction,
        principal_id,
        rl.write_failed(reply, span, rl.FailedWrite(_write_facts(delivery), reason), now_ns=time.time_ns()),
    )


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

    async def decide(self, *, reply_id: str, span_id: str, decide: Decide) -> AppliedTransition:
        """Apply one rule to a locked reply and its span, in a transaction of its own."""
        return await self._backend.write(
            lambda transaction: decide_on_span(
                transaction,
                self._principal_id,
                reply_id=reply_id,
                span_id=span_id,
                decide=decide,
            ),
        )

    async def update(self, reply_id: str, decide: Callable[[Reply], Transition]) -> AppliedTransition | None:
        """Apply one reply-only rule to the locked reply, in a transaction of its own."""

        def write(transaction: Transaction) -> AppliedTransition | None:
            reply = reply_messages.lock(transaction, self._principal_id, reply_id)
            return None if reply is None else apply(transaction, self._principal_id, decide(reply))

        return await self._backend.write(write)

    async def record_stop(self, *, event_id: str, receipt_order: int, newer_edit: bool) -> AppliedTransition | None:
        """Record a Stop on the reply bound to one event."""
        return await self._backend.write(
            lambda transaction: record_stop(
                transaction,
                self._principal_id,
                event_id=event_id,
                receipt_order=receipt_order,
                newer_edit=newer_edit,
            ),
        )

    async def has_unresolved_rows(self, reply_id: str) -> bool:
        """Return whether any durable write of the reply has an unknown Matrix outcome."""
        return await self._backend.read(
            lambda transaction: has_unresolved_rows(transaction, self._principal_id, reply_id),
        )
