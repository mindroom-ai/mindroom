"""Interleaved reply events keep the lifecycle invariants of docs/architecture/reply-messages.md.

A state machine drives one reply through every rule in random order -- claims,
progress, durable rows and their acknowledgements or failures, Stops at any
point, pauses and every approval outcome, regenerations, deletions,
departures, entity removal, retention, and bot restarts -- and checks after
every step that the records still describe a reply someone owns. Approval
continuations are rows, as the store keeps them: the reply's hold is derived
from them, a claim on a held reply goes through the shared blocking
predicate, and an approval finishes only once its FINAL resolved or an edit
superseded it. Each run ends by draining every owner and checking that
nothing is left waiting.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from mindroom import reply_lifecycle as rl
from mindroom.reply_lifecycle import (
    CancelSpan,
    ClaimContext,
    ClaimRequest,
    FenceApproval,
    Outcome,
    Reply,
    ReplyState,
    SettleSources,
    Span,
    SpanKind,
    SpanOutcome,
    SpanSources,
    StopFacts,
    TerminalWrite,
    WakeApproval,
    WriteFacts,
    WriteStage,
    _RowIntent,
)

_SETTLING_OUTCOMES = frozenset(
    {
        SpanOutcome.COMPLETED,
        SpanOutcome.CANCELLED,
        SpanOutcome.FAILED,
        SpanOutcome.SUPPRESSED,
        SpanOutcome.RESTORED,
    },
)
# A regeneration that ends this way gave up on its answer without a retry.
_ABANDONED_OUTCOMES = frozenset({SpanOutcome.CANCELLED, SpanOutcome.FAILED, SpanOutcome.SUPPRESSED})
# A span running for its approval that ends this way has no answer: the runtime fails the approval.
_UNANSWERED_OUTCOMES = frozenset(
    {SpanOutcome.CANCELLED, SpanOutcome.FAILED, SpanOutcome.SUPPRESSED, SpanOutcome.RELEASED},
)
_DRAIN_ROUNDS = 1000


@dataclass
class _Continuation:
    """One approval_continuations row: the span whose pause created it, and how far it got."""

    approval_id: str
    # The span whose first pause created it, which the store keeps as it advances.
    paused_span_id: str
    delivery_id: str
    # The span whose latest pause waits for a decision.
    waiting_span_id: str = ""
    generation: int = 0
    claim_span_id: str | None = None
    state: str = "waiting"  # waiting, ready, failing
    disposition: rl.FailureDisposition | None = None

    @property
    def superseded(self) -> bool:
        return self.state == "failing" and self.disposition == "superseded"


@dataclass
class _Row:
    intent: _RowIntent
    creates_event: bool
    delivery_id: str
    # Only a create that showed the placeholder alone is acknowledged as such.
    placeholder_only: bool = False


@dataclass
class _Model:
    generation: int = 1
    counter: int = 0
    receipt_order: int = 0
    edits: int = 0
    reply: Reply | None = None
    spans: dict[str, Span] = field(default_factory=dict)
    # Journal sources settled, as the store settles a span's pending events.
    settled: set[str] = field(default_factory=set)
    cancel_requested: set[str] = field(default_factory=set)
    rows: list[_Row] = field(default_factory=list)
    # Each delivery id's FINAL row: pending, acknowledged, or refused for good.
    finals: dict[str, str] = field(default_factory=dict)
    continuations: dict[str, _Continuation] = field(default_factory=dict)
    # Edits whose claim waited for the reply to be free.
    deferred: list[str] = field(default_factory=list)
    # Each edit's receipt order, which its claims carry however late they run.
    edit_orders: dict[str, int] = field(default_factory=dict)
    removed: bool = False
    # Logical sources the user deleted: no settlement answers their turn.
    deleted: set[str] = field(default_factory=set)
    # Edits a completed answer consumed, as the turn ledger commits them.
    consumed: set[str] = field(default_factory=set)
    # Spans whose sources settled unanswered before anything consumed their edit: by a departure, or by an
    # approval that finished with no owner left to answer it.
    unanswered: set[str] = field(default_factory=set)
    # The approval whose pause each paused span's continuation was created by.
    paused_by: dict[str, str] = field(default_factory=dict)
    # The turn ledger recorded the turn answered, which suppresses a later replay of its original source.
    turn_answered: bool = False
    # The bot left the room, which drops everything owed to it.
    left: bool = False
    # What each write of a reply, by sequence, may show: one that ends the reply, work in progress, or refused.
    writes: dict[tuple[str, int], str] = field(default_factory=dict)


class ReplyLifecycleMachine(RuleBasedStateMachine):
    """Drive one reply through random interleavings of every lifecycle event."""

    def __init__(self) -> None:
        super().__init__()
        self.model = _Model()

    # --- plumbing -----------------------------------------------------------

    @property
    def generation(self) -> str:
        """Return the active bot instance's generation."""
        return f"gen-{self.model.generation}"

    def _next(self, prefix: str) -> str:
        self.model.counter += 1
        return f"{prefix}-{self.model.counter}"

    def _now(self) -> int:
        self.model.counter += 1
        return self.model.counter

    def _bot(self) -> bool:
        """Return whether the entity still has a bot that runs its replies."""
        return not self.model.removed

    def _settle(self, span_id: str) -> None:
        self.model.settled.update(self.model.spans[span_id].sources.pending)

    def _is_settled(self, span_id: str) -> bool:
        return set(self.model.spans[span_id].sources.pending) <= self.model.settled

    def _current(self) -> Span | None:
        reply = self.model.reply
        if reply is None or reply.current_span_id is None:
            return None
        return self.model.spans[reply.current_span_id]

    def _last(self) -> Span | None:
        reply = self.model.reply
        return None if reply is None else self.model.spans[reply.last_span_id]

    def _live(self) -> Span | None:
        current = self._current()
        if not self._bot() or current is None or current.ended or current.bot_generation != self.generation:
            return None
        return current

    def _held_by(self) -> str | None:
        """Return the approval that holds the reply, as the store reads it from the continuations."""
        for continuation in self.model.continuations.values():
            if continuation.paused_span_id in self.model.spans and not continuation.superseded:
                return continuation.approval_id
        return None

    def _derive_hold(self) -> None:
        if self.model.reply is not None:
            self.model.reply = replace(self.model.reply, approval_id=self._held_by())

    def _unresolved_rows(self) -> bool:
        return bool(self.model.rows)

    def _apply(self, transition: rl.Transition) -> rl.Transition:
        if transition.outcome is Outcome.RECOMPUTE:
            msg = "rendered from the current revision, yet asked to recompute"
            raise AssertionError(msg)
        before = self.model.reply
        if transition.reply is not None:
            self.model.reply = transition.reply
        for span in transition.spans:
            self.model.spans[span.span_id] = span
            if before is not None and span.ended:
                self._check_abandonment(before, span, transition)
        for effect in transition.effects:
            self._apply_effect(effect)
        self._derive_hold()
        self._note_write(before, transition)
        if transition.row is not None:
            reply = self.model.reply
            assert reply is not None
            span = self.model.spans[transition.row.span_id]
            creates = reply.event_id is None and transition.row.stage is not WriteStage.EDIT
            self.model.rows.append(_Row(transition.row, creates_event=creates, delivery_id=span.delivery_id))
            if transition.row.stage is WriteStage.FINAL:
                self.model.finals[span.delivery_id] = "pending"
        return transition

    def _note_write(self, before: Reply | None, transition: rl.Transition) -> None:
        """Record what a write the transition recorded may show: a row's, or a progress edit sent ahead of its record."""
        reply = self.model.reply
        if reply is None or transition.outcome is not Outcome.APPLIED:
            return
        if transition.row is not None:
            self.model.writes[(reply.reply_id, transition.row.sequence)] = "ends" if reply.terminal else "open"
        elif (
            before is not None
            and before.reply_id == reply.reply_id
            and reply.possibly_shown_seq is not None
            and reply.possibly_shown_seq > (before.possibly_shown_seq or 0)
        ):
            self.model.writes.setdefault((reply.reply_id, reply.possibly_shown_seq), "open")

    def _check_abandonment(self, before: Reply, span: Span, transition: rl.Transition) -> None:
        """I11, I12, I14: an abandoned regeneration restores exactly when nothing it wrote may show."""
        if span.kind is not SpanKind.REGENERATION or span.rollback is None or self.model.removed:
            return
        wrote = before.possibly_shown_seq is not None and before.possibly_shown_seq > span.base_sequence
        if span.outcome is SpanOutcome.RESTORED:
            # I12: never back to the rollback once a write that may show was recorded.
            assert not wrote, (before, span)
            # A Stop never lets unfinished work the regeneration replaced run again.
            assert not before.unapplied_stop or span.rollback.state in rl._TERMINAL_STATES, (before, span)
            return
        after = self.model.reply
        assert after is not None
        # An exit whose own terminal row ends the reply, as a finish that found the Stop, is no abandonment; a span
        # a restart lost is abandoned only when the reply ends with it instead of waiting for a retry.
        abandoned = span.outcome in _ABANDONED_OUTCOMES or (span.outcome is SpanOutcome.LOST and after.terminal)
        if not abandoned or transition.row is not None:
            return
        shown = before.event_id is not None and not before.placeholder_only
        if not wrote and shown and before.approval_id is None and span.rollback.state in rl._TERMINAL_STATES:
            # I11: a finished answer the regeneration never replaced stands.
            msg = f"abandoned regeneration {span.span_id} did not restore {span.rollback.state}"
            raise AssertionError(msg)
        if wrote and after.terminal:
            # I14: Matrix is brought to match the records; a create still in flight is removed once acknowledged.
            converges = (
                transition.row is not None
                or after.owed_write is not None
                or bool(after.redaction_pending)
                or (after.event_id is None and self._unresolved_rows())
            )
            assert converges, (before, after, span)

    def _apply_effect(self, effect: rl.Effect) -> None:
        match effect:
            case SettleSources(span_id=span_id, consumes_edit=consumes_edit, answered=answered):
                settled = self.model.spans[span_id]
                # As the store does: nothing answers a turn whose every message the user deleted.
                answered = answered and not set(settled.sources.logical) <= self.model.deleted
                if consumes_edit and answered and settled.prepared_edit is not None:
                    # S5: only an answer the reply completed consumes the edit a regeneration selected.
                    reply = self.model.reply
                    assert reply is not None
                    assert self.model.spans[reply.last_span_id].outcome is SpanOutcome.COMPLETED, reply
                    self.model.consumed.add(settled.prepared_edit)
                if answered:
                    self.model.turn_answered = True
                self._settle(span_id)
            case CancelSpan(span_id=span_id):
                self.model.cancel_requested.add(span_id)
            case FenceApproval(approval_id=approval_id, disposition=disposition):
                self._fence(approval_id, disposition)
            case WakeApproval():
                # The wake only makes the settlement run sooner; the drain runs it regardless.
                pass
            case _:
                msg = f"unmodelled effect {effect!r}"
                raise AssertionError(msg)

    def _fence(self, approval_id: str, disposition: rl.FailureDisposition) -> None:
        """Fence a continuation as the store does: a FINAL still delivered wins, supersession replaces a failure."""
        continuation = self.model.continuations.get(approval_id)
        if continuation is None or self.model.finals.get(continuation.delivery_id) in {"pending", "acknowledged"}:
            return
        if continuation.state in {"waiting", "ready"} or (
            disposition == "superseded" and continuation.state == "failing"
        ):
            continuation.state = "failing"
            continuation.disposition = disposition

    def _claim(
        self,
        *,
        edit: str | None = None,
        approval: _Continuation | None = None,
        replay_of: Span | None = None,
    ) -> rl.Transition:
        reply = self.model.reply
        if approval is not None:
            paused = self.model.spans[approval.paused_span_id]
            delivery_id, sources = paused.delivery_id, paused.sources
        elif edit is not None:
            delivery_id, sources = edit, SpanSources(pending=(edit,), logical=("$source",))
        elif replay_of is not None:
            delivery_id, sources = replay_of.delivery_id, replay_of.sources
        else:
            delivery_id, sources = "$source", SpanSources(pending=("$source",), logical=("$source",))
        request = ClaimRequest(
            span_id=self._next("span"),
            delivery_id=delivery_id,
            sources=sources,
            bot_generation=self.generation,
            now_ns=self._now(),
            new_reply_id=self._next("reply"),
            entity_name="agent",
            room_id="!room",
            thread_id=None,
            membership_epoch=1,
            empty_presentation="empty",
            driving_edit_id=edit,
            approval_id=None if approval is None else approval.approval_id,
            # A regeneration carries the edit it selected, which its completed answer consumes.
            prepared_edit=edit,
        )
        context = ClaimContext(
            reply=reply,
            last_span=self._last(),
            current_span=self._current(),
            interactive_span=None,
            durable_write_debt=reply is not None and self._unresolved_rows(),
            active_generation=self.generation,
            edit_receipt_order=None if edit is None else self.model.edit_orders[edit],
        )
        transition = rl.claim(request, context)
        if transition.outcome is Outcome.DEFERRED and edit is not None and edit not in self.model.deferred:
            # An approval's claim is retried by its recovery and a replay by journal replay; an edit waits here.
            self.model.deferred.append(edit)
        if transition.reply is not None and reply is not None and transition.reply.reply_id != reply.reply_id:
            # A regeneration of a gone reply starts a new record. The old one stays while a superseded
            # approval names it, until that approval's cleanup, which this model runs first.
            for continuation in tuple(self.model.continuations.values()):
                assert continuation.superseded, continuation
                self._finish_approval(continuation, owner_available=True, retry=False)
            self.model.spans = {}
            self.model.settled = set()
            self.model.rows = []
            self.model.finals = {}
        transition = self._apply(transition)
        if transition.claimed is not None and approval is not None:
            approval.claim_span_id = transition.claimed.span_id
        return transition

    def _held_by_any(self) -> bool:
        return any(c.paused_span_id in self.model.spans for c in self.model.continuations.values())

    def _terminal_write(self, requested: ReplyState, *, consumes_edit: bool = False) -> TerminalWrite:
        reply = self.model.reply
        assert reply is not None
        return TerminalWrite(
            shown=f"shown-{reply.revision}",
            prepared_revision=reply.revision,
            state=rl._expected_terminal_state(reply, requested),
            consumes_edit=consumes_edit,
        )

    def _runs_for(self, span: Span) -> _Continuation | None:
        """Return the continuation a span runs for: its resume, or the span its approval let go on in place."""
        for continuation in self.model.continuations.values():
            if continuation.claim_span_id == span.span_id:
                return continuation
        return None

    # --- claims -------------------------------------------------------------

    @precondition(lambda self: self._bot() and self.model.reply is None)
    @rule()
    def start_turn(self) -> None:
        """Start turn."""
        self._claim()

    @precondition(
        lambda self: (
            self._bot()
            and self.model.reply is not None
            and self.model.reply.state is ReplyState.ACTIVE
            and self.model.reply.current_span_id is None
            and self.model.reply.approval_id is None
            and self.model.spans[self.model.reply.last_span_id].outcome in rl._SOURCES_PENDING_OUTCOMES
            and not self._is_settled(self.model.reply.last_span_id)
        ),
    )
    @rule()
    def replay(self) -> None:
        """Replay the sources the reply's last span left pending; an approval's are its recovery's, not replay's."""
        last = self._last()
        assert last is not None
        if "$source" in self.model.deleted:
            # The source gate finds the message deleted, and the reply ends as a deletion ends it.
            self._apply(rl.sources_deleted(self.model.reply, last, now_ns=self._now()))  # type: ignore[arg-type]
        elif last.delivery_id in self.model.edit_orders:
            # The pending source is an edit, a regeneration's or its approved resume's: the regenerator replays it
            # with the edit it selected.
            self._claim(edit=last.delivery_id)
        else:
            self._claim(replay_of=last)

    @precondition(
        lambda self: (
            self._bot()
            and self.model.reply is not None
            # The regenerator regenerates only an answer with an event.
            and self.model.reply.event_id is not None
            # A span this instance runs holds the conversation; one an older instance left is retired by the claim.
            and (self._current() is None or self._current().bot_generation != self.generation)
            and "$source" not in self.model.deleted
        ),
    )
    @rule()
    def regenerate(self) -> None:
        """An edit regenerates the reply, whatever approval holds it."""
        self.model.receipt_order += 1
        self.model.edits += 1
        edit = f"$edit-{self.model.edits}"
        self.model.edit_orders[edit] = self.model.receipt_order
        self._claim(edit=edit)

    @precondition(lambda self: self._bot() and bool(self.model.deferred))
    @rule()
    def retry_deferred_claim(self) -> None:
        """What held an edit back resolved, or its wake fired: the edit claims again."""
        self._retry_deferred()

    def _retry_deferred(self) -> None:
        reply = self.model.reply
        if reply is None:
            self.model.deferred.clear()
            return
        if "$source" in self.model.deleted:
            # The regenerator turns away an edit of a deleted message; its event settles as ignored.
            self.model.settled.update(self.model.deferred)
            self.model.deferred.clear()
            return
        for edit in tuple(self.model.deferred):
            if edit in self.model.settled:
                # Its source settled meanwhile; the source gate turns the retry away.
                self.model.deferred.remove(edit)
                continue
            if rl.claim_blocked(reply, durable_write_debt=self._unresolved_rows(), driving_edit=True):
                return
            self.model.deferred.remove(edit)
            if reply.current_span_id is not None:
                # The reply runs again; the edit waits behind it as a newer turn would.
                self.model.deferred.append(edit)
                return
            self._claim(edit=edit)
            reply = self.model.reply
            assert reply is not None

    # --- progress and durable rows -----------------------------------------

    @precondition(
        lambda self: (
            self._live() is not None and self.model.reply is not None and self.model.reply.event_id is not None
        ),
    )
    @rule(previous_ok=st.booleans())
    def write_ahead(self, previous_ok: bool) -> None:
        """A direct progress edit of the event the reply's create bound; the create is always a durable row."""
        span = self._live()
        assert span is not None
        self._apply(
            rl.write_ahead(
                self.model.reply,  # type: ignore[arg-type]
                span,
                shown="progress",
                previous=rl.ProgressConfirmation(event_id="$reply", placeholder_only=False) if previous_ok else None,
                active_generation=self.generation,
                durable_write_debt=self._unresolved_rows(),
                now_ns=self._now(),
            ),
        )

    @precondition(
        lambda self: self._live() is not None and self.model.reply is not None and self.model.reply.event_id is None,
    )
    @rule(placeholder=st.booleans())
    def enqueue_initial(self, placeholder: bool) -> None:
        """Enqueue initial."""
        if any(row.intent.stage is WriteStage.INITIAL for row in self.model.rows):
            return
        reply = self.model.reply
        span = self._live()
        assert reply is not None
        assert span is not None
        self._apply(
            rl.enqueue_initial(
                reply,
                span,
                shown="initial",
                placeholder_only=placeholder,
                prepared_revision=reply.revision,
                now_ns=self._now(),
            ),
        )
        self.model.rows[-1].placeholder_only = placeholder

    @precondition(lambda self: self._bot() and bool(self.model.rows))
    @rule()
    def acknowledge_row(self) -> None:
        """Acknowledge row."""
        self._acknowledge_row()

    def _acknowledge_row(self) -> None:
        row = self.model.rows.pop(0)
        reply = self.model.reply
        assert reply is not None
        creates = row.creates_event and reply.event_id is None
        self._apply(
            rl.write_acknowledged(
                reply,
                WriteFacts(row.intent.stage, row.intent.sequence, row.intent.span_id, creates, row.placeholder_only),
                event_id="$reply",
                membership_current=True,
                now_ns=self._now(),
            ),
        )
        if row.intent.stage is WriteStage.FINAL:
            # The approval runtime finishes an approval whose FINAL this resolved in a later transaction,
            # so claims woken now can still meet the reply it holds.
            self.model.finals[row.delivery_id] = "acknowledged"
        self._retry_deferred()

    @precondition(lambda self: self._bot() and bool(self.model.rows))
    @rule(reason=st.sampled_from(["delivery_failed", "too_large"]))
    def fail_row(self, reason: str) -> None:
        """Matrix refuses a row for good."""
        row = self.model.rows[0]
        span = self.model.spans[row.intent.span_id]
        if row.intent.stage is WriteStage.FINAL and not span.ended:
            return
        self.model.rows.pop(0)
        reply = self.model.reply
        assert reply is not None
        refused = "refused" if row.intent.stage is WriteStage.FINAL else "refused_edit"
        self.model.writes[(reply.reply_id, row.intent.sequence)] = refused
        # A later span claims the reply only after its rows resolved, so a refused FINAL is the last span's.
        assert row.intent.stage is not WriteStage.FINAL or span.span_id == reply.last_span_id, (row, reply)
        restorable = (
            row.intent.stage is WriteStage.FINAL
            and span.kind is SpanKind.REGENERATION
            and span.rollback is not None
            and span.span_id == reply.last_span_id
            and not reply.placeholder_only
            and reply.event_id is not None
            and row.intent.sequence - 1 <= span.base_sequence
        )
        self._apply(
            rl.write_failed(
                reply,
                span,
                rl.FailedWrite(
                    WriteFacts(row.intent.stage, row.intent.sequence, row.intent.span_id, row.creates_event, False),
                    reason,
                ),
                now_ns=self._now(),
            ),
        )
        # The Stop's own refused row restores only a finished answer.
        finished_only = span.outcome is SpanOutcome.CANCELLED
        if (
            restorable
            and span.rollback is not None
            and not (finished_only and span.rollback.state not in rl._TERMINAL_STATES)
        ):
            # I11: only the refused FINAL was written, so the answer it would replace stands as the room shows it;
            # a paused one does not get its approval back.
            restored = self.model.reply
            assert restored is not None
            expected = ReplyState.FAILED if span.rollback.state is ReplyState.PAUSED else span.rollback.state
            assert restored.state is expected, (restored, span)
            assert (restored.possibly_shown_seq or 0) <= span.base_sequence, (restored, span)
        if row.intent.stage is WriteStage.FINAL:
            # As for an acknowledgement, the approval runtime settles the approval later.
            self.model.finals[row.delivery_id] = "refused"
        if row.intent.stage is WriteStage.INITIAL:
            # A refused create also fails the reply's edit rows waiting on it.
            self.model.rows = [waiting for waiting in self.model.rows if waiting.intent.stage is not WriteStage.EDIT]
        self._retry_deferred()

    @precondition(
        lambda self: self._bot() and self.model.reply is not None and self.model.reply.owed_write is not None,
    )
    @rule()
    def flush_owed(self) -> None:
        """Flush owed."""
        self._flush_owed()

    def _flush_owed(self) -> None:
        reply = self.model.reply
        assert reply is not None
        assert reply.owed_write is not None
        span = self.model.spans[reply.owed_write.span_id]
        self._apply(
            rl.flush_owed_write(
                reply,
                span,
                shown="note",
                prepared_revision=reply.revision,
                span_has_final=span.delivery_id in self.model.finals,
                now_ns=self._now(),
            ),
        )

    @precondition(lambda self: self._bot() and bool(self.model.reply and self.model.reply.redaction_pending))
    @rule()
    def redact(self) -> None:
        """Redact."""
        reply = self.model.reply
        assert reply is not None
        self._apply(rl.redactions_done(reply, reply.redaction_pending, now_ns=self._now()))

    @precondition(lambda self: self._live() is not None)
    @rule()
    def stop_button(self) -> None:
        """Stop button."""
        self._apply(
            rl.record_stop_button(
                self.model.reply,
                event_id=self._next("$button"),
                membership_current=True,
                now_ns=self._now(),
            ),
        )  # type: ignore[arg-type]

    # --- span exits ---------------------------------------------------------

    def _waiting_in_place(self) -> bool:
        reply = self.model.reply
        return reply is not None and reply.state is ReplyState.PAUSED

    @precondition(lambda self: self._live() is not None)
    @rule()
    def finish(self) -> None:
        """Finish."""
        span = self._live()
        assert span is not None
        if self._waiting_in_place():
            return
        # A run that completed asks its answer to consume the edit it selected.
        write = self._terminal_write(ReplyState.COMPLETED, consumes_edit=True)
        self._span_exit(span, rl.finish(self.model.reply, span, write, now_ns=self._now()))  # type: ignore[arg-type]

    @precondition(lambda self: self._live() is not None)
    @rule()
    def finish_with_a_payload_rendered_before_a_stop(self) -> None:
        """Finish with a payload rendered before a stop."""
        span = self._live()
        reply = self.model.reply
        assert span is not None
        assert reply is not None
        if reply.state is ReplyState.PAUSED:
            return
        prepared = self._terminal_write(ReplyState.COMPLETED)
        self.stop(lag=0)
        if self._live() is None:
            return
        transition = rl.finish(self.model.reply, span, prepared, now_ns=self._now())  # type: ignore[arg-type]
        changed = self.model.reply is not None and self.model.reply.revision != prepared.prepared_revision
        if transition.outcome is Outcome.RECOMPUTE:
            assert changed
            return
        self._span_exit(span, transition)

    @precondition(lambda self: self._live() is not None)
    @rule(phase=st.sampled_from(["pre_delivery", "delivery"]), note=st.booleans())
    def fail(self, phase: rl._FailurePhase, note: bool) -> None:
        """Fail."""
        span = self._live()
        reply = self.model.reply
        assert span is not None
        assert reply is not None
        if reply.state is ReplyState.PAUSED:
            return
        write: TerminalWrite | None = None
        if phase == "delivery" or note or reply.unapplied_stop:
            requested = ReplyState.FAILED if phase == "delivery" else ReplyState.ACTIVE
            write = self._terminal_write(requested)
        self._span_exit(span, rl.fail(reply, span, write, phase=phase, now_ns=self._now()))

    @precondition(
        lambda self: (
            self._live() is not None
            and self._live().kind is not SpanKind.APPROVAL_RESUME
            and self.model.reply is not None
            and self.model.reply.unapplied_stop
        ),
    )
    @rule(phase=st.sampled_from(["pre_delivery", "delivery"]))
    def fail_rendered_before_the_span_saw_its_stop(self, phase: rl._FailurePhase) -> None:
        """A failure note rendered at the Stop's revision, before the span saw the Stop, renders again."""
        span = self._live()
        reply = self.model.reply
        assert span is not None
        assert reply is not None
        if reply.state is ReplyState.PAUSED:
            return
        requested = ReplyState.FAILED if phase == "delivery" else ReplyState.ACTIVE
        write = TerminalWrite(shown=f"shown-{reply.revision}", prepared_revision=reply.revision, state=requested)
        transition = rl.fail(reply, span, write, phase=phase, now_ns=self._now())
        assert transition.outcome is Outcome.RECOMPUTE
        assert transition.reply == reply

    @precondition(lambda self: self._live() is not None)
    @rule(reason=st.sampled_from(["suppressed", "hook_failed"]))
    def suppress(self, reason: rl._SuppressReason) -> None:
        """Suppress."""
        span = self._live()
        assert span is not None
        if self._waiting_in_place():
            return
        self._span_exit(span, rl.suppress(self.model.reply, span, reason=reason, now_ns=self._now()))  # type: ignore[arg-type]

    @precondition(lambda self: self._live() is not None)
    @rule(superseded=st.booleans())
    def release(self, superseded: bool) -> None:
        """Release."""
        span = self._live()
        assert span is not None
        if self._waiting_in_place():
            return
        if span.kind is SpanKind.APPROVAL_RESUME:
            # A shutdown hands the resume back to its approval's recovery, which releases it to replay.
            continuation = self._runs_for(span)
            assert continuation is not None
            self._release_approval(continuation)
            return
        outcome = SpanOutcome.SUPERSEDED if superseded else SpanOutcome.RELEASED
        self._span_exit(span, rl.release(self.model.reply, span, outcome=outcome, now_ns=self._now()))  # type: ignore[arg-type]

    @precondition(
        lambda self: (
            self._live() is not None
            and self._live().span_id in self.model.cancel_requested
            and self.model.reply is not None
            and self.model.reply.unapplied_stop
        ),
    )
    @rule()
    def span_observes_its_stop(self) -> None:
        """Span observes its stop."""
        span = self._live()
        reply = self.model.reply
        assert span is not None
        assert reply is not None
        write = None
        if span.kind is not SpanKind.APPROVAL_RESUME:
            write = self._terminal_write(ReplyState.CANCELLED)
        self._span_exit(span, rl.stopped(reply, span, write, now_ns=self._now()))

    def _span_exit(self, span: Span, transition: rl.Transition) -> None:
        """Apply a span's exit; a span running for its approval that ended without its answer fails that approval."""
        self._apply(transition)
        continuation = self._runs_for(span)
        if continuation is None or continuation.state != "ready":
            return
        ended = self.model.spans[span.span_id]
        if ended.outcome in _UNANSWERED_OUTCOMES and self.model.finals.get(continuation.delivery_id) is None:
            continuation.state = "failing"
            continuation.disposition = "cancelled_by_user" if ended.outcome is SpanOutcome.CANCELLED else "failed"

    # --- approvals ----------------------------------------------------------

    @precondition(lambda self: self._live() is not None)
    @rule(in_place=st.booleans())
    def pause(self, in_place: bool) -> None:
        """A span pauses for approval: a new continuation, or the next generation of the one it runs for."""
        reply = self.model.reply
        span = self._live()
        assert reply is not None
        assert span is not None
        if reply.state is ReplyState.PAUSED:
            return
        runs_for = self._runs_for(span)
        if runs_for is None and reply.approval_id is not None:
            # Only the span an approval let run may pause the reply it holds again.
            return
        transition = rl.pause(
            reply,
            span,
            rl.PauseWrite(
                shown="paused",
                prepared_revision=reply.revision,
                stage=WriteStage.EDIT if reply.event_id is not None else WriteStage.INITIAL,
            ),
            in_place=in_place,
            now_ns=self._now(),
        )
        if transition.outcome is Outcome.STOPPED:
            assert reply.unapplied_stop
            return
        if runs_for is not None:
            # The run advances and waits again; it still names the span whose pause created it.
            runs_for.waiting_span_id = span.span_id
            runs_for.generation += 1
            runs_for.claim_span_id = None
            runs_for.state = "waiting"
        else:
            approval_id = self._next("approval")
            self.model.continuations[approval_id] = _Continuation(
                approval_id,
                span.span_id,
                span.delivery_id,
                waiting_span_id=span.span_id,
            )
            self.model.paused_by[span.span_id] = approval_id
        self._apply(transition)

    @precondition(
        lambda self: (
            self._bot() and any(continuation.state == "waiting" for continuation in self.model.continuations.values())
        ),
    )
    @rule(claim_now=st.booleans(), data=st.data())
    def decide(self, claim_now: bool, data: st.DataObject) -> None:
        """A human decides, or the card expires; the run resumes with the decisions and usually claims its reply at once.

        A denial resumes it too: the run gets the refusal and answers, or pauses again.
        """
        waiting = [c for c in self.model.continuations.values() if c.state == "waiting"]
        continuation = data.draw(st.sampled_from(waiting))
        continuation.state = "ready"
        if claim_now:
            self._claim_ready()

    @precondition(
        lambda self: (
            self._bot() and any(continuation.state == "waiting" for continuation in self.model.continuations.values())
        ),
    )
    @rule()
    def pause_shown_and_approved(self) -> None:
        """The common path: Matrix takes the pause, the human approves at once, and the run claims its reply."""
        while self.model.rows:
            self._acknowledge_row()
        for continuation in self.model.continuations.values():
            if continuation.state == "waiting":
                continuation.state = "ready"
        self._claim_ready()

    @precondition(
        lambda self: (
            self._bot()
            and any(
                continuation.state == "ready" and continuation.claim_span_id is None
                for continuation in self.model.continuations.values()
            )
        ),
    )
    @rule()
    def claim_approval(self) -> None:
        """An approved run claims its reply: a resume, or the span that waits in place goes on."""
        self._claim_ready()

    def _claim_ready(self) -> None:
        for continuation in tuple(self.model.continuations.values()):
            if continuation.state != "ready" or continuation.claim_span_id is not None:
                continue
            reply = self.model.reply
            if reply is None or reply.approval_id != continuation.approval_id or reply.state is not ReplyState.PAUSED:
                continue
            current = self._live()
            if current is not None and current.span_id == continuation.waiting_span_id:
                transition = self._apply(
                    rl.resumed_in_place(reply, current, approval_id=continuation.approval_id, now_ns=self._now()),
                )
                if transition.applied:
                    continuation.claim_span_id = current.span_id
                continue
            if reply.current_span_id is not None:
                # An older instance's in-place wait still holds the reply; its recovery ends it first.
                continue
            self._claim(approval=continuation)

    @precondition(
        lambda self: (
            self._bot() and any(c.state == "failing" or self._may_finish(c) for c in self.model.continuations.values())
        ),
    )
    @rule()
    def settle_approvals(self) -> None:
        """The approval runtime settles failing approvals and finishes those whose FINAL resolved."""
        self._settle_approvals()

    def _settle_approvals(self) -> None:
        for continuation in tuple(self.model.continuations.values()):
            if continuation.state == "ready" and self.model.finals.get(continuation.delivery_id) == "refused":
                # Matrix refused the answer for good: the runtime settles the run as a failure.
                continuation.state = "failing"
                continuation.disposition = "failed"
            if continuation.state == "failing" and not continuation.superseded:
                self._write_failure_note(continuation)
            if self._may_finish(continuation):
                self._finish_approval(continuation, owner_available=self._bot())

    def _write_failure_note(self, continuation: _Continuation) -> None:
        """Write a failed approval's note, after the span that waits in place for it gave up."""
        reply = self.model.reply
        if reply is None or continuation.delivery_id in self.model.finals:
            return
        current = self._current()
        runs = {continuation.paused_span_id, continuation.waiting_span_id, continuation.claim_span_id}
        if current is not None and current.span_id in runs:
            if current.bot_generation != self.generation:
                self._apply(rl.span_left_behind(reply, current, active_generation=self.generation, now_ns=self._now()))
            elif current.span_id in self.model.cancel_requested and reply.unapplied_stop:
                # The span applies the Stop first.
                return
            else:
                # The wait learns the failure and ends its turn with the note as its answer.
                self._apply(rl.release(reply, current, now_ns=self._now()))
            reply = self.model.reply
            assert reply is not None
        last = self._last()
        assert last is not None
        cancelled = continuation.disposition == "cancelled_by_user" or reply.unapplied_stop
        requested = ReplyState.CANCELLED if cancelled else ReplyState.FAILED
        self._apply(
            rl.approval_failure_note(
                reply,
                last,
                approval_id=continuation.approval_id,
                shown="approval failed",
                state=rl._expected_terminal_state(reply, requested),
                prepared_revision=reply.revision,
                span_has_final=last.delivery_id in self.model.finals,
                now_ns=self._now(),
            ),
        )

    def _may_finish(self, continuation: _Continuation) -> bool:
        """The store's gate: a superseded run, or a FINAL at its first source Matrix took or refused for good."""
        return continuation.superseded or self.model.finals.get(continuation.delivery_id) in {
            "acknowledged",
            "refused",
        }

    def _finish_approval(self, continuation: _Continuation, *, owner_available: bool, retry: bool = True) -> None:
        """Apply the finish to the reply while the continuation still holds it, then delete the continuation."""
        reply = self.model.reply
        assert reply is not None
        # No owner left to answer leaves the turn unanswered.
        answers_turn = owner_available
        transition = self._apply(
            rl.approval_settled(
                reply,
                self._last(),
                approval_id=continuation.approval_id,
                paused_span_id=continuation.paused_span_id,
                result="failed" if continuation.state == "failing" else "finished",
                disposition=continuation.disposition,
                answers_turn=answers_turn,
                now_ns=self._now(),
            ),
        )
        if not answers_turn:
            assert all(not e.answered for e in transition.effects if isinstance(e, SettleSources)), transition
            if not self._is_settled(continuation.paused_span_id):
                self.model.unanswered.add(continuation.paused_span_id)
        del self.model.continuations[continuation.approval_id]
        self._derive_hold()
        if retry:
            self._retry_deferred()

    def _release_approval(self, continuation: _Continuation) -> None:
        """Hand an interrupted run's sources back to replay, ending the span that ran for it."""
        reply = self.model.reply
        assert reply is not None
        span = None if continuation.claim_span_id is None else self.model.spans.get(continuation.claim_span_id)
        current = span if span is not None and span.span_id == reply.current_span_id else None
        self._apply(rl.approval_released(reply, current, now_ns=self._now()))
        del self.model.continuations[continuation.approval_id]
        self._derive_hold()
        self._retry_deferred()

    @precondition(
        lambda self: (
            self._bot()
            and any(
                continuation.state == "ready"
                and continuation.claim_span_id is not None
                and (
                    continuation.claim_span_id not in self.model.spans
                    or self.model.spans[continuation.claim_span_id].ended
                    or self.model.spans[continuation.claim_span_id].bot_generation != self.generation
                )
                for continuation in self.model.continuations.values()
            )
        ),
    )
    @rule()
    def recover_claimed_approvals(self) -> None:
        """Approval recovery after a restart: frozen answers finish, unanswered resumes fail, the rest replay."""
        self._recover_claimed()

    def _recover_claimed(self) -> None:
        for continuation in tuple(self.model.continuations.values()):
            if continuation.state != "ready" or continuation.claim_span_id is None:
                continue
            span = self.model.spans.get(continuation.claim_span_id)
            if span is not None and not span.ended and span.bot_generation == self.generation:
                continue
            final = self.model.finals.get(continuation.delivery_id)
            if final is not None:
                if final == "refused":
                    # Recovery settles a refused answer as a failure.
                    continuation.state = "failing"
                    continuation.disposition = "failed"
                if final in {"acknowledged", "refused"}:
                    self._finish_approval(continuation, owner_available=True)
            elif span is not None and span.outcome in _UNANSWERED_OUTCOMES - {SpanOutcome.RELEASED}:
                continuation.state = "failing"
                continuation.disposition = "failed"
            else:
                self._release_approval(continuation)

    # --- reply-authored -----------------------------------------------------

    @precondition(lambda self: self._bot() and self.model.reply is not None)
    @rule(lag=st.integers(0, 2))
    def stop(self, lag: int) -> None:
        """A Stop, while a span runs or while none does."""
        reply = self.model.reply
        assert reply is not None
        self.model.receipt_order += 1
        receipt = max(1, self.model.receipt_order - lag)
        live = self._live()
        transition = rl.stop(
            reply,
            self._current() or self._last(),
            StopFacts(
                receipt_order=receipt,
                # As the store decides it: the edit the reply's regeneration answers outranks an older Stop.
                newer_edit=(reply.edit_receipt_order or 0) > receipt,
                span_live=live is not None,
            ),
            now_ns=self._now(),
        )
        self._apply(transition)

    @precondition(lambda self: self._bot() and self.model.reply is not None)
    @rule()
    def restart(self) -> None:
        """Restart."""
        self.model.generation += 1
        self.model.cancel_requested.clear()
        reply = self.model.reply
        last = self._current() or self._last()
        assert reply is not None
        assert last is not None
        self._apply(
            rl.owner_lost(
                reply,
                last,
                rl.OwnerLostFacts(
                    active_generation=self.generation,
                    sources_pending=not self._is_settled(last.span_id),
                ),
                now_ns=self._now(),
            ),
        )

    @precondition(lambda self: self._bot() and self.model.reply is not None and not self.model.reply.terminal)
    @rule()
    def delete_sources(self) -> None:
        """The user deletes the message every span of the reply answers."""
        span = self._current() or self._last()
        self.model.deleted.add("$source")
        transition = self._apply(rl.sources_deleted(self.model.reply, span, now_ns=self._now()))  # type: ignore[arg-type]
        assert all(not e.answered for e in transition.effects if isinstance(e, SettleSources)), transition

    @precondition(lambda self: self._bot() and self.model.reply is not None)
    @rule()
    def depart(self) -> None:
        """The bot leaves the room: its replies end, and the room's continuations go with them."""
        self._apply(rl.departed(self.model.reply, self._current(), now_ns=self._now()))  # type: ignore[arg-type]
        self.model.left = True
        self.model.rows = [row for row in self.model.rows if row.intent.stage is not WriteStage.EDIT]
        for continuation in tuple(self.model.continuations.values()):
            del self.model.continuations[continuation.approval_id]
        # The departure settles every pending turn event of the room, unanswered.
        for span_id in self.model.spans:
            if not self._is_settled(span_id):
                self.model.unanswered.add(span_id)
            self._settle(span_id)
        self._derive_hold()
        self.model.deferred.clear()

    @precondition(
        lambda self: (
            self._bot()
            and self.model.reply is not None
            and self.model.reply.state is ReplyState.ACTIVE
            and self.model.reply.current_span_id is None
            and not self.model.reply.unapplied_stop
            and self.model.spans[self.model.reply.last_span_id].outcome in rl._SOURCES_PENDING_OUTCOMES
            and not self._is_settled(self.model.reply.last_span_id)
            # The dispatcher hands a continuation's sources to the approval runtime instead.
            and self._held_by() is None
        ),
    )
    @rule()
    def settle_without_reply(self) -> None:
        """Settle without reply."""
        reply = self.model.reply
        assert reply is not None
        last = self.model.spans[reply.last_span_id]
        self._settle(last.span_id)
        transition = self._apply(rl.sources_settled_without_reply(reply, last, now_ns=self._now()))
        assert all(not e.answered for e in transition.effects if isinstance(e, SettleSources)), transition

    @precondition(
        lambda self: (
            self._bot()
            and self.model.reply is not None
            and not self._is_settled(self.model.reply.last_span_id)
            and self.model.spans[self.model.reply.last_span_id].outcome is not None
        ),
    )
    @rule()
    def supersede_replay(self) -> None:
        """A newer message supersedes the replay of the reply's sources."""
        reply = self.model.reply
        assert reply is not None
        last = self.model.spans[reply.last_span_id]
        transition = self._apply(
            rl.replay_superseded(reply, last, durable_write_debt=self._unresolved_rows(), now_ns=self._now()),
        )
        assert all(not e.answered for e in transition.effects if isinstance(e, SettleSources)), transition
        if transition.outcome is rl.Outcome.DUPLICATE and self._held_by() is None:
            # A reply that already ended leaves the sources to the caller, which settles them as ignored.
            self._settle(last.span_id)

    @precondition(
        lambda self: (
            self._bot()
            and self.model.reply is not None
            and not self.model.continuations
            and self.model.reply.current_span_id is None
            and not self._is_settled(self.model.reply.last_span_id)
            and self.model.spans[self.model.reply.last_span_id].outcome in rl._SOURCES_PENDING_OUTCOMES
        ),
    )
    @rule()
    def drop_replay(self) -> None:
        """Ingress settles the sources a reply waits to replay without a turn."""
        reply = self.model.reply
        assert reply is not None
        last = self.model.spans[reply.last_span_id]
        self._settle(last.span_id)
        transition = self._apply(rl.replay_dropped(reply, last, sources_pending=False, now_ns=self._now()))
        assert all(not e.answered for e in transition.effects if isinstance(e, SettleSources)), transition

    @precondition(
        lambda self: (
            self._bot()
            and self.model.reply is not None
            and self.model.reply.state is ReplyState.ACTIVE
            and self.model.reply.approval_id is None
            # The source gate turns a deleted message away before any dispatch.
            and "$source" not in self.model.deleted
        ),
    )
    @rule()
    def dispatch_failure(self) -> None:
        """A dispatch failed, before or after its claim."""
        self._apply(rl.dispatch_failed(self.model.reply, self._live(), error_text="boom", now_ns=self._now()))  # type: ignore[arg-type]

    @precondition(lambda self: self._bot() and self.model.reply is not None)
    @rule()
    def remove_entity(self) -> None:
        """The entity leaves the configuration: its reply ends, and its approvals are discarded once noticed."""
        reply = self.model.reply
        assert reply is not None
        # No bot remains to write anything: the removal is no abandonment the reply's Matrix event must match.
        self.model.removed = True
        transition = self._apply(rl.removed_entity(reply, self._current() or self._last(), now_ns=self._now()))  # type: ignore[arg-type]
        # S2: nothing answered the turn of a removed entity's reply.
        assert all(not e.answered for e in transition.effects if isinstance(e, SettleSources)), transition
        self.model.deferred.clear()
        # Nothing delivers its rows any more.
        self.model.rows.clear()

    @precondition(lambda self: self.model.removed)
    @rule()
    def time_passes(self) -> None:
        """Nothing runs for a removed entity."""

    @precondition(lambda self: self.model.removed and bool(self.model.continuations))
    @rule()
    def discard_unavailable_approvals(self) -> None:
        """The router posts the unavailable notice, then each approval of the removed entity ends without an answer."""
        for continuation in tuple(self.model.continuations.values()):
            if continuation.state != "failing":
                continuation.state = "failing"
                continuation.disposition = "failed"
            self._finish_approval(continuation, owner_available=False)

    @precondition(
        lambda self: (
            self.model.reply is not None
            and self.model.reply.terminal
            and not self.model.rows
            and self.model.reply.owed_write is None
            and not self.model.reply.redaction_pending
            and not self.model.reply.unapplied_stop
        ),
    )
    @rule()
    def retention(self) -> None:
        """Retention forgets a finished reply that owes nothing, unless a continuation still names its spans."""
        if self._held_by_any():
            return
        self.model.reply = None
        self.model.spans = {}
        self.model.settled = set()
        self.model.finals = {}
        self.model.deferred.clear()
        # The next turn is a new message, which the ledger has not answered.
        self.model.deleted.clear()
        self.model.turn_answered = False

    # --- invariants ---------------------------------------------------------

    @invariant()
    def a_turn_waiting_for_its_replay_is_unanswered(self) -> None:
        """I16: a reply that waits to replay its original source has not answered that turn, or the replay never runs."""
        reply = self.model.reply
        if reply is None or reply.state is not ReplyState.ACTIVE or reply.current_span_id is not None:
            return
        last = self._last()
        assert last is not None
        if (
            last.outcome in rl._SOURCES_PENDING_OUTCOMES
            and "$source" in last.sources.pending
            and not self._is_settled(last.span_id)
        ):
            assert not self.model.turn_answered, (reply, last)

    @invariant()
    def one_current_span(self) -> None:
        """I1: at most one current span, and every other span has ended."""
        reply = self.model.reply
        if reply is None:
            return
        for span in self.model.spans.values():
            if span.span_id == reply.current_span_id:
                assert span.outcome is None
            else:
                assert span.ended, span

    @invariant()
    def non_terminal_replies_have_an_owner(self) -> None:
        """I5: a non-terminal reply has a span, pending sources, an approval, or an owed write."""
        reply = self.model.reply
        if reply is None or reply.terminal:
            return
        last = self.model.spans[reply.last_span_id]
        owned = (
            reply.current_span_id is not None
            or reply.approval_id is not None
            or any(continuation.claim_span_id == last.span_id for continuation in self.model.continuations.values())
            or (last.outcome in rl._SOURCES_PENDING_OUTCOMES and not self._is_settled(last.span_id))
            or reply.owed_write is not None
        )
        assert owned, (reply, last, self.model.continuations)

    @invariant()
    def terminal_spans_settled_their_sources(self) -> None:
        """I6: a span's sources settle with its terminal transition, except those an approval holds."""
        held = {continuation.paused_span_id for continuation in self.model.continuations.values()}
        for span in self.model.spans.values():
            if span.kind is SpanKind.APPROVAL_RESUME or span.outcome not in _SETTLING_OUTCOMES or span.span_id in held:
                continue
            assert self._is_settled(span.span_id), span

    @invariant()
    def writes_are_recorded_before_they_are_sent(self) -> None:
        """I7: every write took a sequence, and nothing past the sequence is shown or confirmed."""
        reply = self.model.reply
        if reply is None:
            return
        assert (reply.possibly_shown_seq or 0) <= reply.reply_sequence
        assert (reply.confirmed_seq or 0) <= reply.reply_sequence
        for row in self.model.rows:
            assert row.intent.sequence <= reply.reply_sequence

    @invariant()
    def stop_buttons_go_when_replies_stop_being_active(self) -> None:
        """I8: only a reply whose span runs keeps a Stop button: active, or paused while its span waits in place."""
        reply = self.model.reply
        waiting_in_place = reply is not None and reply.state is ReplyState.PAUSED and reply.current_span_id is not None
        if reply is not None and reply.state is not ReplyState.ACTIVE and not waiting_in_place:
            assert reply.stop_button_event_id is None

    @invariant()
    def a_finished_reply_shows_its_end(self) -> None:
        """I15: once a finished reply owes nothing, its latest write that may show ends it and Matrix took it.

        A note Matrix refused cannot be resent, so the reply stops owing it; a
        room the bot left owes nothing.
        """
        reply = self.model.reply
        if (
            reply is None
            or not reply.terminal
            or reply.state is ReplyState.GONE
            or self.model.removed
            or self.model.left
        ):
            return
        if (
            self.model.rows
            or reply.owed_write is not None
            or reply.redaction_pending
            or reply.possibly_shown_seq is None
        ):
            return
        shown = self.model.writes.get((reply.reply_id, reply.possibly_shown_seq), "ends")
        assert shown in {"ends", "refused_edit"}, (shown, reply)

    @invariant()
    def a_finished_reply_shows_its_stop(self) -> None:
        """I4: a terminal reply that is still visible has no Stop left to apply."""
        reply = self.model.reply
        if reply is not None and reply.terminal and reply.state is not ReplyState.GONE:
            assert not reply.unapplied_stop, reply

    @invariant()
    def continuations_name_spans_of_the_reply(self) -> None:
        """I10: a continuation's paused span is still recorded; retention never forgot what it reads."""
        for continuation in self.model.continuations.values():
            assert continuation.paused_span_id in self.model.spans, continuation

    @invariant()
    def one_approval_holds_a_reply(self) -> None:
        """I13: at most one continuation that is not superseded names the reply's spans."""
        holders = [
            continuation
            for continuation in self.model.continuations.values()
            if not continuation.superseded and continuation.paused_span_id in self.model.spans
        ]
        assert len(holders) <= 1, holders

    @invariant()
    def a_paused_reply_is_held(self) -> None:
        """A paused reply waits on the approval that holds it."""
        reply = self.model.reply
        if reply is not None and reply.state is ReplyState.PAUSED:
            assert reply.approval_id is not None, (reply, self.model.continuations)

    # --- drain --------------------------------------------------------------

    def teardown(self) -> None:
        """I9: every owner can move what it holds: draining them all leaves no approval, debt, or waiting claim."""
        for _ in range(_DRAIN_ROUNDS):
            if not self._drain_step():
                break
        assert not self.model.continuations, self.model.continuations
        assert not self.model.deferred, self.model.deferred
        reply = self.model.reply
        if reply is not None and self.model.removed:
            # No bot remains for a removed entity: its reply ended owing nothing, and no source waits on it.
            assert self._is_settled(reply.last_span_id), (reply, self._last())
            assert reply.terminal, reply
            assert reply.owed_write is None, reply
            assert not reply.redaction_pending, reply
        if reply is None or self.model.removed:
            return
        assert not self.model.rows, self.model.rows
        assert reply.owed_write is None, reply
        assert not reply.redaction_pending, reply
        assert reply.terminal, (reply, self._last())
        resumed = {
            span.approval_id
            for span in self.model.spans.values()
            if span.kind is SpanKind.APPROVAL_RESUME and span.outcome is SpanOutcome.COMPLETED
        }
        for span in self.model.spans.values():
            if span.outcome in rl._SOURCES_PENDING_OUTCOMES:
                continue
            assert self._is_settled(span.span_id), span
            answered = span.outcome is SpanOutcome.COMPLETED or self.model.paused_by.get(span.span_id) in resumed
            if (
                answered
                and span.prepared_edit is not None
                and "$source" not in self.model.deleted
                and span.span_id not in self.model.unanswered
            ):
                # S5: a span carrying an edit, a regeneration or a replay of one, whose answer it or its approval's
                # resume completed consumed the edit it selected, whatever Matrix then did with that answer.
                assert span.prepared_edit in self.model.consumed, span

    def _drain_step(self) -> bool:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Move one owner forward; return whether anything was left to move."""
        model = self.model
        if model.removed:
            if model.continuations:
                self.discard_unavailable_approvals()
                return True
            return False
        reply = model.reply
        if reply is None:
            return False
        if model.rows:
            self._acknowledge_row()
            return True
        if reply.redaction_pending:
            self.redact()
            return True
        if reply.owed_write is not None:
            self._flush_owed()
            return True
        waiting = [continuation for continuation in model.continuations.values() if continuation.state == "waiting"]
        for continuation in waiting:
            # Every card is decided or expires, which resumes its run.
            continuation.state = "ready"
        if waiting:
            return True
        live = self._live()
        if live is not None:
            if reply.unapplied_stop and live.span_id in model.cancel_requested:
                self.span_observes_its_stop()
            elif reply.state is ReplyState.PAUSED:
                # The wait gets its decision through the continuation, settled below.
                continuation = self._runs_for(live) or model.continuations.get(self._held_by() or "")
                if continuation is None:
                    msg = f"in-place wait {live.span_id} has no approval"
                    raise AssertionError(msg)
                if continuation.state == "ready" and continuation.claim_span_id is None:
                    self._claim_ready()
                else:
                    self._settle_approvals()
            else:
                self.finish()
            return True
        if any(c.state == "ready" and c.claim_span_id is None for c in model.continuations.values()):
            before = dict(model.continuations)
            self._claim_ready()
            if model.continuations != before or self._live() is not None:
                return True
        if any(c.state == "ready" and c.claim_span_id is not None for c in model.continuations.values()):
            self._recover_claimed()
            return True
        if model.continuations:
            self._settle_approvals()
            return True
        if model.deferred:
            self._retry_deferred()
            return True
        if not reply.terminal:
            current = self._current()
            if current is not None:
                # A span an older instance left: the restart that follows ends it.
                self.restart()
                return True
            if self.model.spans[reply.last_span_id].outcome in rl._SOURCES_PENDING_OUTCOMES:
                self.replay()
                return True
            self.restart()
            return True
        for span in self.model.spans.values():
            if span.ended and span.kind is not SpanKind.APPROVAL_RESUME and not self._is_settled(span.span_id):
                # Journal replay dispatches the sources a later span overtook; the ended reply answers nothing.
                transition = self._claim(replay_of=span)
                assert transition.outcome is Outcome.DUPLICATE, transition
                self._settle(span.span_id)
                return True
        return False


def test_a_refused_answer_after_an_approved_regeneration_ends_with_the_delivery_failed_note() -> None:
    """I15 on the sequence that left a partial reply looking unfinished: Matrix refuses its answer for good."""
    machine = ReplyLifecycleMachine()
    machine.start_turn()
    machine.dispatch_failure()
    machine.flush_owed()
    machine.acknowledge_row()
    machine.regenerate()
    machine.pause(in_place=False)
    machine.pause_shown_and_approved()
    machine.finish()
    machine.fail_row(reason="too_large")
    machine.a_finished_reply_shows_its_end()
    machine.teardown()
    reply = machine.model.reply
    assert reply is not None
    assert reply.state is ReplyState.FAILED
    # The resume completed its answer, so the regeneration's edit is committed though Matrix refused it.
    assert machine.model.consumed == {"$edit-1"}


def test_a_refused_answer_of_a_regeneration_approved_in_place_still_commits_its_edit() -> None:
    """S5 on an in-place approval: the refused answer fails the approval but commits the edit, as a queued answer does."""
    machine = ReplyLifecycleMachine()
    machine.start_turn()
    machine.dispatch_failure()
    machine.flush_owed()
    machine.acknowledge_row()
    machine.regenerate()
    machine.pause(in_place=True)
    machine.acknowledge_row()
    machine.pause_shown_and_approved()
    machine.finish()
    machine.fail_row(reason="too_large")
    machine.a_finished_reply_shows_its_end()
    machine.teardown()
    reply = machine.model.reply
    assert reply is not None
    assert reply.state is ReplyState.FAILED
    assert machine.model.consumed == {"$edit-1"}


def test_an_abandoned_regeneration_of_unfinished_work_leaves_its_turn_for_the_retry() -> None:
    """I16 on a regeneration that failed before writing over a partial reply a retry waits to finish."""
    machine = ReplyLifecycleMachine()
    machine.start_turn()
    machine.enqueue_initial(placeholder=False)
    machine.acknowledge_row()
    machine.release(superseded=False)
    machine.regenerate()
    machine.dispatch_failure()
    machine.a_turn_waiting_for_its_replay_is_unanswered()
    reply = machine.model.reply
    assert reply is not None
    assert reply.state is ReplyState.ACTIVE
    assert not machine.model.turn_answered
    machine.teardown()


@pytest.mark.timeout(300)
def test_reply_lifecycle_invariants() -> None:
    """Random interleavings never break the lifecycle invariants, and every run drains."""
    ReplyLifecycleMachine.TestCase.settings = settings(
        max_examples=400,
        stateful_step_count=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    ReplyLifecycleMachine.TestCase().runTest()
