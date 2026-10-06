"""Interleaved reply events keep the lifecycle invariants (DESIGN.md §6.5 I1-I8).

A state machine drives one reply through every rule in random order -- claims,
progress, durable rows and their acknowledgements or failures, Stops at any
point, pauses and approval outcomes, regenerations, deletions, departures, and
bot restarts -- and checks after every step that the records still describe a
reply someone owns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    RowIntent,
    SettleSources,
    Span,
    SpanKind,
    SpanOutcome,
    SpanSources,
    StopFacts,
    TerminalWrite,
    WriteFacts,
    WriteStage,
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


@dataclass
class _Approval:
    approval_id: str
    state: str  # waiting, claimed, failing
    disposition: rl.FailureDisposition | None = None


@dataclass
class _Row:
    intent: RowIntent
    creates_event: bool


@dataclass
class _Model:
    generation: int = 1
    counter: int = 0
    receipt_order: int = 0
    last_edit_order: int = 0
    reply: Reply | None = None
    spans: dict[str, Span] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)
    # Spans that ended while an approval held their reply: its finish settles their sources.
    held: set[str] = field(default_factory=set)
    cancel_requested: set[str] = field(default_factory=set)
    rows: list[_Row] = field(default_factory=list)
    finals: set[str] = field(default_factory=set)
    approval: _Approval | None = None


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
        if current is None or current.ended or current.bot_generation != self.generation:
            return None
        return current

    def _apply(self, transition: rl.Transition) -> rl.Transition:
        if transition.outcome is Outcome.RECOMPUTE:
            msg = "rendered from the current revision, yet asked to recompute"
            raise AssertionError(msg)
        before = self.model.reply
        if transition.reply is not None:
            self.model.reply = transition.reply
        for span in transition.spans:
            if (
                span.ended
                and before is not None
                and before.approval_id is not None
                and span.kind is not SpanKind.APPROVAL_RESUME
            ):
                # It ended while its reply's approval held its sources.
                self.model.held.add(span.span_id)
            self.model.spans[span.span_id] = span
        for effect in transition.effects:
            self._apply_effect(effect)
        if transition.row is not None:
            reply = self.model.reply
            assert reply is not None
            creates = reply.event_id is None and transition.row.stage is not WriteStage.EDIT
            self.model.rows.append(_Row(transition.row, creates_event=creates))
            if transition.row.stage is WriteStage.FINAL:
                self.model.finals.add(transition.row.span_id)
        return transition

    def _apply_effect(self, effect: rl.Effect) -> None:
        match effect:
            case SettleSources(span_id=span_id):
                self.model.settled.add(span_id)
            case CancelSpan(span_id=span_id):
                self.model.cancel_requested.add(span_id)
            case FenceApproval(disposition=disposition):
                assert self.model.approval is not None
                self.model.approval.state = "failing"
                self.model.approval.disposition = disposition
            case _:
                # Waking the approval and transferring a Stop need no model state.
                pass

    def _sources(self) -> SpanSources:
        return SpanSources(pending=("$source",), logical=("$source",))

    def _claim(self, *, edit: str | None = None, approval_id: str | None = None) -> rl.Transition:
        reply = self.model.reply
        request = ClaimRequest(
            span_id=self._next("span"),
            delivery_id=edit or ("$approval" if approval_id else "$source"),
            sources=self._sources(),
            bot_generation=self.generation,
            now_ns=self._now(),
            new_reply_id=self._next("reply"),
            entity_name="agent",
            room_id="!room",
            thread_id=None,
            membership_epoch=1,
            requester_id="@user",
            visibility_policy=rl.VisibilityPolicy.NORMAL,
            empty_presentation="empty",
            driving_edit_id=edit,
            approval_id=approval_id,
        )
        context = ClaimContext(
            reply=reply,
            last_span=self._last(),
            current_span=self._current(),
            interactive_span=None,
            durable_write_debt=bool(self.model.rows) and reply is not None,
            active_generation=self.generation,
            edit_receipt_order=self.model.last_edit_order if edit else None,
        )
        transition = rl.claim(request, context)
        if transition.reply is not None and reply is not None and transition.reply.reply_id != reply.reply_id:
            # A regeneration of a gone reply starts a new record.
            self.model.spans = {}
            self.model.settled = set()
            self.model.held = set()
            self.model.rows = []
            self.model.finals = set()
        return self._apply(transition)

    def _terminal_write(self, requested: ReplyState) -> TerminalWrite:
        reply = self.model.reply
        assert reply is not None
        return TerminalWrite(
            shown=f"shown-{reply.revision}",
            prepared_revision=reply.revision,
            state=rl.expected_terminal_state(reply, requested),
        )

    # --- claims -------------------------------------------------------------

    @precondition(lambda self: self.model.reply is None)
    @rule()
    def start_turn(self) -> None:
        """Start turn."""
        self._claim()

    @precondition(
        lambda self: (
            self.model.reply is not None
            and self.model.reply.state is ReplyState.ACTIVE
            and self.model.reply.current_span_id is None
            and self.model.spans[self.model.reply.last_span_id].outcome in rl.SOURCES_PENDING_OUTCOMES
            and self.model.reply.last_span_id not in self.model.settled
        ),
    )
    @rule()
    def replay(self) -> None:
        """Replay."""
        self._claim()

    @precondition(
        lambda self: (
            self.model.reply is not None
            and self.model.reply.current_span_id is None
            and not (self.model.approval is not None and self.model.approval.state != "waiting")
        ),
    )
    @rule()
    def regenerate(self) -> None:
        """Regenerate."""
        self.model.receipt_order += 1
        self.model.last_edit_order = self.model.receipt_order
        transition = self._claim(edit=self._next("$edit"))
        if (
            transition.claimed is not None
            and self.model.approval is not None
            and self.model.approval.state == "failing"
        ):
            # Decision 1: the superseded approval's cleanup finishes it without touching the reply.
            settled = rl.approval_settled(
                self.model.reply,  # type: ignore[arg-type]
                None,
                approval_id=self.model.approval.approval_id,
                result="failed",
                disposition="superseded",
                now_ns=self._now(),
            )
            assert settled.outcome in {Outcome.DUPLICATE, Outcome.STALE}
            self._approval_finished()

    # --- progress and durable rows -----------------------------------------

    @precondition(lambda self: self._live() is not None)
    @rule(previous_ok=st.booleans())
    def write_ahead(self, previous_ok: bool) -> None:
        """Write ahead."""
        if self.model.rows:
            return
        span = self._live()
        assert span is not None
        self._apply(
            rl.write_ahead(
                self.model.reply,  # type: ignore[arg-type]
                span,
                shown="progress",
                previous=rl.ProgressConfirmation(event_id="$reply", placeholder_only=False) if previous_ok else None,
                active_generation=self.generation,
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

    @precondition(lambda self: bool(self.model.rows))
    @rule(placeholder=st.booleans())
    def acknowledge_row(self, placeholder: bool) -> None:
        """Acknowledge row."""
        row = self.model.rows.pop(0)
        reply = self.model.reply
        assert reply is not None
        creates = row.creates_event and reply.event_id is None
        self._apply(
            rl.write_acknowledged(
                reply,
                WriteFacts(row.intent.stage, row.intent.sequence, row.intent.span_id, creates, placeholder),
                event_id="$reply",
                now_ns=self._now(),
            ),
        )

    @precondition(lambda self: bool(self.model.rows))
    @rule(reason=st.sampled_from(["delivery_failed", "too_large"]))
    def fail_row(self, reason: str) -> None:
        """Fail row."""
        row = self.model.rows.pop(0)
        reply = self.model.reply
        assert reply is not None
        span = self.model.spans[row.intent.span_id]
        if row.intent.stage is WriteStage.FINAL and not span.ended:
            return
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
        if row.intent.stage is WriteStage.INITIAL:
            # A refused create also fails the reply's edit rows waiting on it.
            self.model.rows = [waiting for waiting in self.model.rows if waiting.intent.stage is not WriteStage.EDIT]
        reply = self.model.reply
        approval = self.model.approval
        if reply is not None and reply.state is ReplyState.FAILED and approval and approval.state == "failing":
            self._approval_finished()

    @precondition(lambda self: self.model.reply is not None and self.model.reply.owed_write is not None)
    @rule()
    def flush_owed(self) -> None:
        """Flush owed."""
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
                span_has_final=span.span_id in self.model.finals,
                now_ns=self._now(),
            ),
        )

    @precondition(lambda self: bool(self.model.reply and self.model.reply.redaction_pending))
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
        self._apply(rl.record_stop_button(self.model.reply, event_id=self._next("$button"), now_ns=self._now()))  # type: ignore[arg-type]

    # --- span exits ---------------------------------------------------------

    @precondition(lambda self: self._live() is not None)
    @rule()
    def finish(self) -> None:
        """Finish."""
        span = self._live()
        assert span is not None
        if self.model.reply is not None and self.model.reply.state is ReplyState.PAUSED:
            return
        self._apply(rl.finish(self.model.reply, span, self._terminal_write(ReplyState.COMPLETED), now_ns=self._now()))  # type: ignore[arg-type]

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
        transition = rl.finish(self.model.reply, span, prepared, now_ns=self._now())  # type: ignore[arg-type]
        changed = self.model.reply is not None and self.model.reply.revision != prepared.prepared_revision
        if transition.outcome is Outcome.RECOMPUTE:
            assert changed
            return
        self._apply(transition)

    @precondition(lambda self: self._live() is not None)
    @rule(phase=st.sampled_from(["pre_delivery", "delivery"]), note=st.booleans())
    def fail(self, phase: rl.FailurePhase, note: bool) -> None:
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
        transition = self._apply(rl.fail(reply, span, write, phase=phase, now_ns=self._now()))
        self._approval_resume_ended(span, transition)

    @precondition(lambda self: self._live() is not None)
    @rule(reason=st.sampled_from(["suppressed", "hook_failed"]))
    def suppress(self, reason: rl.SuppressReason) -> None:
        """Suppress."""
        span = self._live()
        assert span is not None
        if self.model.reply is not None and self.model.reply.state is ReplyState.PAUSED:
            return
        transition = self._apply(rl.suppress(self.model.reply, span, reason=reason, now_ns=self._now()))  # type: ignore[arg-type]
        self._approval_resume_ended(span, transition)

    @precondition(lambda self: self._live() is not None)
    @rule(superseded=st.booleans())
    def release(self, superseded: bool) -> None:
        """Release."""
        span = self._live()
        assert span is not None
        if self.model.reply is not None and self.model.reply.state is ReplyState.PAUSED:
            return
        if span.kind is SpanKind.APPROVAL_RESUME:
            self._apply(rl.approval_released(self.model.reply, span, now_ns=self._now()))  # type: ignore[arg-type]
            self.model.approval = None
            return
        outcome = SpanOutcome.SUPERSEDED if superseded else SpanOutcome.RELEASED
        self._apply(rl.release(self.model.reply, span, outcome=outcome, now_ns=self._now()))  # type: ignore[arg-type]

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
        transition = self._apply(rl.stopped(reply, span, write, now_ns=self._now()))
        self._approval_resume_ended(span, transition)

    def _approval_resume_ended(self, span: Span, transition: rl.Transition) -> None:
        """An approval resume that ended without a terminal row fails its continuation."""
        if span.kind is not SpanKind.APPROVAL_RESUME or self.model.approval is None:
            return
        ended = self.model.spans[span.span_id]
        if (
            ended.outcome in {SpanOutcome.CANCELLED, SpanOutcome.FAILED, SpanOutcome.SUPPRESSED}
            and transition.row is None
        ):
            self.model.approval.state = "failing"
            self.model.approval.disposition = (
                "cancelled_by_user" if ended.outcome is SpanOutcome.CANCELLED else "failed"
            )
        elif transition.row is not None:
            finished = rl.approval_settled(
                self.model.reply,  # type: ignore[arg-type]
                ended,
                approval_id=self.model.approval.approval_id,
                result="finished",
                disposition=None,
                now_ns=self._now(),
            )
            self._apply(finished)
            self._approval_finished()

    # --- approvals ----------------------------------------------------------

    @precondition(lambda self: self._live() is not None and self.model.approval is None)
    @rule(in_place=st.booleans())
    def pause(self, in_place: bool) -> None:
        """Pause."""
        reply = self.model.reply
        span = self._live()
        assert reply is not None
        assert span is not None
        if span.kind is SpanKind.APPROVAL_RESUME:
            return
        approval_id = self._next("approval")
        transition = rl.pause(
            reply,
            span,
            rl.PauseWrite(
                shown="paused",
                prepared_revision=reply.revision,
                stage=WriteStage.EDIT if reply.event_id is not None else WriteStage.INITIAL,
            ),
            approval_id=approval_id,
            in_place=in_place,
            now_ns=self._now(),
        )
        if transition.outcome is Outcome.STOPPED:
            assert reply.unapplied_stop
            return
        self._apply(transition)
        self.model.approval = _Approval(approval_id, "waiting")

    @precondition(
        lambda self: (
            self.model.approval is not None
            and self.model.approval.state == "waiting"
            and self.model.reply is not None
            and self.model.reply.state is ReplyState.PAUSED
        ),
    )
    @rule()
    def approve(self) -> None:
        """Approve."""
        approval = self.model.approval
        assert approval is not None
        current = self._live()
        if current is not None:
            self._apply(
                rl.resumed_in_place(self.model.reply, current, approval_id=approval.approval_id, now_ns=self._now()),
            )  # type: ignore[arg-type]
            self.model.approval = None
            return
        transition = self._claim(approval_id=approval.approval_id)
        if transition.claimed is not None:
            approval.state = "claimed"

    @precondition(lambda self: self.model.approval is not None and self.model.approval.state == "failing")
    @rule()
    def settle_approval_failure(self) -> None:
        """Settle approval failure."""
        approval = self.model.approval
        reply = self.model.reply
        assert approval is not None
        assert reply is not None
        current = self._live()
        if current is not None and current.span_id in self.model.cancel_requested and reply.unapplied_stop:
            # The in-place wait's span applies the Stop first.
            return
        self._apply(
            rl.approval_settled(
                reply,
                self._last(),
                approval_id=approval.approval_id,
                result="failed",
                disposition=approval.disposition,
                now_ns=self._now(),
            ),
        )
        if current is not None and self.model.reply is not None and self.model.reply.terminal:
            self._apply(rl.release(self.model.reply, current, now_ns=self._now()))
        self._approval_finished()

    def _approval_finished(self) -> None:
        """The continuation finished, settling the sources it held."""
        self.model.approval = None
        self.model.settled |= self.model.held
        self.model.held = set()

    # --- reply-authored -----------------------------------------------------

    @precondition(lambda self: self.model.reply is not None)
    @rule(lag=st.integers(0, 2))
    def stop(self, lag: int) -> None:
        """Stop."""
        reply = self.model.reply
        assert reply is not None
        self.model.receipt_order += 1
        receipt = max(1, self.model.receipt_order - lag)
        live = self._live()
        transition = rl.stop(
            reply,
            self._current(),
            StopFacts(
                receipt_order=receipt,
                newer_edit=self.model.last_edit_order > receipt,
                span_live=live is not None,
            ),
            now_ns=self._now(),
        )
        self._apply(transition)

    @precondition(lambda self: self.model.reply is not None)
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
                    sources_pending=last.span_id not in self.model.settled,
                ),
                now_ns=self._now(),
            ),
        )

    @precondition(
        lambda self: (
            self._current() is not None
            and self._current().kind is SpanKind.APPROVAL_RESUME
            and self._current().bot_generation != self.generation
        ),
    )
    @rule()
    def recover_interrupted_resume(self) -> None:
        """Main's approval recovery releases a resume an older instance left to replay."""
        current = self._current()
        assert current is not None
        transition = self._apply(rl.approval_released(self.model.reply, current, now_ns=self._now()))  # type: ignore[arg-type]
        if transition.applied:
            self.model.approval = None

    @precondition(lambda self: self.model.reply is not None and not self.model.reply.terminal)
    @rule()
    def delete_sources(self) -> None:
        """Delete sources."""
        self._apply(rl.sources_deleted(self.model.reply, self._current(), now_ns=self._now()))  # type: ignore[arg-type]

    @precondition(lambda self: self.model.reply is not None)
    @rule()
    def depart(self) -> None:
        """Depart."""
        self._apply(rl.departed(self.model.reply, self._current(), now_ns=self._now()))  # type: ignore[arg-type]
        self.model.rows = [row for row in self.model.rows if row.intent.stage is not WriteStage.EDIT]
        self.model.approval = None

    @precondition(
        lambda self: (
            self.model.reply is not None
            and self.model.reply.state is ReplyState.ACTIVE
            and self.model.reply.current_span_id is None
            and not self.model.reply.unapplied_stop
            and self.model.spans[self.model.reply.last_span_id].outcome in rl.SOURCES_PENDING_OUTCOMES
            and self.model.reply.last_span_id not in self.model.settled
        ),
    )
    @rule()
    def settle_without_reply(self) -> None:
        """Settle without reply."""
        reply = self.model.reply
        assert reply is not None
        last = self.model.spans[reply.last_span_id]
        self.model.settled.add(last.span_id)
        self._apply(rl.sources_settled_without_reply(reply, last, now_ns=self._now()))

    @precondition(lambda self: self.model.reply is not None and self.model.reply.state is ReplyState.ACTIVE)
    @rule()
    def dispatch_failure(self) -> None:
        """Dispatch failure."""
        if self.model.approval is not None:
            return
        self._apply(rl.dispatch_failed(self.model.reply, self._live(), error_text="boom", now_ns=self._now()))  # type: ignore[arg-type]

    # --- invariants ---------------------------------------------------------

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
            or (reply.state is ReplyState.PAUSED and reply.approval_id is not None)
            or (last.outcome in rl.SOURCES_PENDING_OUTCOMES and last.span_id not in self.model.settled)
            or (last.kind is SpanKind.APPROVAL_RESUME and self.model.approval is not None)
            or reply.owed_write is not None
        )
        assert owned, (reply, last)

    @invariant()
    def terminal_spans_settled_their_sources(self) -> None:
        """I6: a span's sources settle with its terminal transition, except those an approval holds."""
        for span in self.model.spans.values():
            if (
                span.kind is SpanKind.APPROVAL_RESUME
                or span.outcome not in _SETTLING_OUTCOMES
                or span.span_id in self.model.held
            ):
                continue
            assert span.span_id in self.model.settled, span

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
    def a_finished_reply_shows_its_stop(self) -> None:
        """I4: a terminal reply that is still visible has no Stop left to apply."""
        reply = self.model.reply
        if reply is not None and reply.terminal and reply.state is not ReplyState.GONE:
            assert not reply.unapplied_stop, reply


@pytest.mark.timeout(300)
def test_reply_lifecycle_invariants() -> None:
    """Random interleavings never break the lifecycle invariants."""
    ReplyLifecycleMachine.TestCase.settings = settings(
        max_examples=400,
        stateful_step_count=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    ReplyLifecycleMachine.TestCase().runTest()
