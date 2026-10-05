"""Pure state transitions of durable reply messages (DESIGN.md §6).

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
from typing import Literal

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


TERMINAL_STATES = frozenset({ReplyState.COMPLETED, ReplyState.CANCELLED, ReplyState.FAILED, ReplyState.GONE})


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
SOURCES_PENDING_OUTCOMES = frozenset({SpanOutcome.RELEASED, SpanOutcome.SUPERSEDED, SpanOutcome.LOST})


class Outcome(StrEnum):
    """What one event did to the records."""

    APPLIED = "applied"
    STALE = "stale"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred"
    # The prepared payload was rendered for an older revision; nothing was written.
    RECOMPUTE = "recompute"


class InvalidTransitionError(RuntimeError):
    """An event that the rules say cannot happen: a programming error."""


class WriteStage(StrEnum):
    """Outbox stage of one durable reply write."""

    INITIAL = "initial"
    FINAL = "final"
    EDIT = "edit"


class VisibilityPolicy(StrEnum):
    """Whether a reply is posted normally or only reported by a silent schedule."""

    NORMAL = "normal"
    SILENT_SCHEDULE = "silent_schedule"


class LegacyPending(StrEnum):
    """A one-time Matrix read a reply migrated from main still needs."""

    PRESENTATION_READ = "presentation_read"
    ADOPTION_SCAN = "adoption_scan"


# Note kinds are owned by ``reply_presentation``; the lifecycle names them by value.
NOTE_CANCELLED = "cancelled"
NOTE_RESTART = "restart"
NOTE_INTERRUPTED = "interrupted"
NOTE_DELIVERY_FAILED = "delivery_failed"
NOTE_APPROVAL_FAILED = "approval_failed"
NOTE_ERROR = "error"

FailureDisposition = Literal["cancelled_by_user", "superseded", "failed"]

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
    """What a regeneration restores when it ends before its first acknowledged write.

    The Stop button is not part of it: a button belongs to the span that sent
    it and is removed when the reply stops being active (I8).
    """

    presentation: str
    frozen_display: str | None
    state: ReplyState
    # A historical reply whose presentation was never recorded restores state only.
    presentation_known: bool = True


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
    approval_generation: int | None = None
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
    write never hands sources over. Its stage follows DESIGN.md §7.2 and is
    chosen when it is enqueued: the span's ``FINAL`` while its delivery id has
    none, an ``edit`` row otherwise.
    """

    span_id: str
    note: str
    # Text for notes whose wording is not fixed by their kind (errors).
    text: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """Durable record of one reply (DESIGN.md §4.3)."""

    reply_id: str
    entity_name: str
    room_id: str
    thread_id: str | None
    membership_epoch: int
    requester_id: str
    visibility_policy: VisibilityPolicy
    state: ReplyState
    last_span_id: str
    presentation: str
    revision: int
    reply_sequence: int
    created_at_ns: int
    updated_at_ns: int
    event_id: str | None = None
    continuation_event_ids: tuple[str, ...] = ()
    current_span_id: str | None = None
    frozen_display: str | None = None
    possibly_shown: str | None = None
    possibly_shown_seq: int | None = None
    confirmed_seq: int | None = None
    legacy_pending: LegacyPending | None = None
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
        return self.state in TERMINAL_STATES

    @property
    def unapplied_stop(self) -> bool:
        """Return whether a recorded Stop still has to reach the reply."""
        return self.stop_receipt_order is not None and (
            self.stop_applied_receipt_order is None or self.stop_applied_receipt_order < self.stop_receipt_order
        )

    @property
    def confirmed(self) -> bool:
        """Return whether Matrix acknowledged the reply's latest write (DESIGN.md §5.4)."""
        if self.possibly_shown_seq is None:
            return True
        return self.confirmed_seq is not None and self.confirmed_seq >= self.possibly_shown_seq


# ---------------------------------------------------------------------------
# Effects


@dataclass(frozen=True, slots=True)
class SettleSources:
    """In the transaction: settle every pending source of the span in the journal."""

    span_id: str


@dataclass(frozen=True, slots=True)
class FenceApproval:
    """In the transaction: fence the reply's approval continuation for failure."""

    approval_id: str
    disposition: FailureDisposition


@dataclass(frozen=True, slots=True)
class CancelSpan:
    """After commit: cancel exactly this span's task, if it is running here."""

    span_id: str


@dataclass(frozen=True, slots=True)
class WakeApproval:
    """After commit: wake the approval source so its failure settlement runs."""

    approval_id: str


@dataclass(frozen=True, slots=True)
class TransferStop:
    """In the transaction: copy the reply's Stop onto the turn record of its source."""

    receipt_order: int
    target_event_id: str


type Effect = SettleSources | FenceApproval | CancelSpan | WakeApproval | TransferStop


@dataclass(frozen=True, slots=True)
class RowIntent:
    """A durable write the transaction enqueues with the caller's prepared payload."""

    stage: WriteStage
    sequence: int
    span_id: str
    settles_sources: bool


@dataclass(frozen=True, slots=True)
class Transition:
    """The result of one event: what to write and what to run."""

    outcome: Outcome
    reply: Reply | None
    spans: tuple[Span, ...] = ()
    effects: tuple[Effect, ...] = ()
    row: RowIntent | None = None
    # Set by claims that took effect.
    claimed: Span | None = None

    @property
    def applied(self) -> bool:
        """Return whether the event changed the records."""
        return self.outcome is Outcome.APPLIED


def _unchanged(outcome: Outcome, reply: Reply | None) -> Transition:
    return Transition(outcome=outcome, reply=reply)


def _invalid(message: str) -> InvalidTransitionError:
    return InvalidTransitionError(message)


# ---------------------------------------------------------------------------
# Shared helpers


def _end(span: Span, outcome: SpanOutcome, now_ns: int) -> Span:
    if span.ended:
        msg = f"Span {span.span_id} already ended {span.outcome}"
        raise _invalid(msg)
    return replace(span, outcome=outcome, ended_at_ns=now_ns)


def _clear_current(reply: Reply, span_id: str) -> Reply:
    return replace(reply, current_span_id=None) if reply.current_span_id == span_id else reply


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
    if reply.event_id is None:
        return ()
    return (reply.event_id, *reply.continuation_event_ids)


def _leaves_active(reply: Reply, new_state: ReplyState) -> Reply:
    """Queue the Stop button's redaction when the reply stops being active (I8)."""
    if reply.state is ReplyState.ACTIVE and new_state is not ReplyState.ACTIVE and reply.stop_button_event_id:
        return replace(_with_redactions(reply, reply.stop_button_event_id), stop_button_event_id=None)
    return reply


def _set_state(reply: Reply, state: ReplyState, now_ns: int, **changes: object) -> Reply:
    return _bump(_leaves_active(reply, state), now_ns, state=state, **changes)


def _stop_applied(reply: Reply) -> Reply:
    return replace(reply, stop_applied_receipt_order=reply.stop_receipt_order)


def _next_sequence(reply: Reply) -> tuple[Reply, int]:
    sequence = reply.reply_sequence + 1
    return replace(reply, reply_sequence=sequence), sequence


def _row(
    reply: Reply,
    span: Span,
    stage: WriteStage,
    *,
    shown: str,
    settles_sources: bool,
) -> tuple[Reply, RowIntent]:
    """Allocate the next write sequence for one durable row and record what it may show."""
    reply, sequence = _next_sequence(reply)
    reply = replace(reply, possibly_shown=shown, possibly_shown_seq=sequence)
    return reply, RowIntent(stage=stage, sequence=sequence, span_id=span.span_id, settles_sources=settles_sources)


def _require_current(reply: Reply, span: Span) -> None:
    if span.reply_id != reply.reply_id:
        msg = f"Span {span.span_id} does not belong to reply {reply.reply_id}"
        raise _invalid(msg)


def _stale_span(reply: Reply, span: Span) -> Transition | None:
    """Refuse span-authored events from a span that is no longer current (I1)."""
    _require_current(reply, span)
    if span.ended or reply.current_span_id != span.span_id:
        effects: tuple[Effect, ...] = () if span.ended else (CancelSpan(span.span_id),)
        return Transition(outcome=Outcome.STALE, reply=reply, effects=effects)
    return None


def _check_revision(reply: Reply, prepared_revision: int) -> Transition | None:
    if prepared_revision != reply.revision:
        return _unchanged(Outcome.RECOMPUTE, reply)
    return None


def had_acknowledged_write(reply: Reply, span: Span) -> bool:
    """Return whether Matrix acknowledged any write the span made."""
    return reply.confirmed_seq is not None and reply.confirmed_seq > span.base_sequence


def _restore(reply: Reply, span: Span, now_ns: int) -> Reply:
    """Restore a regeneration's rollback snapshot (DESIGN.md §6.4 "Regeneration rollback").

    The old answer stands, so a Stop recorded during the regeneration is
    satisfied by it. A reply that was paused does not get its approval back:
    consent is never restored, so it ends failed with the interruption note.
    """
    rollback = span.rollback
    if rollback is None:
        msg = f"Span {span.span_id} has no rollback snapshot"
        raise _invalid(msg)
    restored = _stop_applied(_clear_current(reply, span.span_id))
    if rollback.state is ReplyState.PAUSED:
        return _set_state(
            restored,
            ReplyState.FAILED,
            now_ns,
            presentation=rollback.presentation,
            frozen_display=rollback.frozen_display,
            owed_write=OwedWrite(span.span_id, NOTE_INTERRUPTED),
        )
    return _set_state(
        restored,
        rollback.state,
        now_ns,
        presentation=rollback.presentation,
        frozen_display=rollback.frozen_display,
    )


# ---------------------------------------------------------------------------
# Claims


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """One executor's request to claim a reply, after main's first source gate passed."""

    span_id: str
    delivery_id: str
    sources: SpanSources
    bot_generation: str
    now_ns: int
    # Used only when the claim creates a reply.
    new_reply_id: str
    entity_name: str
    room_id: str
    thread_id: str | None
    membership_epoch: int
    requester_id: str
    visibility_policy: VisibilityPolicy
    empty_presentation: str
    # Set for edit regenerations: the edit event that drives this run.
    driving_edit_id: str | None = None
    # Set for approval resumes, claimed with the continuation.
    approval_id: str | None = None
    approval_generation: int | None = None
    # Set when an interactive selection created this span at its acknowledgement.
    interactive_span_id: str | None = None
    # Set when no reply record exists but main recorded a historical response event.
    historical_event_id: str | None = None


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
    # The edit receipt order that settles an older Stop (edit regenerations only).
    edit_receipt_order: int | None = None


def _new_reply(request: ClaimRequest, *, state: ReplyState, event_id: str | None = None) -> Reply:
    return Reply(
        reply_id=request.new_reply_id,
        entity_name=request.entity_name,
        room_id=request.room_id,
        thread_id=request.thread_id,
        membership_epoch=request.membership_epoch,
        requester_id=request.requester_id,
        visibility_policy=request.visibility_policy,
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
        approval_generation=request.approval_generation,
        rollback=rollback,
    )


def _make_current(reply: Reply, span: Span, now_ns: int, **changes: object) -> Reply:
    return _touch(reply, now_ns, current_span_id=span.span_id, last_span_id=span.span_id, **changes)


def _rollback_of(reply: Reply, *, presentation_known: bool = True) -> Rollback:
    return Rollback(
        presentation=reply.presentation,
        frozen_display=reply.frozen_display,
        state=reply.state,
        presentation_known=presentation_known,
    )


def claim(request: ClaimRequest, context: ClaimContext) -> Transition:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Claim a reply for one span (DESIGN.md §6.4 ``claim``)."""
    reply = context.reply
    changed: list[Span] = []
    effects: list[Effect] = []
    if reply is not None and context.current_span is not None:
        current = context.current_span
        if current.bot_generation == context.active_generation:
            # Claims run under the conversation lock, so a live span here is a bug.
            msg = f"Reply {reply.reply_id} already has live span {current.span_id}"
            raise _invalid(msg)
        # A span an older bot instance left current never ends by itself.
        lost = _end(current, SpanOutcome.LOST, request.now_ns)
        changed.append(lost)
        reply = _clear_current(reply, current.span_id)
        if context.last_span is not None and context.last_span.span_id == lost.span_id:
            context = replace(context, last_span=lost)

    if reply is not None and (context.durable_write_debt or reply.owed_write is not None):
        # Waiting under the conversation lock would block the reply's own
        # sends; the debt's resolution wakes these sources instead.
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
            msg = f"Approval resume {request.approval_id} has no paused reply"
            raise _invalid(msg)
        span = _new_span(request, reply, SpanKind.APPROVAL_RESUME)
        return claimed(_make_current(_set_state(reply, ReplyState.ACTIVE, request.now_ns), span, request.now_ns), span)

    interactive = context.interactive_span
    if request.interactive_span_id is not None and interactive is not None and reply is not None:
        if interactive.outcome is None:
            return claimed(_make_current(reply, interactive, request.now_ns), interactive)
        if interactive.outcome is not SpanOutcome.LOST:
            msg = f"Interactive span {interactive.span_id} already ended {interactive.outcome}"
            raise _invalid(msg)
        # The acknowledgement's bot instance is gone: the selection continues as a replay.
        span = _new_span(request, reply, SpanKind.REPLAY)
        return claimed(_make_current(reply, span, request.now_ns), span)

    if reply is None:
        if request.driving_edit_id is not None:
            # A reply older than the records: main's handled-turn ledger named its event.
            historical = _new_reply(request, state=ReplyState.COMPLETED, event_id=request.historical_event_id)
            span = _new_span(
                request,
                historical,
                SpanKind.REGENERATION,
                rollback=_rollback_of(historical, presentation_known=False),
            )
            return claimed(
                _make_current(_set_state(historical, ReplyState.ACTIVE, request.now_ns), span, request.now_ns),
                span,
            )
        created = _new_reply(request, state=ReplyState.ACTIVE)
        span = _new_span(request, created, SpanKind.TURN)
        return claimed(_make_current(created, span, request.now_ns), span)

    last = context.last_span
    if request.driving_edit_id is not None and (last is None or request.driving_edit_id != last.delivery_id):
        if reply.state is ReplyState.GONE:
            created = _new_reply(request, state=ReplyState.ACTIVE)
            span = _new_span(request, created, SpanKind.REGENERATION)
            return claimed(_make_current(created, span, request.now_ns), span)
        rollback = _rollback_of(reply)
        if reply.state is ReplyState.PAUSED:
            if reply.approval_id is None:
                msg = f"Paused reply {reply.reply_id} has no approval"
                raise _invalid(msg)
            # Decision 1: an edit supersedes the approval; its cleanup runs
            # outside the conversation lock and publishes no failure note.
            effects.append(FenceApproval(reply.approval_id, "superseded"))
            effects.append(WakeApproval(reply.approval_id))
            reply = replace(reply, approval_id=None)
        next_reply = reply
        if (
            context.edit_receipt_order is not None
            and reply.stop_receipt_order is not None
            and context.edit_receipt_order > reply.stop_receipt_order
        ):
            next_reply = _stop_applied(next_reply)
        span = _new_span(request, next_reply, SpanKind.REGENERATION, rollback=rollback)
        return claimed(
            _make_current(_set_state(next_reply, ReplyState.ACTIVE, request.now_ns), span, request.now_ns),
            span,
        )

    if reply.state is not ReplyState.ACTIVE or reply.current_span_id is not None or last is None or not last.ended:
        msg = f"Reply {reply.reply_id} in state {reply.state} cannot be claimed again"
        raise _invalid(msg)
    if last.outcome is SpanOutcome.SUPERSEDED:
        span = _new_span(request, reply, last.kind, rollback=last.rollback)
        return claimed(_make_current(reply, span, request.now_ns), span)
    if last.outcome in {SpanOutcome.RELEASED, SpanOutcome.LOST}:
        if last.kind is SpanKind.REGENERATION:
            span = _new_span(request, reply, SpanKind.REGENERATION, rollback=last.rollback)
        else:
            span = _new_span(request, reply, SpanKind.REPLAY)
        return claimed(_make_current(reply, span, request.now_ns), span)
    msg = f"Reply {reply.reply_id}'s last span ended {last.outcome} and cannot be claimed again"
    raise _invalid(msg)


def interactive_acknowledgement(request: ClaimRequest) -> Transition:
    """Create a selection's reply and its first span, not yet current, at the acknowledgement send."""
    created = _new_reply(request, state=ReplyState.ACTIVE)
    created = replace(created, placeholder_only=True)
    span = _new_span(request, created, SpanKind.TURN)
    return Transition(outcome=Outcome.APPLIED, reply=created, spans=(span,))


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


def write_ahead(
    reply: Reply,
    span: Span,
    *,
    shown: str,
    previous_ok: bool,
    previous_event_id: str | None,
    active_generation: str,
    now_ns: int,
) -> Transition:
    """Record the presentation of the next direct progress edit before it is sent (DESIGN.md §5.4)."""
    if span.bot_generation != active_generation:
        return _unchanged(Outcome.STALE, reply)
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    updated = reply
    if previous_ok and reply.possibly_shown_seq is not None:
        updated = replace(updated, confirmed_seq=max(reply.confirmed_seq or 0, reply.possibly_shown_seq))
        if updated.event_id is None and previous_event_id is not None:
            updated = replace(updated, event_id=previous_event_id)
    updated, sequence = _next_sequence(updated)
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
        msg = f"Reply {reply.reply_id} already has an event"
        raise _invalid(msg)
    recompute = _check_revision(reply, prepared_revision)
    if recompute is not None:
        return recompute
    updated, row = _row(reply, span, WriteStage.INITIAL, shown=shown, settles_sources=False)
    updated = _touch(updated, now_ns, placeholder_only=placeholder_only)
    return Transition(outcome=Outcome.APPLIED, reply=updated, row=row)


def write_acknowledged(reply: Reply, write: WriteFacts, *, event_id: str, now_ns: int) -> Transition:
    """Apply Matrix's acknowledgement of one durable row (DESIGN.md §6.4 ``write_acknowledged``)."""
    updated = reply
    effects: list[Effect] = []
    if write.creates_event:
        if reply.event_id is not None and reply.event_id != event_id:
            msg = f"Reply {reply.reply_id} is bound to {reply.event_id}, not {event_id}"
            raise _invalid(msg)
        if reply.event_id is None:
            updated = replace(updated, event_id=event_id)
            if reply.state is ReplyState.GONE:
                # Created after the reply was given up: the late event is removed.
                updated = _with_redactions(updated, event_id)
    if updated.confirmed_seq is None or write.sequence > updated.confirmed_seq:
        updated = replace(updated, confirmed_seq=write.sequence)
        if write.sequence >= (updated.possibly_shown_seq or 0):
            updated = replace(updated, placeholder_only=write.placeholder_only)
    if updated == reply:
        return _unchanged(Outcome.DUPLICATE, reply)
    return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), effects=tuple(effects))


@dataclass(frozen=True, slots=True)
class FailedWrite:
    """A durable row Matrix refused permanently."""

    write: WriteFacts
    reason: str


_DELIVERY_FAILURE_REASONS = frozenset({"delivery_failed", "terminal_update_cancelled", "terminal_update_failed"})


def is_delivery_failure_reason(reason: str) -> bool:
    """Return whether a failure came from Matrix delivery itself rather than the run."""
    return reason in _DELIVERY_FAILURE_REASONS or reason.startswith("terminal_update_exception:")


def write_failed(reply: Reply, span: Span, failure: FailedWrite, *, now_ns: int) -> Transition:
    """Apply a permanent row failure (DESIGN.md §6.4 ``write_failed`` and ``terminal_write_failed``)."""
    write = failure.write
    if write.stage is WriteStage.INITIAL:
        # The span keeps running; a later row creates the event instead.
        return _unchanged(Outcome.APPLIED, reply)
    if write.stage is WriteStage.EDIT:
        if reply.state is ReplyState.PAUSED and span.outcome is SpanOutcome.PAUSED and reply.approval_id is not None:
            # A pause nobody saw cannot hold the approval: fence it like a failed handoff.
            if reply.unapplied_stop:
                owed = OwedWrite(span.span_id, NOTE_CANCELLED)
                updated = _set_state(_stop_applied(reply), ReplyState.CANCELLED, now_ns, approval_id=None)
                disposition: FailureDisposition = "cancelled_by_user"
            else:
                owed = OwedWrite(span.span_id, NOTE_APPROVAL_FAILED)
                updated = _set_state(reply, ReplyState.FAILED, now_ns, approval_id=None)
                disposition = "failed"
            return Transition(
                outcome=Outcome.APPLIED,
                reply=replace(updated, owed_write=owed),
                effects=(FenceApproval(reply.approval_id, disposition),),
            )
        return _unchanged(Outcome.APPLIED, reply)
    return terminal_write_failed(reply, span, reason=failure.reason, first_create=reply.event_id is None, now_ns=now_ns)


def terminal_write_failed(reply: Reply, span: Span, *, reason: str, first_create: bool, now_ns: int) -> Transition:
    """A span's terminal row failed for good after the span ended; its outcome stays."""
    if span.span_id != reply.last_span_id:
        # A later span claimed the reply only after this row resolved, so this cannot be its row.
        msg = f"Terminal row of span {span.span_id} failed after span {reply.last_span_id} claimed the reply"
        raise _invalid(msg)
    if first_create:
        return Transition(outcome=Outcome.APPLIED, reply=_set_state(reply, ReplyState.GONE, now_ns))
    if reply.placeholder_only:
        if is_delivery_failure_reason(reason):
            owed = OwedWrite(span.span_id, NOTE_DELIVERY_FAILED)
            return Transition(
                outcome=Outcome.APPLIED,
                reply=replace(_set_state(reply, ReplyState.FAILED, now_ns), owed_write=owed),
            )
        updated = _with_redactions(_set_state(reply, ReplyState.GONE, now_ns), *_visible_event_ids(reply))
        return Transition(outcome=Outcome.APPLIED, reply=updated)
    if span.kind is SpanKind.REGENERATION and span.rollback is not None and not had_acknowledged_write(reply, span):
        return Transition(outcome=Outcome.APPLIED, reply=_restore(reply, span, now_ns))
    if span.kind is SpanKind.APPROVAL_RESUME:
        # The continuation's failure settlement ends the reply.
        return _unchanged(Outcome.APPLIED, reply)
    return Transition(outcome=Outcome.APPLIED, reply=_set_state(reply, ReplyState.FAILED, now_ns))


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
    """Enqueue the span's terminal row and end it with the row's status (DESIGN.md §6.4 ``terminal_row``)."""
    resume = span.kind is SpanKind.APPROVAL_RESUME
    settles = not resume and stage is WriteStage.FINAL
    updated = _clear_current(reply, span.span_id)
    if stop_applied:
        updated = _stop_applied(updated)
    updated = _set_state(
        updated,
        write.state,
        now_ns,
        presentation=write.shown,
        frozen_display=write.frozen_display if write.frozen_display is not None else updated.frozen_display,
    )
    updated, row = _row(updated, span, stage, shown=write.shown, settles_sources=settles)
    effects: tuple[Effect, ...] = (SettleSources(span.span_id),) if settles else ()
    return Transition(
        outcome=Outcome.APPLIED,
        reply=updated,
        spans=(_end(span, outcome, now_ns),),
        effects=effects,
        row=row,
    )


def expected_terminal_state(reply: Reply, requested: ReplyState) -> ReplyState:
    """Return the state a terminal write must render for, given a possibly unapplied Stop."""
    return ReplyState.CANCELLED if reply.unapplied_stop else requested


def finish(reply: Reply, span: Span, write: TerminalWrite, *, now_ns: int) -> Transition:
    """End a span with its answer (DESIGN.md §6.4 ``finish``)."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    expected = expected_terminal_state(reply, ReplyState.COMPLETED)
    if write.state is not expected:
        msg = f"Terminal write rendered {write.state} where the reply requires {expected}"
        raise _invalid(msg)
    if expected is ReplyState.CANCELLED:
        return _terminal_row(reply, span, write, SpanOutcome.CANCELLED, now_ns, stop_applied=True)
    return _terminal_row(reply, span, write, SpanOutcome.COMPLETED, now_ns)


def stopped(reply: Reply, span: Span, write: TerminalWrite | None, *, now_ns: int) -> Transition:
    """End a span cancelled by its reply's Stop (DESIGN.md §6.4 ``stopped``)."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if not reply.unapplied_stop:
        msg = f"Span {span.span_id} reports a Stop its reply never recorded"
        raise _invalid(msg)
    if span.kind is SpanKind.APPROVAL_RESUME:
        updated = _clear_current(reply, span.span_id)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_touch(updated, now_ns),
            spans=(_end(span, SpanOutcome.CANCELLED, now_ns),),
        )
    if span.kind is SpanKind.REGENERATION and span.rollback is not None and not had_acknowledged_write(reply, span):
        # The old answer stays untouched, as main leaves it.
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_restore(reply, span, now_ns),
            spans=(_end(span, SpanOutcome.RESTORED, now_ns),),
            effects=(SettleSources(span.span_id),),
        )
    if write is None:
        msg = "A stopped span needs its cancelled terminal write"
        raise _invalid(msg)
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    if write.state is not ReplyState.CANCELLED:
        msg = "A stopped span renders a cancelled reply"
        raise _invalid(msg)
    return _terminal_row(reply, span, write, SpanOutcome.CANCELLED, now_ns, stop_applied=True)


FailurePhase = Literal["pre_delivery", "delivery"]


def fail(  # noqa: C901, PLR0911
    reply: Reply,
    span: Span,
    write: TerminalWrite | None,
    *,
    phase: FailurePhase,
    now_ns: int,
) -> Transition:
    """End a span that failed (DESIGN.md §6.4 ``fail``).

    ``write`` is the error or interruption note for delivery failures, and for
    a resumed reply's pre-delivery note; it is ``None`` otherwise.
    """
    if span.ended:
        # A terminal row already decided this span; a later error report is the same outcome.
        return _unchanged(Outcome.DUPLICATE, reply)
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    if reply.unapplied_stop and span.kind is not SpanKind.APPROVAL_RESUME:
        if write is None:
            return _unchanged(Outcome.RECOMPUTE, reply)
        return stopped(reply, span, write, now_ns=now_ns)
    if span.kind is SpanKind.APPROVAL_RESUME:
        updated = _clear_current(reply, span.span_id)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_touch(updated, now_ns),
            spans=(_end(span, SpanOutcome.FAILED, now_ns),),
        )
    if span.kind is SpanKind.REGENERATION and span.rollback is not None and not had_acknowledged_write(reply, span):
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_restore(reply, span, now_ns),
            spans=(_end(span, SpanOutcome.RESTORED, now_ns),),
            effects=(SettleSources(span.span_id),),
        )
    if phase == "pre_delivery":
        # Main returns the sources for a retry, which streams into the kept placeholder.
        updated = _touch(_clear_current(reply, span.span_id), now_ns)
        ended = _end(span, SpanOutcome.RELEASED, now_ns)
        if write is None:
            return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(ended,))
        recompute = _check_revision(reply, write.prepared_revision)
        if recompute is not None:
            return recompute
        # A resumed reply shows its interruption below the recovered content
        # without settling its sources.
        updated = _bump(updated, now_ns, presentation=write.shown)
        updated, row = _row(updated, span, WriteStage.EDIT, shown=write.shown, settles_sources=False)
        return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(ended,), row=row)
    if write is None:
        msg = "A failure during delivery needs its terminal note"
        raise _invalid(msg)
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    if write.state is not ReplyState.FAILED:
        msg = "A failed span renders a failed reply"
        raise _invalid(msg)
    return _terminal_row(reply, span, write, SpanOutcome.FAILED, now_ns)


SuppressReason = Literal["suppressed", "hook_failed"]


def suppress(  # noqa: C901, PLR0911
    reply: Reply,
    span: Span,
    *,
    reason: SuppressReason,
    silent_notice: TerminalWrite | None = None,
    now_ns: int,
) -> Transition:
    """End a span whose answer must not be shown (DESIGN.md §6.4 ``suppress`` and ``hook_failed``)."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    outcome = SpanOutcome.SUPPRESSED if reason == "suppressed" else SpanOutcome.FAILED
    if reply.unapplied_stop and span.kind is not SpanKind.APPROVAL_RESUME:
        outcome = SpanOutcome.CANCELLED
    updated = _clear_current(reply, span.span_id)
    effects: tuple[Effect, ...] = (SettleSources(span.span_id),)
    if span.kind is SpanKind.APPROVAL_RESUME:
        effects = ()
    if silent_notice is not None and (reply.event_id is None or reply.placeholder_only):
        recompute = _check_revision(reply, silent_notice.prepared_revision)
        if recompute is not None:
            return recompute
        return _terminal_row(reply, span, silent_notice, outcome, now_ns)
    if reply.event_id is None:
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_set_state(updated, ReplyState.GONE, now_ns),
            spans=(_end(span, outcome, now_ns),),
            effects=effects,
        )
    if reply.placeholder_only:
        gone = _with_redactions(_set_state(updated, ReplyState.GONE, now_ns), *_visible_event_ids(reply))
        return Transition(outcome=Outcome.APPLIED, reply=gone, spans=(_end(span, outcome, now_ns),), effects=effects)
    if span.kind is SpanKind.REGENERATION and span.rollback is not None and not had_acknowledged_write(reply, span):
        return Transition(
            outcome=Outcome.APPLIED,
            reply=_restore(updated, span, now_ns),
            spans=(_end(span, SpanOutcome.RESTORED, now_ns),),
            effects=effects,
        )
    if span.kind is SpanKind.APPROVAL_RESUME:
        return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), spans=(_end(span, outcome, now_ns),))
    state = ReplyState.CANCELLED if reason == "suppressed" or outcome is SpanOutcome.CANCELLED else ReplyState.FAILED
    if outcome is SpanOutcome.CANCELLED:
        updated = _stop_applied(updated)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_set_state(updated, state, now_ns),
        spans=(_end(span, outcome, now_ns),),
        effects=effects,
    )


def release(reply: Reply, span: Span, *, now_ns: int, outcome: SpanOutcome = SpanOutcome.RELEASED) -> Transition:
    """End a span whose sources stay pending for a retry or replay (``release`` and ``superseded``)."""
    if outcome not in {SpanOutcome.RELEASED, SpanOutcome.SUPERSEDED}:
        msg = f"{outcome} is not a releasing outcome"
        raise _invalid(msg)
    _require_current(reply, span)
    if span.ended:
        return _unchanged(Outcome.DUPLICATE, reply)
    updated = _touch(_clear_current(reply, span.span_id), now_ns)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(_end(span, outcome, now_ns),))


# ---------------------------------------------------------------------------
# Approvals


@dataclass(frozen=True, slots=True)
class PauseWrite:
    """The prepared pause row: its stage, what it shows, and the canonical presentation."""

    shown: str
    prepared_revision: int
    stage: WriteStage


def pause(
    reply: Reply,
    span: Span,
    write: PauseWrite,
    *,
    approval_id: str,
    in_place: bool,
    now_ns: int,
) -> Transition:
    """Pause a reply for approval (DESIGN.md §6.4 ``pause``)."""
    stale = _stale_span(reply, span)
    if stale is not None:
        return stale
    recompute = _check_revision(reply, write.prepared_revision)
    if recompute is not None:
        return recompute
    if reply.unapplied_stop:
        # The Stop path owns this span now; it applies ``stopped`` instead.
        return _unchanged(Outcome.RECOMPUTE, reply)
    if write.stage is WriteStage.FINAL:
        msg = "A pause is never the reply's terminal write"
        raise _invalid(msg)
    updated = _set_state(reply, ReplyState.PAUSED, now_ns, approval_id=approval_id, presentation=write.shown)
    updated, row = _row(updated, span, write.stage, shown=write.shown, settles_sources=False)
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
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_set_state(reply, ReplyState.ACTIVE, now_ns, approval_id=None),
    )


ApprovalResult = Literal["failed", "finished"]


def approval_settled(  # noqa: PLR0911
    reply: Reply,
    last_span: Span | None,
    *,
    approval_id: str,
    result: ApprovalResult,
    disposition: FailureDisposition | None,
    now_ns: int,
) -> Transition:
    """Apply a continuation's finish (DESIGN.md §6.4 ``approval_failed`` and ``approval_finished``)."""
    resumed = last_span is not None and last_span.kind is SpanKind.APPROVAL_RESUME and last_span.ended
    owns = reply.approval_id == approval_id or (
        resumed and last_span is not None and last_span.approval_id == approval_id
    )
    if not owns:
        return _unchanged(Outcome.STALE, reply)
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    if result == "failed":
        if disposition == "superseded":
            return _unchanged(Outcome.DUPLICATE, reply)
        if reply.state is ReplyState.PAUSED or (reply.state is ReplyState.ACTIVE and resumed):
            state = ReplyState.CANCELLED if reply.stop_receipt_order is not None else ReplyState.FAILED
            updated = _set_state(reply, state, now_ns, approval_id=None)
            if state is ReplyState.CANCELLED:
                updated = _stop_applied(updated)
            return Transition(outcome=Outcome.APPLIED, reply=updated)
        return _unchanged(Outcome.STALE, reply)
    if reply.state is ReplyState.ACTIVE and resumed and last_span is not None:
        state = _state_for_span_outcome(last_span.outcome)
        if state is None:
            return _unchanged(Outcome.STALE, reply)
        return Transition(outcome=Outcome.APPLIED, reply=_set_state(reply, state, now_ns, approval_id=None))
    return _unchanged(Outcome.STALE, reply)


def _state_for_span_outcome(outcome: SpanOutcome | None) -> ReplyState | None:
    if outcome is SpanOutcome.COMPLETED:
        return ReplyState.COMPLETED
    if outcome is SpanOutcome.CANCELLED:
        return ReplyState.CANCELLED
    if outcome in {SpanOutcome.FAILED, SpanOutcome.SUPPRESSED}:
        return ReplyState.FAILED
    return None


def approval_released(reply: Reply, span: Span, *, now_ns: int) -> Transition:
    """A continuation released to replay ends its resume span with sources pending."""
    if span.ended:
        return _unchanged(Outcome.DUPLICATE, reply)
    updated = _set_state(_clear_current(reply, span.span_id), ReplyState.ACTIVE, now_ns, approval_id=None)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=(_end(span, SpanOutcome.RELEASED, now_ns),))


# ---------------------------------------------------------------------------
# Reply-authored events


@dataclass(frozen=True, slots=True)
class StopFacts:
    """Inputs of one durable Stop on a reply."""

    receipt_order: int
    # A newer edit already superseded the Stop.
    newer_edit: bool
    # The reply has a current span of the active generation that has not ended.
    span_live: bool


def stop(reply: Reply, current: Span | None, facts: StopFacts, *, now_ns: int) -> Transition:
    """Record a Stop on a reply (DESIGN.md §6.4 ``stop``)."""
    if facts.newer_edit:
        return _unchanged(Outcome.DUPLICATE, reply)
    if reply.stop_receipt_order is not None and facts.receipt_order <= reply.stop_receipt_order:
        return _unchanged(Outcome.DUPLICATE, reply)
    recorded = _bump(reply, now_ns, stop_receipt_order=facts.receipt_order)
    if reply.terminal:
        # The frozen terminal row wins; the Stop is satisfied by it.
        return Transition(outcome=Outcome.APPLIED, reply=_stop_applied(recorded))
    live = current if facts.span_live and current is not None and not current.ended else None
    if reply.state is ReplyState.PAUSED:
        if reply.approval_id is None:
            msg = f"Paused reply {reply.reply_id} has no approval"
            raise _invalid(msg)
        effects: list[Effect] = [FenceApproval(reply.approval_id, "cancelled_by_user")]
        if live is not None:
            # A response-local approval wait: cancel the waiting span too.
            effects.append(CancelSpan(live.span_id))
        effects.append(WakeApproval(reply.approval_id))
        return Transition(outcome=Outcome.APPLIED, reply=recorded, effects=tuple(effects))
    if live is not None:
        return Transition(outcome=Outcome.APPLIED, reply=recorded, effects=(CancelSpan(live.span_id),))
    # No span is running: the Stop applies directly to the last one.
    owed = OwedWrite(reply.last_span_id, NOTE_CANCELLED)
    cancelled = _set_state(_stop_applied(recorded), ReplyState.CANCELLED, now_ns, owed_write=owed)
    return Transition(outcome=Outcome.APPLIED, reply=cancelled, effects=(SettleSources(reply.last_span_id),))


def apply_pending_stop(reply: Reply, current: Span | None, facts: StopFacts, *, now_ns: int) -> Transition:
    """Apply a Stop that waited for its target event to be bound (``create_sent``)."""
    transition = stop(reply, current, facts, now_ns=now_ns)
    if transition.reply is None or not transition.applied:
        return transition
    return replace(
        transition,
        effects=(*transition.effects, TransferStop(facts.receipt_order, transition.reply.event_id or "")),
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
        msg = "An owed write belongs to the span it names"
        raise _invalid(msg)
    stage = WriteStage.EDIT if span_has_final else WriteStage.FINAL
    updated = _touch(reply, now_ns, owed_write=None, presentation=shown)
    updated, row = _row(updated, span, stage, shown=shown, settles_sources=False)
    return Transition(outcome=Outcome.APPLIED, reply=updated, row=row)


def dispatch_failed(reply: Reply, current: Span | None, *, error_text: str, now_ns: int) -> Transition:
    """A dispatch failed before or after a claim: the reply shows the error (DESIGN.md §6.4)."""
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    spans: tuple[Span, ...] = ()
    updated = reply
    stopped_first = reply.unapplied_stop
    if current is not None and not current.ended:
        outcome = SpanOutcome.CANCELLED if stopped_first else SpanOutcome.FAILED
        spans = (_end(current, outcome, now_ns),)
        updated = _clear_current(updated, current.span_id)
    if stopped_first:
        # A Stop recorded before the failure decides what the reply shows.
        owed = OwedWrite(reply.last_span_id, NOTE_CANCELLED)
        updated = _set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed)
    else:
        owed = OwedWrite(reply.last_span_id, NOTE_ERROR, error_text)
        updated = _set_state(updated, ReplyState.FAILED, now_ns, owed_write=owed)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=spans, effects=(SettleSources(reply.last_span_id),))


def sources_settled_without_reply(reply: Reply, span: Span, *, now_ns: int) -> Transition:
    """The span's sources settled without an answer (DESIGN.md §6.4)."""
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    if not (span.span_id == reply.current_span_id or span.outcome in {SpanOutcome.RELEASED, SpanOutcome.LOST}):
        return _unchanged(Outcome.STALE, reply)
    spans: tuple[Span, ...] = ()
    updated = _clear_current(reply, span.span_id)
    if not span.ended:
        spans = (_end(span, SpanOutcome.SUPPRESSED, now_ns),)
    if span.kind is SpanKind.REGENERATION and span.rollback is not None and not had_acknowledged_write(reply, span):
        return Transition(outcome=Outcome.APPLIED, reply=_restore(updated, span, now_ns), spans=spans)
    if reply.event_id is None or reply.placeholder_only:
        gone = _with_redactions(_set_state(updated, ReplyState.GONE, now_ns), *_visible_event_ids(reply))
        return Transition(outcome=Outcome.APPLIED, reply=gone, spans=spans)
    return Transition(outcome=Outcome.APPLIED, reply=_set_state(updated, ReplyState.FAILED, now_ns), spans=spans)


def sources_deleted(reply: Reply, current: Span | None, *, now_ns: int) -> Transition:
    """Every logical source of the reply's current work was deleted (DESIGN.md §6.4)."""
    if reply.terminal or reply.state is ReplyState.PAUSED:
        return _unchanged(Outcome.DUPLICATE, reply)
    if current is not None and current.kind is SpanKind.APPROVAL_RESUME:
        return _unchanged(Outcome.DUPLICATE, reply)
    effects: list[Effect] = []
    spans: tuple[Span, ...] = ()
    updated = reply
    if current is not None and not current.ended:
        effects.append(CancelSpan(current.span_id))
        effects.append(SettleSources(current.span_id))
        spans = (_end(current, SpanOutcome.CANCELLED, now_ns),)
        updated = _clear_current(updated, current.span_id)
    else:
        effects.append(SettleSources(reply.last_span_id))
    gone = _with_redactions(_set_state(updated, ReplyState.GONE, now_ns), *_visible_event_ids(reply))
    return Transition(outcome=Outcome.APPLIED, reply=gone, spans=spans, effects=tuple(effects))


def departed(reply: Reply, current: Span | None, *, now_ns: int) -> Transition:
    """The bot left the room: non-terminal replies end without touching Matrix (DESIGN.md §6.4)."""
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
        updated,
        now_ns,
        state=ReplyState.GONE,
        redaction_pending=(),
        stop_button_event_id=None,
        owed_write=None,
        approval_id=None,
    )
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=spans, effects=effects)


@dataclass(frozen=True, slots=True)
class OwnerLostFacts:
    """Inputs of the startup ownership check for one reply."""

    active_generation: str
    sources_pending: bool


def owner_lost(reply: Reply, last: Span, facts: OwnerLostFacts, *, now_ns: int) -> Transition:  # noqa: PLR0911
    """Settle a reply whose span an older bot instance left behind (DESIGN.md §6.4 ``owner_lost``)."""
    if reply.state is not ReplyState.ACTIVE:
        return _unchanged(Outcome.DUPLICATE, reply)
    orphaned = (last.outcome is None and last.bot_generation != facts.active_generation) or (
        last.outcome is SpanOutcome.LOST and not facts.sources_pending
    )
    if not orphaned:
        return _unchanged(Outcome.DUPLICATE, reply)
    spans: list[Span] = []
    updated = reply
    if last.outcome is None:
        if last.kind is SpanKind.APPROVAL_RESUME:
            # Main's approval recovery still owns this resume and reports through approval events.
            return _unchanged(Outcome.DUPLICATE, reply)
        last = _end(last, SpanOutcome.LOST, now_ns)
        spans.append(last)
        updated = _clear_current(updated, last.span_id)
    if reply.unapplied_stop:
        owed = OwedWrite(last.span_id, NOTE_CANCELLED)
        updated = _set_state(_stop_applied(updated), ReplyState.CANCELLED, now_ns, owed_write=owed)
        return Transition(
            outcome=Outcome.APPLIED,
            reply=updated,
            spans=tuple(spans),
            effects=(SettleSources(last.span_id),),
        )
    if facts.sources_pending:
        # Replay claims it; the lost span marks where the claim continues.
        return Transition(outcome=Outcome.APPLIED, reply=_touch(updated, now_ns), spans=tuple(spans))
    if last.kind is SpanKind.REGENERATION and last.rollback is not None:
        return Transition(outcome=Outcome.APPLIED, reply=_restore(updated, last, now_ns), spans=tuple(spans))
    owed = OwedWrite(last.span_id, NOTE_RESTART)
    updated = _set_state(updated, ReplyState.FAILED, now_ns, owed_write=owed)
    return Transition(outcome=Outcome.APPLIED, reply=updated, spans=tuple(spans))


def removed_entity(reply: Reply, current: Span | None, *, now_ns: int) -> Transition:
    """End a reply whose entity left the configuration, without writing to Matrix (DESIGN.md §9.3)."""
    if reply.terminal:
        return _unchanged(Outcome.DUPLICATE, reply)
    spans: tuple[Span, ...] = ()
    updated = reply
    if current is not None and not current.ended:
        spans = (_end(current, SpanOutcome.LOST, now_ns),)
        updated = _clear_current(updated, current.span_id)
    return Transition(
        outcome=Outcome.APPLIED,
        reply=_set_state(updated, ReplyState.FAILED, now_ns, approval_id=None, owed_write=None),
        spans=spans,
    )


def redactions_done(reply: Reply, event_ids: tuple[str, ...], *, now_ns: int) -> Transition:
    """Drop event ids whose redaction landed or was refused because the event is gone."""
    remaining = tuple(event_id for event_id in reply.redaction_pending if event_id not in event_ids)
    if remaining == reply.redaction_pending:
        return _unchanged(Outcome.DUPLICATE, reply)
    return Transition(outcome=Outcome.APPLIED, reply=_touch(reply, now_ns, redaction_pending=remaining))


def record_stop_button(reply: Reply, *, event_id: str, now_ns: int) -> Transition:
    """Record the Stop button reaction sent for an active reply, or queue it for removal."""
    if reply.state is not ReplyState.ACTIVE:
        return Transition(outcome=Outcome.APPLIED, reply=_touch(_with_redactions(reply, event_id), now_ns))
    return Transition(outcome=Outcome.APPLIED, reply=_touch(reply, now_ns, stop_button_event_id=event_id))
