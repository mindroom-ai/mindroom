"""Pure state transitions of durable reply messages (docs/architecture/reply-messages.md).

A reply is one agent or team answer, shown as one Matrix event, that several
execution spans write over time. This module decides, from a reply record, the
spans an event names, and explicit inputs, what changes: the reply's state, a
span's write-once outcome, which durable write a transition owes, and which
effects the caller runs inside the same transaction or after it commits.

Nothing here performs I/O or reads a clock. Presentations cross this boundary
as opaque JSON strings: callers render and prepare payloads before the
transaction, and every prepared payload names the reply ``revision`` it was
rendered for, so a transition can refuse content that a Stop, deletion, or
departure committed meanwhile has made wrong (``Outcome.RECOMPUTE``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Vocabulary


class ReplyState(StrEnum):
    """Lifecycle state of one reply."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    GONE = "gone"


_TERMINAL_STATES = frozenset({ReplyState.COMPLETED, ReplyState.CANCELLED, ReplyState.FAILED, ReplyState.GONE})


class SpanKind(StrEnum):
    """Which executor claimed a reply."""

    TURN = "turn"
    REPLAY = "replay"
    APPROVAL_RESUME = "approval_resume"
    REGENERATION = "regeneration"


class SpanOutcome(StrEnum):
    """How one span ended; written once."""

    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    RESTORED = "restored"
    RELEASED = "released"
    SUPERSEDED = "superseded"
    LOST = "lost"


# Outcomes after which the span's sources are still pending, so a retry or
# replay claims the reply again.
_SOURCES_PENDING_OUTCOMES = frozenset({SpanOutcome.RELEASED, SpanOutcome.SUPERSEDED, SpanOutcome.LOST})


class Outcome(StrEnum):
    """What one event did to the records."""

    APPLIED = "applied"
    STALE = "stale"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred"
    # The prepared payload was rendered for an older revision; nothing was written.
    RECOMPUTE = "recompute"
    # A Stop recorded on the reply must end the span through its Stop path first.
    STOPPED = "stopped"


class WriteStage(StrEnum):
    """Outbox stage of one durable reply write."""

    INITIAL = "initial"
    FINAL = "final"
    EDIT = "edit"


# Note kinds are owned by ``reply_presentation``; the lifecycle names them by value.
_NOTE_CANCELLED = "cancelled"
_NOTE_RESTART = "restart"
_NOTE_INTERRUPTED = "interrupted"
_NOTE_DELIVERY_FAILED = "delivery_failed"
_NOTE_APPROVAL_FAILED = "approval_failed"
_NOTE_ERROR = "error"

FailureDisposition = Literal["cancelled_by_user", "failed"]

# ---------------------------------------------------------------------------
# Records


@dataclass(frozen=True, slots=True)
class SpanSources:
    """The immutable sources a span answers (``ResponseSources``)."""

    pending: tuple[str, ...]
    logical: tuple[str, ...]
    discovery: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rollback:
    """What a regeneration restores when it ends before it recorded any write Matrix may show.

    The Stop button is not part of it: a button belongs to the span that sent
    it and is removed when the reply stops being active (I8).
    """

    presentation: str
    frozen_display: str | None
    # A finished state: only a finished answer is ever restored.
    state: ReplyState
    # What the room may show before the regeneration wrote, and that write's sequence.
    possibly_shown: str | None = None
    possibly_shown_seq: int | None = None


@dataclass(frozen=True, slots=True)
class Span:
    """One claim on a reply by one executor."""

    span_id: str
    reply_id: str
    kind: SpanKind
    delivery_id: str
    sources: SpanSources
    bot_generation: str
    claimed_at_ns: int
    # The reply's write sequence at claim; any acknowledged write above it is this span's or later.
    base_sequence: int
    approval_id: str | None = None
    rollback: Rollback | None = None
    outcome: SpanOutcome | None = None
    ended_at_ns: int | None = None

    @property
    def ended(self) -> bool:
        """Return whether the span's outcome is written."""
        return self.outcome is not None


@dataclass(frozen=True, slots=True)
class OwedWrite:
    """A note a reply-authored transition decided but could not carry a payload for.

    The deciding transition already settled whatever sources it ended, so the
    write never hands sources over. Its stage is chosen when it is enqueued:
    the span's ``FINAL`` while its delivery id has none, an ``edit`` row
    otherwise.
    """

    span_id: str
    note: str
    # Text for notes whose wording is not fixed by their kind (errors).
    text: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """Durable record of one reply."""

    reply_id: str
    entity_name: str
    room_id: str
    thread_id: str | None
    membership_epoch: int
    state: ReplyState
    last_span_id: str
    presentation: str
    revision: int
    reply_sequence: int
    created_at_ns: int
    updated_at_ns: int
    event_id: str | None = None
    current_span_id: str | None = None
    frozen_display: str | None = None
    possibly_shown: str | None = None
    possibly_shown_seq: int | None = None
    confirmed_seq: int | None = None
    placeholder_only: bool = False
    stop_receipt_order: int | None = None
    stop_applied_receipt_order: int | None = None
    stop_button_event_id: str | None = None
    redaction_pending: tuple[str, ...] = ()
    approval_id: str | None = None
    owed_write: OwedWrite | None = None

    @property
    def terminal(self) -> bool:
        """Return whether the reply reached a terminal state."""
        return self.state in _TERMINAL_STATES

    @property
    def unapplied_stop(self) -> bool:
        """Return whether a recorded Stop still has to reach the reply."""
        return self.stop_receipt_order is not None and (
            self.stop_applied_receipt_order is None or self.stop_applied_receipt_order < self.stop_receipt_order
        )

    @property
    def confirmed(self) -> bool:
        """Return whether Matrix acknowledged the reply's latest write."""
        if self.possibly_shown_seq is None:
            return True
        return self.confirmed_seq is not None and self.confirmed_seq >= self.possibly_shown_seq


# ---------------------------------------------------------------------------
# Effects


@dataclass(frozen=True, slots=True)
class SettleSources:
    """In the transaction: settle every pending source of the span, which records its turn answered."""

    span_id: str
    # False when nothing answered the sources, deleted or terminal without an answer: their turn stays unanswered.
    answered: bool = True


@dataclass(frozen=True, slots=True)
class FenceApproval:
    """In the transaction: fence the reply's approval continuation for failure."""

    approval_id: str
    disposition: FailureDisposition


@dataclass(frozen=True, slots=True)
class CancelSpan:
    """After commit: cancel exactly this span's task, if it is running here."""

    span_id: str
    # Whether a user's Stop cancels it, rather than the reply moving on without it.
    by_stop: bool = False


@dataclass(frozen=True, slots=True)
class WakeApproval:
    """After commit: wake the approval source so its failure settlement runs."""

    approval_id: str


type Effect = SettleSources | FenceApproval | CancelSpan | WakeApproval


@dataclass(frozen=True, slots=True)
class _RowIntent:
    """A durable write the transaction enqueues with the caller's prepared payload."""

    stage: WriteStage
    sequence: int
    span_id: str


@dataclass(frozen=True, slots=True)
class Transition:
    """The result of one event: what to write and what to run."""

    outcome: Outcome
    reply: Reply | None
    spans: tuple[Span, ...] = ()
    effects: tuple[Effect, ...] = ()
    row: _RowIntent | None = None
    # Set by claims that took effect.
    claimed: Span | None = None
    # Why the rules ended this reply as an event they do not model; the store logs it.
    unmodeled: str | None = None

    @property
    def applied(self) -> bool:
        """Return whether the event changed the records."""
        return self.outcome is Outcome.APPLIED


def _unchanged(outcome: Outcome, reply: Reply | None) -> Transition:
    return Transition(outcome=outcome, reply=reply)


# ---------------------------------------------------------------------------
# Shared helpers


def _end(span: Span, outcome: SpanOutcome, now_ns: int) -> Span:
    """End a span; one that already ended keeps its first outcome."""
    if span.ended:
        return span
    return replace(span, outcome=outcome, ended_at_ns=now_ns)


def _clear_current(reply: Reply, span_id: str) -> Reply:
    if reply.current_span_id != span_id:
        return reply
    cleared = replace(reply, current_span_id=None)
    # A reply that waited in place keeps its Stop button only while its span runs (I8).
    return cleared if cleared.state is ReplyState.ACTIVE else _leaves_active(cleared, cleared.state)


def _bump(reply: Reply, now_ns: int, **changes: object) -> Reply:
    """Apply changes that alter what a payload would contain."""
    return replace(reply, revision=reply.revision + 1, updated_at_ns=now_ns, **changes)  # type: ignore[arg-type]


def _touch(reply: Reply, now_ns: int, **changes: object) -> Reply:
    """Apply bookkeeping changes that leave every payload as it was."""
    return replace(reply, updated_at_ns=now_ns, **changes)  # type: ignore[arg-type]


def _with_redactions(reply: Reply, *event_ids: str | None) -> Reply:
    pending = list(reply.redaction_pending)
    for event_id in event_ids:
        if event_id is not None and event_id not in pending:
            pending.append(event_id)
    return replace(reply, redaction_pending=tuple(pending))


def _visible_event_ids(reply: Reply) -> tuple[str, ...]:
    return () if reply.event_id is None else (reply.event_id,)


def _leaves_active(reply: Reply, new_state: ReplyState) -> Reply:
    """Queue the Stop button's redaction when the reply stops being active (I8).

    A span that waits in place for approval keeps running, so its reply keeps
    the button through the wait (``pause(in_place)`` keeps it).
    """
    if new_state is not ReplyState.ACTIVE and reply.stop_button_event_id:
        return replace(_with_redactions(reply, reply.stop_button_event_id), stop_button_event_id=None)
    return reply


def _set_state(reply: Reply, state: ReplyState, now_ns: int, **changes: object) -> Reply:
    return _bump(_leaves_active(reply, state), now_ns, state=state, **changes)


def _stop_applied(reply: Reply) -> Reply:
    return replace(reply, stop_applied_receipt_order=reply.stop_receipt_order)


def _settle_sources(reply: Reply, span: Span, *, answered: bool = True) -> tuple[Effect, ...]:
    """Settle a span's sources when it ends, unless an approval continuation owns them.

    A resume's sources are its continuation's, and so are those of a span
    that waits in place: the continuation's finish settles them, and until
    then a wake that redispatches them is what runs its failure settlement.
    Once a release handed them back to replay, the reply settles them as any.
    """
    if reply.approval_id is not None:
        return ()
    return (SettleSources(span.span_id, answered=answered),)


def _next_sequence(reply: Reply) -> tuple[Reply, int]:
    sequence = reply.reply_sequence + 1
    return replace(reply, reply_sequence=sequence), sequence


def _row(
    reply: Reply,
    span: Span,
    stage: WriteStage,
    *,
    shown: str,
) -> tuple[Reply, _RowIntent]:
    """Allocate the next write sequence for one durable row and record what it may show."""
    reply, sequence = _next_sequence(reply)
    reply = replace(reply, possibly_shown=shown, possibly_shown_seq=sequence)
    return reply, _RowIntent(stage=stage, sequence=sequence, span_id=span.span_id)


def _stale_span(reply: Reply, span: Span) -> Transition | None:
    """Refuse span-authored events from a span that is no longer current (I1)."""
    if span.ended or reply.current_span_id != span.span_id:
        effects: tuple[Effect, ...] = () if span.ended else (CancelSpan(span.span_id),)
        return Transition(outcome=Outcome.STALE, reply=reply, effects=effects)
    return None


def _check_revision(reply: Reply, prepared_revision: int) -> Transition | None:
    if prepared_revision != reply.revision:
        return _unchanged(Outcome.RECOMPUTE, reply)
    return None


def _wrote_anything(reply: Reply, span: Span) -> bool:
    """Return whether the span recorded any write, which Matrix may show though no confirmation says so yet."""
    return reply.possibly_shown_seq is not None and reply.possibly_shown_seq > span.base_sequence


def _kept_answer(
    reply: Reply,
    span: Span,
    *,
    before_sequence: int | None = None,
) -> Rollback | None:
    """Return the answer a regeneration abandoned now leaves as the room shows it, if it leaves one.

    Only when it recorded no write Matrix may show: an unacknowledged write
    may have landed, so it counts. ``before_sequence`` counts only the writes
    before a row Matrix refused for good. A regeneration claimed again after
    an attempt that wrote carries no rollback.
    """
    rollback = span.rollback
    if span.kind is not SpanKind.REGENERATION or rollback is None:
        return None
    if before_sequence is not None:
        return rollback if before_sequence - 1 <= span.base_sequence else None
    return None if _wrote_anything(reply, span) else rollback


def _rollback_after(reply: Reply, last: Span) -> Rollback | None:
    """Return the rollback a regeneration claimed again carries: none once an earlier attempt wrote."""
    return None if _wrote_anything(reply, last) else last.rollback


def _restore(reply: Reply, span: Span, rollback: Rollback, now_ns: int) -> Reply:
    """Restore a regeneration's rollback snapshot, a finished answer.

    The old answer stands, so a Stop recorded during the regeneration is satisfied by it.
    """
    # A row Matrix refused for good shows nothing: the room shows what it did before the regeneration.
    restored = replace(
        _stop_applied(_clear_current(reply, span.span_id)),
        possibly_shown=rollback.possibly_shown,
        possibly_shown_seq=rollback.possibly_shown_seq,
    )
    return _set_state(
        restored,
        rollback.state,
        now_ns,
        presentation=rollback.presentation,
        frozen_display=rollback.frozen_display,
    )


def _restored(reply: Reply, span: Span, rollback: Rollback, now_ns: int, *effects: Effect) -> Transition:
    """Abandon a regeneration that showed nothing: the finished answer it was replacing stands."""
    spans = () if span.ended else (_end(span, SpanOutcome.RESTORED, now_ns),)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_restore(reply, span, rollback, now_ns),
        spans=spans,
        effects=effects,
    )


def _unmodeled(reply: Reply, span: Span | None, *, reason: str, now_ns: int) -> Transition:
    """End a reply for an event the rules do not model: failed with the error note, nothing left running or pending.

    Nothing raises, so no Matrix acknowledgement, deletion, or room lane is
    held up; the store logs ``reason``. An approval that holds the reply fails,
    and its settlement settles the sources it holds.
    """
    if reply.terminal:
        return replace(_unchanged(Outcome.DUPLICATE, reply), unmodeled=reason)
    spans: tuple[Span, ...] = ()
    effects: list[Effect] = []
    updated = reply
    if span is not None and not span.ended and span.span_id == reply.current_span_id:
        spans = (_end(span, SpanOutcome.FAILED, now_ns),)
        updated = _clear_current(updated, span.span_id)
        effects.append(CancelSpan(span.span_id))
    if reply.approval_id is not None:
        effects += [FenceApproval(reply.approval_id, "failed"), WakeApproval(reply.approval_id)]
    else:
        effects.append(SettleSources(reply.last_span_id))
    owed = OwedWrite(reply.last_span_id, _NOTE_ERROR)
    updated = _set_state(_stop_applied(updated), ReplyState.FAILED, now_ns, owed_write=owed)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=spans, effects=tuple(effects), unmodeled=reason)


def _unmodeled_claim(reply: Reply | None, span: Span | None, *, reason: str, now_ns: int) -> Transition:
    """A claim the rules do not model opens no span; the reply it reaches ends as ``unmodeled`` ends it."""
    if reply is None:
        return Transition(outcome=Outcome.DUPLICATE, reply=None, unmodeled=reason)
    return _unmodeled(reply, span, reason=reason, now_ns=now_ns)


# ---------------------------------------------------------------------------
# Claims


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """One executor's request to claim a reply, after the first source gate passed."""

    span_id: str
    delivery_id: str
    sources: SpanSources
    bot_generation: str
    now_ns: int
    # The membership the claim's delivery was admitted in, which a new or regenerated reply writes in.
    membership_epoch: int
    # Used only when the claim creates a reply.
    new_reply_id: str
    entity_name: str
    room_id: str
    thread_id: str | None
    empty_presentation: str
    # Set for edit regenerations: the edit event that drives this run.
    driving_edit_id: str | None = None
    # Set for approval resumes, claimed with the continuation.
    approval_id: str | None = None
    # Set when an interactive selection created this span at its acknowledgement.
    interactive_span_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimContext:
    """Facts the journal layer reads inside the claim transaction."""

    reply: Reply | None
    last_span: Span | None
    current_span: Span | None
    interactive_span: Span | None
    # An enqueued reply row whose Matrix outcome is unknown.
    durable_write_debt: bool
    active_generation: str


def _new_reply(request: ClaimRequest, *, state: ReplyState, event_id: str | None = None) -> Reply:
    return Reply(
        reply_id=request.new_reply_id,
        entity_name=request.entity_name,
        room_id=request.room_id,
        thread_id=request.thread_id,
        membership_epoch=request.membership_epoch,
        state=state,
        last_span_id=request.span_id,
        presentation=request.empty_presentation,
        revision=0,
        reply_sequence=0,
        created_at_ns=request.now_ns,
        updated_at_ns=request.now_ns,
        event_id=event_id,
    )


def _new_span(
    request: ClaimRequest,
    reply: Reply,
    kind: SpanKind,
    *,
    rollback: Rollback | None = None,
) -> Span:
    return Span(
        span_id=request.span_id,
        reply_id=reply.reply_id,
        kind=kind,
        delivery_id=request.delivery_id,
        sources=request.sources,
        bot_generation=request.bot_generation,
        claimed_at_ns=request.now_ns,
        base_sequence=reply.reply_sequence,
        approval_id=request.approval_id,
        rollback=rollback,
    )


def claim_blocked(reply: Reply, *, durable_write_debt: bool) -> bool:
    """Return whether a claim on the reply must wait, and retry once its earlier writes resolve.

    Earlier writes still unresolved or a note still owed would be overtaken by the new span.
    """
    return durable_write_debt or reply.owed_write is not None


def _make_current(reply: Reply, span: Span, now_ns: int, **changes: object) -> Reply:
    return _touch(reply, now_ns, current_span_id=span.span_id, last_span_id=span.span_id, **changes)


def _rollback_of(reply: Reply) -> Rollback:
    return Rollback(
        presentation=reply.presentation,
        frozen_display=reply.frozen_display,
        state=reply.state,
        possibly_shown=reply.possibly_shown,
        possibly_shown_seq=reply.possibly_shown_seq,
    )


def claim(request: ClaimRequest, context: ClaimContext) -> Transition:  # noqa: C901, PLR0911, PLR0912
    """Claim a reply for one span."""
    reply = context.reply
    if request.bot_generation != context.active_generation:
        # A bot instance that no longer owns the principal's replies; the one
        # that does replays these sources.
        return _unchanged(Outcome.STALE, reply)
    changed: list[Span] = []
    effects: list[Effect] = []
    if reply is not None and context.current_span is not None:
        current = context.current_span
        if current.bot_generation == context.active_generation:
            # Claims run under the conversation lock, so a live span here is unmodeled.
            return _unmodeled_claim(reply, current, reason="claim_with_live_span", now_ns=request.now_ns)
        # A span an older bot instance left current never ends by itself.
        lost = _end(current, SpanOutcome.LOST, request.now_ns)
        changed.append(lost)
        reply = _clear_current(reply, current.span_id)
        if context.last_span is not None and context.last_span.span_id == lost.span_id:
            context = replace(context, last_span=lost)

    if reply is not None and claim_blocked(reply, durable_write_debt=context.durable_write_debt):
        # Waiting under the conversation lock would block the reply's own
        # sends or the approval's settlement; their resolution retries these
        # sources instead.
        return Transition(outcome=Outcome.DEFERRED, reply=reply if changed else context.reply, spans=tuple(changed))

    def claimed(transition_reply: Reply, span: Span, *extra_spans: Span) -> Transition:
        return Transition(
            outcome=Outcome.APPLIED,
            reply=transition_reply,
            spans=(*changed, *extra_spans, span),
            effects=tuple(effects),
            claimed=span,
        )

    if request.approval_id is not None:
        if reply is None or reply.state is not ReplyState.PAUSED or reply.approval_id != request.approval_id:
            ended = _unmodeled_claim(
                reply,
                context.last_span,
                reason="resume_without_paused_reply",
                now_ns=request.now_ns,
            )
            if reply is not None and reply.approval_id == request.approval_id:
                return ended
            # The approval that asked to resume fails too, so its recovery does not retry it forever.
            failing = (FenceApproval(request.approval_id, "failed"), WakeApproval(request.approval_id))
            return replace(ended, effects=(*ended.effects, *failing))
        span = _new_span(request, reply, SpanKind.APPROVAL_RESUME)
        return claimed(_make_current(_set_state(reply, ReplyState.ACTIVE, request.now_ns), span, request.now_ns), span)

    interactive = context.interactive_span
    if request.interactive_span_id is not None and interactive is not None and reply is not None:
        if interactive.outcome is None and not reply.terminal:
            return claimed(_make_current(reply, interactive, request.now_ns), interactive)
        if interactive.outcome is None:
            # A Stop ended the selection's reply before its claim: nothing runs for it.
            return _unchanged(Outcome.DUPLICATE, reply)
        if interactive.outcome is not SpanOutcome.LOST:
            # A Stop that reached the reply first ended the selection's span;
            # Stops do not wait for the conversation lock claims hold.
            return _unchanged(Outcome.DUPLICATE, reply)
        # The acknowledgement's bot instance is gone: the selection continues as a replay.
        span = _new_span(request, reply, SpanKind.REPLAY)
        return claimed(_make_current(reply, span, request.now_ns), span)

    if reply is None:
        if request.driving_edit_id is not None:
            # The regenerator regenerates only a reply it found.
            return _unmodeled_claim(None, None, reason="regeneration_without_reply", now_ns=request.now_ns)
        created = _new_reply(request, state=ReplyState.ACTIVE)
        span = _new_span(request, created, SpanKind.TURN)
        return claimed(_make_current(created, span, request.now_ns), span)

    last = context.last_span
    if request.driving_edit_id is not None and (last is None or request.driving_edit_id != last.delivery_id):
        return _regeneration(request, reply, last, claimed)

    if (
        request.driving_edit_id is not None
        and last is not None
        and last.ended
        and last.outcome not in {SpanOutcome.RELEASED, SpanOutcome.LOST, SpanOutcome.SUPERSEDED}
    ):
        # A retry of the edit the last span already answered, as a sync
        # restart retries a regeneration that finished: nothing runs again.
        return _unchanged(Outcome.DUPLICATE, reply)
    if reply.terminal:
        # A Stop, deletion, or departure that does not wait for the conversation
        # lock ended the reply between the source gate and this claim, or this
        # retry's: nothing runs for it, and that ending owns its sources.
        return _unchanged(Outcome.DUPLICATE, reply)
    if reply.state is not ReplyState.ACTIVE or reply.current_span_id is not None or last is None or not last.ended:
        return _unmodeled_claim(reply, last, reason="reply_not_reclaimable", now_ns=request.now_ns)
    if last.outcome is SpanOutcome.SUPERSEDED:
        span = _new_span(request, reply, last.kind, rollback=_rollback_after(reply, last))
        return claimed(_make_current(reply, span, request.now_ns), span)
    if last.outcome in {SpanOutcome.RELEASED, SpanOutcome.LOST}:
        if last.kind is SpanKind.REGENERATION:
            span = _new_span(request, reply, SpanKind.REGENERATION, rollback=_rollback_after(reply, last))
        else:
            span = _new_span(request, reply, SpanKind.REPLAY)
        return claimed(_make_current(reply, span, request.now_ns), span)
    return _unmodeled_claim(reply, last, reason="last_span_not_reclaimable", now_ns=request.now_ns)


def _regeneration(
    request: ClaimRequest,
    reply: Reply,
    last: Span | None,
    claimed: Callable[[Reply, Span], Transition],
) -> Transition:
    """Claim a new edit's regeneration of an unheld reply nothing runs for.

    The regenerator stops a running reply before it claims. A reply an
    approval holds, or one that is gone, regenerates nothing: the edit only
    changed the message. Only a finished answer is kept to restore; a
    regeneration that never answered passes its own on. An interrupted turn's
    own replay finds its turn answered once the regeneration answers it.
    """
    if reply.state is ReplyState.GONE or reply.approval_id is not None:
        return _unchanged(Outcome.DUPLICATE, reply)
    if last is not None and last.kind is SpanKind.REGENERATION and last.outcome in _SOURCES_PENDING_OUTCOMES:
        rollback = _rollback_after(reply, last)
    else:
        rollback = _rollback_of(reply) if reply.terminal else None
    # Its rows belong to the membership its edit arrived in, which a leave and rejoin moved on.
    updated = replace(reply, membership_epoch=request.membership_epoch)
    span = _new_span(request, updated, SpanKind.REGENERATION, rollback=rollback)
    # A regeneration replaces the whole answer; the old display lives in the rollback.
    regenerating = _set_state(updated, ReplyState.ACTIVE, request.now_ns, frozen_display=None)
    return claimed(_make_current(regenerating, span, request.now_ns), span)


def interactive_acknowledgement(request: ClaimRequest, *, shown: str) -> Transition:
    """Create a selection's reply, its first span not yet current, and the acknowledgement's ``INITIAL`` row."""
    created = replace(_new_reply(request, state=ReplyState.ACTIVE), placeholder_only=True)
    span = _new_span(request, created, SpanKind.TURN)
    created, row = _row(created, span, WriteStage.INITIAL, shown=shown)
    return Transition(outcome=Outcome.APPLIED, reply=created, spans=(span,), row=row)


# ---------------------------------------------------------------------------
# Writes


@dataclass(frozen=True, slots=True)
class WriteFacts:
    """A durable row whose Matrix outcome just became known."""

    stage: WriteStage
    sequence: int
    span_id: str
    # The row created the reply's event rather than editing it.
    creates_event: bool
    # The row shows only the placeholder.
    placeholder_only: bool


@dataclass(frozen=True, slots=True)
class ProgressConfirmation:
    """Matrix accepted the span's latest direct progress edit."""

    # The event the edit wrote into, or created when it was the first send.
    event_id: str
    # Whether that edit showed only the placeholder.
    placeholder_only: bool


def confirm_progress(reply: Reply, confirmation: ProgressConfirmation | None) -> Reply:
    """Record that the latest direct edit landed, as the next write-ahead or durable transition does."""
    if confirmation is None or reply.possibly_shown_seq is None:
        return reply
    updated = replace(
        reply,
        confirmed_seq=max(reply.confirmed_seq or 0, reply.possibly_shown_seq),
        placeholder_only=confirmation.placeholder_only,
    )
    if updated.event_id is None:
        updated = replace(updated, event_id=confirmation.event_id)
    return updated


def write_ahead(
    reply: Reply,
    span: Span,
    *,
    shown: str,
    previous: ProgressConfirmation | None,
    active_generation: str,
    durable_write_debt: bool,
    now_ns: int,
) -> Transition:
    """Record the presentation of the next direct progress edit before it is sent.

    While an earlier durable write of the reply is unresolved the edit waits:
    sent later, that write would replace newer progress with what it shows.
    """
    if span.bot_generation != active_generation:
        return _unchanged(Outcome.STALE, reply)
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if durable_write_debt:
        return _unchanged(Outcome.DEFERRED, reply)
    updated, sequence = _next_sequence(confirm_progress(reply, previous))
    updated = _touch(updated, now_ns, possibly_shown=shown, possibly_shown_seq=sequence)
    return Transition(outcome=Outcome.APPLIED, reply=updated)


def enqueue_initial(
    reply: Reply,
    span: Span,
    *,
    shown: str,
    placeholder_only: bool,
    prepared_revision: int,
    now_ns: int,
) -> Transition:
    """Enqueue the reply's first visible create as the span's INITIAL row."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if reply.event_id is not None:
        return _unmodeled(reply, span, reason="second_initial_row", now_ns=now_ns)
    recompute = _check_revision(reply, prepared_revision)
    if recompute is not None:
        return recompute
    updated, row = _row(reply, span, WriteStage.INITIAL, shown=shown)
    updated = _touch(updated, now_ns, placeholder_only=placeholder_only)
    return Transition(outcome=Outcome.APPLIED, reply=updated, row=row)


def write_acknowledged(
    reply: Reply,
    write: WriteFacts,
    *,
    event_id: str,
    membership_current: bool,
    now_ns: int,
) -> Transition:
    """Apply Matrix's acknowledgement of one durable row."""
    updated = reply
    if write.creates_event:
        if reply.event_id is not None and reply.event_id != event_id:
            # Two creates of one reply: the first binding stands and the stray event goes.
            stray = _touch(_with_redactions(reply, event_id), now_ns)
            return Transition(outcome=Outcome.APPLIED, reply=stray, unmodeled="second_create_acknowledged")
        if reply.event_id is None:
            updated = replace(updated, event_id=event_id)
            if reply.state is ReplyState.GONE and membership_current:
                # Created after the reply was given up: the late event is removed,
                # unless the room was left, which drops everything owed to it.
                updated = _with_redactions(updated, event_id)
    if updated.confirmed_seq is None or write.sequence > updated.confirmed_seq:
        updated = replace(updated, confirmed_seq=write.sequence)
        if write.sequence >= (updated.possibly_shown_seq or 0):
            updated = replace(updated, placeholder_only=write.placeholder_only)
    if updated == reply:
        return _unchanged(Outcome.DUPLICATE, reply)
    return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns))


@dataclass(frozen=True, slots=True)
class FailedWrite:
    """A durable row Matrix refused permanently."""

    write: WriteFacts
    reason: str


def write_failed(reply: Reply, span: Span, failure: FailedWrite, *, now_ns: int) -> Transition:
    """Apply a permanent row failure."""
    write = failure.write
    if write.stage is WriteStage.FINAL:
        return _terminal_write_failed(
            reply,
            span,
            first_create=reply.event_id is None,
            sequence=write.sequence,
            now_ns=now_ns,
        )
    if (
        reply.state is ReplyState.PAUSED
        and reply.approval_id is not None
        and span.span_id == reply.last_span_id
        and write.sequence == reply.possibly_shown_seq
    ):
        return _failed_pause(reply, span, now_ns=now_ns)
    # A refused create leaves the span running and a later row creates the
    # event; a refused non-terminal note leaves the reply as it was.
    return _unchanged(Outcome.APPLIED, reply)


def _failed_pause(reply: Reply, span: Span, *, now_ns: int) -> Transition:
    """A pause nobody saw cannot hold its approval: fence it like a failed handoff."""
    assert reply.approval_id is not None
    spans: tuple[Span, ...] = ()
    effects: list[Effect] = []
    updated = reply
    if not span.ended:
        # A response-local approval wait is still waiting on this pause; its
        # turn ends here, with the failure note as its answer. Its sources stay
        # with the continuation, whose failure settlement settles them.
        spans = (_end(span, SpanOutcome.FAILED, now_ns),)
        updated = _clear_current(updated, span.span_id)
        effects.append(CancelSpan(span.span_id))
    if reply.unapplied_stop:
        owed = OwedWrite(span.span_id, _NOTE_CANCELLED)
        updated = _set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns)
        disposition: FailureDisposition = "cancelled_by_user"
    else:
        owed = OwedWrite(span.span_id, _NOTE_APPROVAL_FAILED)
        updated = _set_state(updated, ReplyState.FAILED, now_ns)
        disposition = "failed"
    effects.insert(0, FenceApproval(reply.approval_id, disposition))
    # Its cards are live: the approval runtime's failure settlement expires them.
    effects.append(WakeApproval(reply.approval_id))
    return Transition(
        outcome=Outcome.APPLIED,
        reply=replace(updated, owed_write=owed),
        spans=spans,
        effects=tuple(effects),
    )


def _terminal_write_failed(reply: Reply, span: Span, *, first_create: bool, sequence: int, now_ns: int) -> Transition:
    """A span's terminal row failed for good after the span ended; its outcome stays."""
    if span.span_id != reply.last_span_id:
        # A later span claims the reply only after this row resolved, so this is not its row.
        return replace(_unchanged(Outcome.STALE, reply), unmodeled="refused_row_of_an_older_span")
    if first_create:
        return Transition(outcome=Outcome.APPLIED, reply=_set_state(reply, ReplyState.GONE, now_ns))
    # Its sources settled answered when the row was queued, so no retry of unfinished work can follow:
    # it restores only a finished answer.
    kept = None if reply.placeholder_only else _kept_answer(reply, span, before_sequence=sequence)
    if kept is not None:
        return _restored(reply, span, kept, now_ns)
    # What the reply shows, its placeholder or its progress, would read as unfinished: it says delivery failed instead.
    owed = OwedWrite(span.span_id, _NOTE_DELIVERY_FAILED)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=replace(_set_state(reply, ReplyState.FAILED, now_ns), owed_write=owed),
    )


# ---------------------------------------------------------------------------
# Span-authored terminal events


@dataclass(frozen=True, slots=True)
class TerminalWrite:
    """The prepared terminal row of a span, rendered for one revision and state."""

    shown: str
    prepared_revision: int
    state: ReplyState
    # A frozen display (post-hook text) that later spans continue below.
    frozen_display: str | None = None
    # The span's last direct progress edit, which this durable write confirms.
    confirms: ProgressConfirmation | None = None


def _terminal_row(
    reply: Reply,
    span: Span,
    write: TerminalWrite,
    outcome: SpanOutcome,
    now_ns: int,
    *,
    stop_applied: bool = False,
    stage: WriteStage = WriteStage.FINAL,
) -> Transition:
    """Enqueue the span's terminal row and end it with the row's status."""
    settles = stage is WriteStage.FINAL and bool(_settle_sources(reply, span))
    updated = _clear_current(confirm_progress(reply, write.confirms), span.span_id)
    if stop_applied:
        updated = _stop_applied(updated)
    # The span's presentation already folded any earlier frozen display, so the
    # row's own post-hook display, or none, is what the reply shows now.
    updated = _set_state(
        updated,
        write.state,
        now_ns,
        presentation=write.shown,
        frozen_display=write.frozen_display,
    )
    shown = write.shown if write.frozen_display is None else write.frozen_display
    updated, row = _row(updated, span, stage, shown=shown)
    effects: tuple[Effect, ...] = (SettleSources(span.span_id),) if settles else ()
    return Transition(
        outcome=Outcome.APPLIED,
        reply=updated,
        spans=(_end(span, outcome, now_ns),),
        effects=effects,
        row=row,
    )


def _expected_terminal_state(reply: Reply, requested: ReplyState) -> ReplyState:
    """Return the state a terminal write must render for, given a possibly unapplied Stop."""
    return ReplyState.CANCELLED if reply.unapplied_stop else requested


def finish(reply: Reply, span: Span, write: TerminalWrite, *, now_ns: int) -> Transition:
    """End a span with its answer."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    expected = _expected_terminal_state(reply, ReplyState.COMPLETED)
    if write.state is not expected:
        if expected is ReplyState.CANCELLED:
            # The span learned the Stop's revision from one of its own writes
            # before the Stop reached it: the payload renders again, cancelled.
            return _unchanged(Outcome.RECOMPUTE, reply)
        return _unmodeled(reply, span, reason="finish_rendered_another_state", now_ns=now_ns)
    if expected is ReplyState.CANCELLED:
        return _terminal_row(reply, span, write, SpanOutcome.CANCELLED, now_ns, stop_applied=True)
    return _terminal_row(reply, span, write, SpanOutcome.COMPLETED, now_ns)


def stopped(  # noqa: PLR0911
    reply: Reply,
    span: Span,
    write: TerminalWrite | None,
    *,
    confirms: ProgressConfirmation | None = None,
    now_ns: int,
) -> Transition:
    """End a span cancelled by its reply's Stop."""
    # The write's own confirmation counts too: an acknowledged progress edit
    # means a regeneration already changed what the room shows.
    reply = confirm_progress(reply, confirms or (None if write is None else write.confirms))
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if not reply.unapplied_stop:
        return _unmodeled(reply, span, reason="stop_never_recorded", now_ns=now_ns)
    if span.kind is SpanKind.APPROVAL_RESUME:
        updated = _clear_current(reply, span.span_id)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_touch(updated, now_ns),
            spans=(_end(span, SpanOutcome.CANCELLED, now_ns),),
        )
    if (kept := _kept_answer(reply, span)) is not None:
        return _restored(reply, span, kept, now_ns, *_settle_sources(reply, span))
    if write is None:
        # An exit that rendered nothing (a release, an error before delivery)
        # still ends the reply as stopped; the cancel note it owes follows.
        cancelled = _set_state(
            _stop_applied(_clear_current(reply, span.span_id)),
            ReplyState.CANCELLED,
            now_ns,
            owed_write=OwedWrite(span.span_id, _NOTE_CANCELLED),
        )
        return Transition(
            outcome=Outcome.APPLIED,
            reply=cancelled,
            spans=(_end(span, SpanOutcome.CANCELLED, now_ns),),
            effects=_settle_sources(reply, span),
        )
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    if write.state is not ReplyState.CANCELLED:
        return _unmodeled(reply, span, reason="stop_rendered_another_state", now_ns=now_ns)
    return _terminal_row(reply, span, write, SpanOutcome.CANCELLED, now_ns, stop_applied=True)


_FailurePhase = Literal["pre_delivery", "delivery"]


def fail(  # noqa: C901, PLR0911
    reply: Reply,
    span: Span,
    write: TerminalWrite | None,
    *,
    phase: _FailurePhase,
    confirms: ProgressConfirmation | None = None,
    now_ns: int,
) -> Transition:
    """End a span that failed.

    ``write`` is the error or interruption note for delivery failures, and for
    a resumed reply's pre-delivery note; it is ``None`` otherwise.
    """
    if span.ended:
        # A terminal row already decided this span; a later error report is the same outcome.
        return _unchanged(Outcome.DUPLICATE, reply)
    reply = confirm_progress(reply, confirms or (None if write is None else write.confirms))
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if reply.unapplied_stop and span.kind is not SpanKind.APPROVAL_RESUME:
        if write is not None and write.state is not ReplyState.CANCELLED:
            # As in ``finish``: the span learned the Stop's revision from one
            # of its own writes before the Stop reached it, so the payload
            # renders again, cancelled.
            return _unchanged(Outcome.RECOMPUTE, reply)
        return stopped(reply, span, write, now_ns=now_ns)
    if span.kind is SpanKind.APPROVAL_RESUME:
        updated = _clear_current(reply, span.span_id)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_touch(updated, now_ns),
            spans=(_end(span, SpanOutcome.FAILED, now_ns),),
        )
    if phase == "delivery" and (kept := _kept_answer(reply, span)) is not None:
        return _restored(reply, span, kept, now_ns, *_settle_sources(reply, span))
    if phase == "pre_delivery":
        # The sources return for a retry, which streams into the kept
        # placeholder; a regeneration's retry runs it again with its rollback,
        # and whatever drops the retry instead puts the earlier answer back.
        updated = _touch(_clear_current(reply, span.span_id), now_ns)
        ended = _end(span, SpanOutcome.RELEASED, now_ns)
        if write is None or _kept_answer(reply, span) is not None:
            # A regeneration that wrote nothing leaves the answer it was replacing as the room shows it.
            return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(ended,))
        recompute = _check_revision(reply, write.prepared_revision)
        if recompute is not None:
            return recompute
        # A resumed reply shows its interruption below the recovered content
        # without settling its sources.
        updated = _bump(updated, now_ns, presentation=write.shown)
        updated, row = _row(updated, span, WriteStage.EDIT, shown=write.shown)
        return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(ended,), row=row)
    if write is None:
        return _unmodeled(reply, span, reason="delivery_failure_without_note", now_ns=now_ns)
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    if write.state is not ReplyState.FAILED:
        return _unmodeled(reply, span, reason="failure_rendered_another_state", now_ns=now_ns)
    return _terminal_row(reply, span, write, SpanOutcome.FAILED, now_ns)


_SuppressReason = Literal["suppressed", "hook_failed"]


def suppress(
    reply: Reply,
    span: Span,
    *,
    reason: _SuppressReason,
    confirms: ProgressConfirmation | None = None,
    now_ns: int,
) -> Transition:
    """End a span whose answer must not be shown."""
    reply = confirm_progress(reply, confirms)
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    outcome = SpanOutcome.SUPPRESSED if reason == "suppressed" else SpanOutcome.FAILED
    # A span that runs for its approval, a resume or one approved in place, leaves the reply to that approval.
    for_approval = span.kind is SpanKind.APPROVAL_RESUME or reply.approval_id is not None
    if reply.unapplied_stop and not for_approval:
        outcome = SpanOutcome.CANCELLED
    updated = _clear_current(reply, span.span_id)
    effects = _settle_sources(reply, span)
    ended = (_end(span, outcome, now_ns),)
    if for_approval:
        # Its failure settlement writes the reply's end, the FINAL that lets the approval finish.
        return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), spans=ended)
    if reply.event_id is None or reply.placeholder_only:
        gone = _with_redactions(_set_state(_stop_applied(updated), ReplyState.GONE, now_ns), *_visible_event_ids(reply))
        return Transition(outcome=Outcome.APPLIED, reply=gone, spans=ended, effects=effects)
    if (kept := _kept_answer(reply, span)) is not None:
        return _restored(reply, span, kept, now_ns, *effects)
    # What the reply showed stays, ended by a note: nothing else would replace the in-progress status it shows.
    if outcome is SpanOutcome.CANCELLED:
        updated, state, note = _stop_applied(updated), ReplyState.CANCELLED, _NOTE_CANCELLED
    else:
        state = ReplyState.CANCELLED if reason == "suppressed" else ReplyState.FAILED
        note = _NOTE_INTERRUPTED
    ending = _set_state(updated, state, now_ns, owed_write=OwedWrite(span.span_id, note))
    return Transition(outcome=Outcome.APPLIED, reply=ending, spans=ended, effects=effects)


def release(
    reply: Reply,
    span: Span,
    *,
    now_ns: int,
    outcome: Literal[SpanOutcome.RELEASED, SpanOutcome.SUPERSEDED] = SpanOutcome.RELEASED,
    confirms: ProgressConfirmation | None = None,
) -> Transition:
    """End a span whose sources stay pending for a retry or replay (``release`` and ``superseded``)."""
    if span.ended:
        return _unchanged(Outcome.DUPLICATE, reply)
    reply = confirm_progress(reply, confirms)
    if reply.unapplied_stop and span.kind is not SpanKind.APPROVAL_RESUME and reply.current_span_id == span.span_id:
        # A recorded Stop outranks a retry: the sources settle and the reply ends stopped.
        return stopped(reply, span, None, now_ns=now_ns)
    updated = _touch(_clear_current(reply, span.span_id), now_ns)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(_end(span, outcome, now_ns),))


# ---------------------------------------------------------------------------
# Approvals


@dataclass(frozen=True, slots=True)
class PauseWrite:
    """The prepared pause row: its stage, what it shows, and the canonical presentation.

    ``stage`` is ``None`` when the reply's create already showed the pause, so
    the pause writes no row of its own.
    """

    shown: str
    prepared_revision: int
    stage: WriteStage | None
    # The span's last direct progress edit, which this durable write confirms.
    confirms: ProgressConfirmation | None = None


def pause(
    reply: Reply,
    span: Span,
    write: PauseWrite,
    *,
    in_place: bool,
    now_ns: int,
) -> Transition:
    """Pause a reply for approval; the continuation created with the pause holds it."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    if reply.unapplied_stop:
        # The Stop path owns this span now; it applies ``stopped`` instead.
        return _unchanged(Outcome.STOPPED, reply)
    if write.stage is WriteStage.FINAL:
        return _unmodeled(reply, span, reason="pause_as_terminal_row", now_ns=now_ns)
    confirmed = confirm_progress(reply, write.confirms)
    changes = {"presentation": write.shown}
    updated = (
        # The waiting span still runs: its reply keeps the Stop button.
        _bump(confirmed, now_ns, state=ReplyState.PAUSED, **changes)
        if in_place
        else _set_state(confirmed, ReplyState.PAUSED, now_ns, **changes)
    )
    row = None
    if write.stage is not None:
        updated, row = _row(updated, span, write.stage, shown=write.shown)
    if in_place:
        return Transition(outcome=Outcome.APPLIED, reply=updated, row=row)
    updated = _clear_current(updated, span.span_id)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=updated,
        spans=(_end(span, SpanOutcome.PAUSED, now_ns),),
        row=row,
    )


def resumed_in_place(reply: Reply, span: Span, *, approval_id: str, now_ns: int) -> Transition:
    """A response-local approval wait got its decision: the waiting span goes on."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if reply.state is not ReplyState.PAUSED or reply.approval_id != approval_id:
        return _unchanged(Outcome.STALE, reply)
    # The span still runs for its approval, which a Stop must fence and whose
    # finish settles the sources and ends the reply, as for a resume.
    return Transition(outcome=Outcome.APPLIED, reply=_set_state(reply, ReplyState.ACTIVE, now_ns))


_ApprovalResult = Literal["failed", "finished"]


def approval_settled(
    reply: Reply,
    last_span: Span | None,
    *,
    approval_id: str,
    paused_span_id: str,
    result: _ApprovalResult,
    disposition: FailureDisposition | None,
    answers_turn: bool,
    now_ns: int,
) -> Transition:
    """Apply a continuation's finish, which settles the sources its pause held whatever the reply does.

    The turn stays unanswered when no owner is left to answer it
    (``answers_turn``); the store also leaves a deleted turn unanswered.
    """
    decided = _approval_finish(
        reply,
        last_span,
        approval_id=approval_id,
        result=result,
        disposition=disposition,
        now_ns=now_ns,
    )
    settle = SettleSources(paused_span_id, answered=answers_turn)
    ended = decided.reply
    if not answers_turn and decided.outcome is not Outcome.STALE and ended is not None and ended.terminal:
        # No owner is left, as for a removed entity or one that permanently failed to start: the reply ends
        # owing Matrix nothing, as one no approval held does, and an owner that comes back redacts nothing.
        dropped = _without_matrix_work(ended, now_ns)
        if replace(dropped, updated_at_ns=ended.updated_at_ns) != ended:
            decided = replace(decided, outcome=Outcome.APPLIED, reply=dropped)
    return replace(decided, effects=(settle, *decided.effects))


def _approval_finish(
    reply: Reply,
    last_span: Span | None,
    *,
    approval_id: str,
    result: _ApprovalResult,
    disposition: FailureDisposition | None,
    now_ns: int,
) -> Transition:
    """Apply a continuation's finish to the reply it paused."""
    resumed = last_span is not None and last_span.kind is SpanKind.APPROVAL_RESUME and last_span.ended
    owns = reply.approval_id == approval_id or (
        resumed and last_span is not None and last_span.approval_id == approval_id
    )
    if not owns:
        return _unchanged(Outcome.STALE, reply)
    if reply.terminal:
        # Its terminal row or note already ended the reply; the finish only settles the sources.
        return _unchanged(Outcome.DUPLICATE, reply)
    if result == "failed":
        return _approval_failed(reply, last_span, approval_id=approval_id, disposition=disposition, now_ns=now_ns)
    # A finished run's acknowledged answer already ended the reply.
    return _unchanged(Outcome.STALE, reply)


def _approval_failed(
    reply: Reply,
    last_span: Span | None,
    *,
    approval_id: str,
    disposition: FailureDisposition | None,
    now_ns: int,
) -> Transition:
    """End the reply a failed continuation held."""
    resume = last_span if last_span is not None and last_span.kind is SpanKind.APPROVAL_RESUME else None
    # A resume an older instance left current runs nowhere: the failure ends it.
    orphaned = (
        resume is not None
        and not resume.ended
        and resume.approval_id == approval_id
        and reply.current_span_id == resume.span_id
    )
    resumed = resume is not None and resume.ended
    # A span approved in place still holds the reply once it ended without its answer.
    approved_in_place = (
        reply.approval_id == approval_id and last_span is not None and last_span.ended and resume is None
    )
    if not (
        reply.state is ReplyState.PAUSED
        or (reply.state is ReplyState.ACTIVE and (resumed or orphaned or approved_in_place))
    ):
        return _unchanged(Outcome.STALE, reply)
    stopped_by_user = disposition == "cancelled_by_user" or reply.unapplied_stop
    state = ReplyState.CANCELLED if stopped_by_user else ReplyState.FAILED
    spans: tuple[Span, ...] = ()
    updated = reply
    if orphaned and resume is not None:
        spans = (_end(resume, SpanOutcome.LOST, now_ns),)
        updated = _clear_current(updated, resume.span_id)
    updated = _set_state(updated, state, now_ns)
    if state is ReplyState.CANCELLED:
        updated = _stop_applied(updated)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=spans)


def approval_failure_note(
    reply: Reply,
    span: Span,
    *,
    approval_id: str,
    shown: str,
    state: ReplyState,
    prepared_revision: int,
    span_has_final: bool,
    now_ns: int,
) -> Transition:
    """Write a failed approval's note, the reply's terminal write, while its continuation settles.

    The note freezes the reply's end: a Stop that arrives later is satisfied
    by it, as by any terminal row, and the continuation's finish changes
    nothing on the reply.
    """
    resumed = span.kind is SpanKind.APPROVAL_RESUME and span.ended and span.approval_id == approval_id
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    if (reply.approval_id != approval_id and not resumed) or reply.current_span_id is not None:
        # Another approval owns the reply now, or a running span writes it.
        return _unchanged(Outcome.STALE, reply)
    if span.span_id != reply.last_span_id:
        # A later span claimed the reply: the approval's note no longer belongs on it.
        return replace(_unchanged(Outcome.STALE, reply), unmodeled="approval_note_of_an_older_span")
    recompute = _check_revision(reply, prepared_revision)
    if recompute is not None:
        return recompute
    expected = _expected_terminal_state(reply, state)
    if state is not expected:
        return _unchanged(Outcome.RECOMPUTE, reply)
    stage = WriteStage.EDIT if span_has_final else WriteStage.FINAL
    updated = _set_state(reply, state, now_ns, presentation=shown)
    if state is ReplyState.CANCELLED:
        updated = _stop_applied(updated)
    updated, row = _row(updated, span, stage, shown=shown)
    return Transition(outcome=Outcome.APPLIED, reply=updated, row=row)


def span_left_behind(reply: Reply, span: Span, *, active_generation: str | None, now_ns: int) -> Transition:
    """End a span an older bot instance left current, so the reply's approval settlement can write it."""
    if span.ended or span.bot_generation == active_generation or reply.current_span_id != span.span_id:
        return _unchanged(Outcome.DUPLICATE, reply)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_touch(_clear_current(reply, span.span_id), now_ns),
        spans=(_end(span, SpanOutcome.LOST, now_ns),),
    )


def approval_released(reply: Reply, span: Span | None, *, now_ns: int) -> Transition:
    """A continuation released to replay ends the span running for it, if any, keeping sources pending.

    A Stop the run never saw outranks the replay instead: the reply ends
    cancelled with its note owed, and the sources settle.
    """
    if reply.terminal or (span is None and reply.approval_id is None) or (span is not None and span.ended):
        return _unchanged(Outcome.DUPLICATE, reply)
    if reply.unapplied_stop:
        # A Stop the run never saw outranks the replay: the reply ends cancelled and the sources settle.
        updated = reply if span is None else _clear_current(reply, span.span_id)
        owed = OwedWrite(reply.last_span_id, _NOTE_CANCELLED)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed),
            spans=() if span is None else (_end(span, SpanOutcome.CANCELLED, now_ns),),
            effects=(SettleSources(reply.last_span_id),),
        )
    if span is None:
        # A span approved in place that a restart already ended: the replay answers without the approval.
        return Transition(outcome=Outcome.APPLIED, reply=_touch(reply, now_ns))
    updated = _set_state(_clear_current(reply, span.span_id), ReplyState.ACTIVE, now_ns)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(_end(span, SpanOutcome.RELEASED, now_ns),))


# ---------------------------------------------------------------------------
# Reply-authored events


@dataclass(frozen=True, slots=True)
class StopFacts:
    """Inputs of one durable Stop on a reply."""

    receipt_order: int
    # The reply has a current span of the active generation that has not ended.
    span_live: bool


def stop(reply: Reply, span: Span | None, facts: StopFacts, *, now_ns: int) -> Transition:  # noqa: PLR0911
    """Record a Stop on a reply.

    ``span`` is the reply's current span if it has one, else its last span.
    """
    if reply.stop_receipt_order is not None and facts.receipt_order <= reply.stop_receipt_order:
        return _unchanged(Outcome.DUPLICATE, reply)
    recorded = _bump(reply, now_ns, stop_receipt_order=facts.receipt_order)
    if reply.terminal:
        # The frozen terminal row wins; the Stop is satisfied by it.
        return Transition(outcome=Outcome.APPLIED, reply=_stop_applied(recorded))
    unended = span if span is not None and not span.ended else None
    live = unended if facts.span_live else None
    if (
        reply.state is ReplyState.PAUSED
        or reply.approval_id is not None
        or (unended is not None and unended.kind is SpanKind.APPROVAL_RESUME)
    ):
        # The approval runtime owns a paused reply and a resume, even one an
        # older bot instance left running or one already cancelled: its failure
        # settlement ends the reply.
        if reply.approval_id is None:
            return _unmodeled(recorded, unended, reason="stop_on_an_unheld_approval_reply", now_ns=now_ns)
        effects: list[Effect] = [FenceApproval(reply.approval_id, "cancelled_by_user")]
        if live is not None:
            effects.append(CancelSpan(live.span_id, by_stop=True))
        effects.append(WakeApproval(reply.approval_id))
        return Transition(outcome=Outcome.APPLIED, reply=recorded, effects=tuple(effects))
    if live is not None:
        return Transition(outcome=Outcome.APPLIED, reply=recorded, effects=(CancelSpan(live.span_id, by_stop=True),))
    # No span is running: the Stop applies directly. A span nobody runs any
    # more (an older bot instance's, or a selection not yet admitted) ends here.
    if span is not None and (kept := _kept_answer(reply, span)) is not None:
        return _restored(recorded, span, kept, now_ns, SettleSources(span.span_id))
    spans: tuple[Span, ...] = ()
    updated = recorded
    if unended is not None:
        spans = (_end(unended, SpanOutcome.CANCELLED, now_ns),)
        updated = _clear_current(updated, unended.span_id)
    owed = OwedWrite(reply.last_span_id, _NOTE_CANCELLED)
    cancelled = _set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=cancelled,
        spans=spans,
        effects=(SettleSources(reply.last_span_id),),
    )


def flush_owed_write(
    reply: Reply,
    span: Span,
    *,
    shown: str,
    prepared_revision: int,
    span_has_final: bool,
    now_ns: int,
) -> Transition:
    """Enqueue the note a reply-authored transition owed, rendered after it committed."""
    owed = reply.owed_write
    if owed is None:
        return _unchanged(Outcome.DUPLICATE, reply)
    recompute = _check_revision(reply, prepared_revision)
    if recompute is not None:
        return recompute
    if span.span_id != owed.span_id:
        return _unchanged(Outcome.RECOMPUTE, reply)
    stage = WriteStage.EDIT if span_has_final else WriteStage.FINAL
    updated = _touch(reply, now_ns, owed_write=None, presentation=shown)
    updated, row = _row(updated, span, stage, shown=shown)
    return Transition(outcome=Outcome.APPLIED, reply=updated, row=row)


def dispatch_failed(reply: Reply, current: Span | None, *, error_text: str, now_ns: int) -> Transition:
    """A dispatch failed before or after a claim: the reply shows the error."""
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    if current is not None and not current.ended and (kept := _kept_answer(reply, current)) is not None:
        return _restored(reply, current, kept, now_ns, SettleSources(current.span_id))
    spans: tuple[Span, ...] = ()
    updated = reply
    stopped_first = reply.unapplied_stop
    if current is not None and not current.ended:
        outcome = SpanOutcome.CANCELLED if stopped_first else SpanOutcome.FAILED
        spans = (_end(current, outcome, now_ns),)
        updated = _clear_current(updated, current.span_id)
    if stopped_first:
        # A Stop recorded before the failure decides what the reply shows.
        owed = OwedWrite(reply.last_span_id, _NOTE_CANCELLED)
        updated = _set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed)
    else:
        owed = OwedWrite(reply.last_span_id, _NOTE_ERROR, error_text)
        updated = _set_state(updated, ReplyState.FAILED, now_ns, owed_write=owed)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=spans, effects=(SettleSources(reply.last_span_id),))


def sources_settled_without_reply(reply: Reply, span: Span, *, now_ns: int) -> Transition:
    """The span's sources became terminal without an answer; the rule settles them and leaves their turn unanswered.

    A reply an approval holds is its approval's: its settlement ends the reply.
    """
    if reply.terminal or reply.approval_id is not None:
        return _unchanged(Outcome.DUPLICATE, reply)
    # A selection's first span waits, unended, for its claim to make it current.
    awaiting_claim = not span.ended and reply.current_span_id is None and span.span_id == reply.last_span_id
    if not (span.span_id == reply.current_span_id or span.outcome in _SOURCES_PENDING_OUTCOMES or awaiting_claim):
        return _unchanged(Outcome.STALE, reply)
    effects = _settle_sources(reply, span, answered=False)
    if (kept := _kept_answer(reply, span)) is not None:
        return _restored(reply, span, kept, now_ns, *effects)
    spans: tuple[Span, ...] = ()
    updated = _clear_current(reply, span.span_id)
    if not span.ended:
        spans = (_end(span, SpanOutcome.SUPPRESSED, now_ns),)
    if reply.event_id is None or (reply.placeholder_only and reply.confirmed):
        gone = _with_redactions(_set_state(_stop_applied(updated), ReplyState.GONE, now_ns), *_visible_event_ids(reply))
        return Transition(outcome=Outcome.APPLIED, reply=gone, spans=spans, effects=effects)
    if reply.unapplied_stop:
        # A Stop recorded before the sources settled decides how the reply ends.
        owed = OwedWrite(span.span_id, _NOTE_CANCELLED)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed),
            spans=spans,
            effects=effects,
        )
    # What the reply showed stays, ended by the interrupted note; nothing else
    # would replace the in-progress status it shows. An edit Matrix has not
    # confirmed may show more than the placeholder, so it stays too.
    owed = OwedWrite(span.span_id, _NOTE_INTERRUPTED)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_set_state(updated, ReplyState.FAILED, now_ns, owed_write=owed),
        spans=spans,
        effects=effects,
    )


def replay_superseded(reply: Reply, last: Span, *, durable_write_debt: bool, now_ns: int) -> Transition:
    """A newer message superseded the replay of the reply's sources; they settle in this transaction.

    A reply that still owes Matrix a write is never superseded, because its
    replay is what resolves that write; nor is one ``replay_dropped`` keeps.
    """
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    if durable_write_debt or reply.owed_write is not None:
        return _unchanged(Outcome.DEFERRED, reply)
    return replay_dropped(reply, last, sources_pending=False, now_ns=now_ns)


def replay_dropped(reply: Reply, last: Span, *, sources_pending: bool, now_ns: int) -> Transition:
    """The sources a reply waits to replay settled without a turn, as ingress settles one it will not answer.

    Nothing replays them any more, so the reply ends as its earlier span left
    it. A reply a span runs, or with sources still pending, keeps waiting; an
    approval ends its reply instead.
    """
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    if sources_pending or reply.current_span_id is not None or reply.approval_id is not None:
        return _unchanged(Outcome.DEFERRED, reply)
    return sources_settled_without_reply(reply, last, now_ns=now_ns)


def sources_deleted(reply: Reply, span: Span | None, *, now_ns: int) -> Transition:
    """Every logical source of the reply's current work was deleted.

    ``span`` is the reply's current span, or its last one when none runs.
    A reply an approval holds, paused, resuming, or settling a resume that
    ended, is its approval's: the card stays the consent surface, and the
    approval's settlement ends the reply and settles its sources.
    """
    if reply.terminal or reply.approval_id is not None:
        return _unchanged(Outcome.DUPLICATE, reply)
    live = span is not None and span.span_id == reply.current_span_id and not span.ended
    current = span if live else None
    # A regeneration a restart or retry left waiting for its replay still holds the answer it would replace.
    waiting = span if not live and span is not None and span.outcome in _SOURCES_PENDING_OUTCOMES else None
    regeneration = current or waiting
    kept = None if regeneration is None else _kept_answer(reply, regeneration)
    if regeneration is not None and kept is not None:
        # The answer an edit was regenerating stands, as when the regeneration
        # fails before showing anything: a finished answer is kept.
        # The restored answer stands, but nothing answers sources the user deleted.
        settle = SettleSources(regeneration.span_id, answered=False)
        effects = (settle,) if regeneration is waiting else (CancelSpan(regeneration.span_id), settle)
        return _restored(reply, regeneration, kept, now_ns, *effects)
    effects: list[Effect] = []
    spans: tuple[Span, ...] = ()
    updated = reply
    if current is not None:
        effects.append(CancelSpan(current.span_id))
        effects.append(SettleSources(current.span_id, answered=False))
        spans = (_end(current, SpanOutcome.CANCELLED, now_ns),)
        updated = _clear_current(updated, current.span_id)
    else:
        effects.append(SettleSources(reply.last_span_id, answered=False))
    gone = _with_redactions(
        _set_state(_stop_applied(updated), ReplyState.GONE, now_ns),
        *_visible_event_ids(reply),
    )
    return Transition(outcome=Outcome.APPLIED, reply=gone, spans=spans, effects=tuple(effects))


def departed(reply: Reply, current: Span | None, *, now_ns: int) -> Transition:
    """The bot left the room: non-terminal replies end without touching Matrix."""
    if reply.terminal:
        if not reply.redaction_pending and reply.owed_write is None:
            return _unchanged(Outcome.DUPLICATE, reply)
        return Transition(outcome=Outcome.APPLIED, reply=_touch(reply, now_ns, redaction_pending=(), owed_write=None))
    effects: tuple[Effect, ...] = ()
    spans: tuple[Span, ...] = ()
    updated = reply
    if current is not None and not current.ended:
        effects = (CancelSpan(current.span_id),)
        spans = (_end(current, SpanOutcome.RELEASED, now_ns),)
        updated = _clear_current(updated, current.span_id)
    updated = _bump(
        _stop_applied(updated),
        now_ns,
        state=ReplyState.GONE,
        redaction_pending=(),
        stop_button_event_id=None,
        owed_write=None,
    )
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=spans, effects=effects)


@dataclass(frozen=True, slots=True)
class OwnerLostFacts:
    """Inputs of the startup ownership check for one reply."""

    active_generation: str
    sources_pending: bool


def _stop_left_unapplied(reply: Reply, updated: Reply, last: Span, ended: tuple[Span, ...], now_ns: int) -> Transition:
    """Apply at start a Stop the span an older bot instance ran never saw, as that span would have."""
    if (kept := _kept_answer(reply, last)) is not None:
        # The answer the regeneration never replaced stands.
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_restore(updated, last, kept, now_ns),
            spans=tuple(replace(span, outcome=SpanOutcome.RESTORED) for span in ended),
            effects=(SettleSources(last.span_id),),
        )
    owed = OwedWrite(last.span_id, _NOTE_CANCELLED)
    cancelled = _set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed)
    return Transition(outcome=Outcome.APPLIED, reply=cancelled, spans=ended, effects=(SettleSources(last.span_id),))


def owner_lost(reply: Reply, last: Span, facts: OwnerLostFacts, *, now_ns: int) -> Transition:  # noqa: PLR0911
    """Settle a reply whose span an older bot instance left behind."""
    if (
        reply.state is ReplyState.PAUSED
        and last.outcome is None
        and last.span_id == reply.current_span_id
        and last.bot_generation != facts.active_generation
    ):
        # An in-place approval wait an older instance ran ends as any pause
        # does: the reply waits for its decision, without the span's Stop button.
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_touch(_clear_current(reply, last.span_id), now_ns),
            spans=(_end(last, SpanOutcome.PAUSED, now_ns),),
        )
    if reply.state is not ReplyState.ACTIVE:
        return _unchanged(Outcome.DUPLICATE, reply)
    orphaned = (last.outcome is None and last.bot_generation != facts.active_generation) or (
        last.outcome in _SOURCES_PENDING_OUTCOMES and not facts.sources_pending
    )
    if not orphaned:
        return _unchanged(Outcome.DUPLICATE, reply)
    spans: list[Span] = []
    updated = reply
    if last.outcome is None:
        if last.kind is SpanKind.APPROVAL_RESUME:
            # Approval recovery still owns this resume and reports through approval events.
            return _unchanged(Outcome.DUPLICATE, reply)
        last = _end(last, SpanOutcome.LOST, now_ns)
        spans.append(last)
        updated = _clear_current(updated, last.span_id)
    if reply.approval_id is not None:
        # A span approved in place ran for its approval: approval recovery
        # settles the reply, its sources, and any Stop that fenced it.
        return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), spans=tuple(spans))
    if reply.unapplied_stop:
        return _stop_left_unapplied(reply, updated, last, tuple(spans), now_ns)
    if facts.sources_pending:
        # Replay claims it; the lost span marks where the claim continues.
        return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), spans=tuple(spans))
    return _ended_by_restart(reply, updated, last, tuple(spans), now_ns)


def _ended_by_restart(reply: Reply, updated: Reply, last: Span, ended: tuple[Span, ...], now_ns: int) -> Transition:
    """End a reply whose sources settled while no instance ran its span."""
    if (kept := _kept_answer(reply, last)) is not None:
        return Transition(outcome=Outcome.APPLIED, reply=_restore(updated, last, kept, now_ns), spans=ended)
    if reply.event_id is None and reply.possibly_shown_seq is None:
        # It never wrote anything: a restart note would be a message of its own.
        return Transition(outcome=Outcome.APPLIED, reply=_set_state(updated, ReplyState.GONE, now_ns), spans=ended)
    owed = OwedWrite(last.span_id, _NOTE_RESTART)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_set_state(updated, ReplyState.FAILED, now_ns, owed_write=owed),
        spans=ended,
    )


def removed_entity(reply: Reply, span: Span, *, now_ns: int) -> Transition:
    """End a reply whose entity left the configuration, without writing to Matrix.

    ``span`` is the reply's current span, or its last one when none runs. Its
    sources settle unanswered, unless an approval holds the reply: its
    settlement ends the reply and settles them. A reply that already ended
    drops what it still owed Matrix.
    """
    if reply.terminal:
        if reply.owed_write is None and not reply.redaction_pending and reply.stop_button_event_id is None:
            return _unchanged(Outcome.DUPLICATE, reply)
        return Transition(outcome=Outcome.APPLIED, reply=_without_matrix_work(reply, now_ns))
    spans: tuple[Span, ...] = ()
    updated = reply
    if span.span_id == reply.current_span_id and not span.ended:
        spans = (_end(span, SpanOutcome.LOST, now_ns),)
        updated = _clear_current(updated, span.span_id)
    if reply.approval_id is not None:
        # Its approval ends it: discarded while the entity stays gone, or settled by an owner that comes back.
        return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), spans=spans)
    # No bot remains to answer, write, or redact anything for this entity.
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_without_matrix_work(_set_state(_stop_applied(updated), ReplyState.FAILED, now_ns), now_ns),
        spans=spans,
        effects=_settle_sources(reply, span, answered=False),
    )


def _without_matrix_work(reply: Reply, now_ns: int) -> Reply:
    """Drop what a removed entity's reply still owed Matrix: no bot remains to write or redact it."""
    return _touch(reply, now_ns, owed_write=None, redaction_pending=(), stop_button_event_id=None)


def owed_write_refused(reply: Reply, owed: OwedWrite, *, now_ns: int) -> Transition:
    """The outbox refused the row an owed write needs, as in a room the bot left: nothing can send it."""
    if reply.owed_write != owed:
        return _unchanged(Outcome.DUPLICATE, reply)
    return Transition(outcome=Outcome.APPLIED, reply=_touch(reply, now_ns, owed_write=None))


def redactions_done(reply: Reply, event_ids: tuple[str, ...], *, now_ns: int) -> Transition:
    """Drop event ids whose redaction landed or was refused because the event is gone."""
    remaining = tuple(event_id for event_id in reply.redaction_pending if event_id not in event_ids)
    if remaining == reply.redaction_pending:
        return _unchanged(Outcome.DUPLICATE, reply)
    return Transition(outcome=Outcome.APPLIED, reply=_touch(reply, now_ns, redaction_pending=remaining))


def record_stop_button(reply: Reply, *, event_id: str, membership_current: bool, now_ns: int) -> Transition:
    """Record the Stop button reaction sent for a running reply, or queue it for removal (I8).

    A reply runs while active, and while paused with the span that waits in
    place still current. A reply shows one button: one it already recorded is
    queued for removal. A button the room was left before it landed stays,
    as everything owed to that room is dropped.
    """
    running = reply.state is ReplyState.ACTIVE or (
        reply.state is ReplyState.PAUSED and reply.current_span_id is not None
    )
    if not running:
        if not membership_current:
            return _unchanged(Outcome.DUPLICATE, reply)
        return Transition(outcome=Outcome.APPLIED, reply=_touch(_with_redactions(reply, event_id), now_ns))
    updated = (
        reply if reply.stop_button_event_id in {None, event_id} else _with_redactions(reply, reply.stop_button_event_id)
    )
    return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns, stop_button_event_id=event_id))
