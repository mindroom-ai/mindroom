"""Pure reply lifecycle rules, one test per rule and guard."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

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
)

if TYPE_CHECKING:
    from collections.abc import Callable

GEN = "gen-2"
OLD_GEN = "gen-1"
NOW = 1_000


def _request(span_id: str = "span-1", **changes: object) -> ClaimRequest:
    request = ClaimRequest(
        span_id=span_id,
        delivery_id="$source",
        sources=SpanSources(pending=("$source",), logical=("$source",)),
        bot_generation=GEN,
        now_ns=NOW,
        new_reply_id="reply-1",
        entity_name="agent",
        room_id="!room",
        thread_id="$thread",
        membership_epoch=1,
        empty_presentation="{}",
    )
    return replace(request, **changes)  # type: ignore[arg-type]


def _context(reply: Reply | None = None, *spans: Span, debt: bool = False, **changes: object) -> ClaimContext:
    by_id = {span.span_id: span for span in spans}
    last = by_id.get(reply.last_span_id) if reply is not None else None
    current = by_id.get(reply.current_span_id) if reply is not None and reply.current_span_id else None
    context = ClaimContext(
        reply=reply,
        last_span=last,
        current_span=current,
        interactive_span=None,
        durable_write_debt=debt,
        active_generation=GEN,
    )
    return replace(context, **changes)  # type: ignore[arg-type]


def _turn() -> tuple[Reply, Span]:
    transition = rl.claim(_request(), _context())
    assert transition.reply is not None
    assert transition.claimed is not None
    return transition.reply, transition.claimed


def _write(reply: Reply, state: ReplyState, shown: str = "shown") -> TerminalWrite:
    return TerminalWrite(shown=shown, prepared_revision=reply.revision, state=state)


def _span_after(transition: rl.Transition, span_id: str) -> Span:
    return next(span for span in transition.spans if span.span_id == span_id)


def _ended(reply: Reply, span: Span, outcome: SpanOutcome) -> tuple[Reply, Span]:
    ended = replace(span, outcome=outcome, ended_at_ns=NOW)
    return replace(reply, current_span_id=None), ended


# --- claim -----------------------------------------------------------------


def test_claim_without_a_reply_creates_an_active_turn() -> None:
    """The first claim of a source creates the reply and makes its span current."""
    reply, span = _turn()
    assert reply.state is ReplyState.ACTIVE
    assert reply.current_span_id == span.span_id == reply.last_span_id
    assert span.kind is SpanKind.TURN
    assert span.base_sequence == 0


def test_claim_with_durable_write_debt_is_deferred() -> None:
    """A reply whose earlier row is unresolved is not claimed under the conversation lock."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.RELEASED)
    transition = rl.claim(_request("span-2"), _context(reply, span, debt=True))
    assert transition.outcome is Outcome.DEFERRED
    assert transition.claimed is None


def test_claim_after_release_is_a_replay() -> None:
    """A released or lost span's reply is continued by a replay."""
    reply, span = _turn()
    for outcome in (SpanOutcome.RELEASED, SpanOutcome.LOST):
        ended_reply, ended = _ended(reply, span, outcome)
        transition = rl.claim(_request("span-2"), _context(ended_reply, ended))
        assert transition.claimed is not None
        assert transition.claimed.kind is SpanKind.REPLAY


def test_claim_after_superseded_keeps_the_kind() -> None:
    """A rebuild or journal retry claims again with the superseded span's kind and no restart note."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.SUPERSEDED)
    transition = rl.claim(_request("span-2"), _context(reply, span))
    assert transition.claimed is not None
    assert transition.claimed.kind is SpanKind.TURN


def test_claim_ends_a_span_an_older_generation_left_current() -> None:
    """A span of an older bot instance is lost before the new claim decides."""
    reply, span = _turn()
    stale = replace(span, bot_generation=OLD_GEN)
    transition = rl.claim(_request("span-2"), _context(reply, stale))
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.LOST
    assert transition.claimed is not None
    assert transition.claimed.kind is SpanKind.REPLAY


def test_claim_with_a_live_span_ends_the_reply_instead_of_raising() -> None:
    """Claims run under the conversation lock, so a live current span is unmodeled: no span opens, the reply ends."""
    reply, span = _turn()
    transition = rl.claim(_request("span-2"), _context(reply, span))
    assert transition.unmodeled is not None
    assert transition.claimed is None
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert CancelSpan(span.span_id) in transition.effects


def test_claim_on_a_terminal_reply_without_an_edit_runs_nothing() -> None:
    """Only a regeneration claims a reply that already ended; a Stop can end one between the source gate and a claim."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED)
    transition = rl.claim(_request("span-2"), _context(reply, span))
    assert transition.outcome is Outcome.DUPLICATE
    assert transition.claimed is None
    assert transition.reply == reply


def test_regeneration_of_a_completed_reply_stores_its_rollback() -> None:
    """An edit makes a completed reply active again and remembers what to restore."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, presentation="answer")
    transition = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.ACTIVE
    claimed = transition.claimed
    assert claimed is not None
    assert claimed.kind is SpanKind.REGENERATION
    assert claimed.rollback is not None
    assert claimed.rollback.state is ReplyState.COMPLETED
    assert claimed.rollback.presentation == "answer"


def test_a_regeneration_with_no_reply_runs_nothing() -> None:
    """The regenerator regenerates only a reply it found, so no claim creates a reply to regenerate."""
    transition = rl.claim(_request(delivery_id="$edit", driving_edit_id="$edit"), _context())
    assert transition.claimed is None
    assert transition.unmodeled is not None


@pytest.mark.parametrize("held", ["paused", "gone"])
def test_an_edit_of_a_held_or_gone_reply_runs_nothing(held: str) -> None:
    """An approval holds its reply, and a reply that is gone shows nothing to regenerate."""
    if held == "paused":
        reply, span, transition = _paused()
        span = _span_after(transition, span.span_id)
    else:
        reply, span = _turn()
        reply, span = _ended(reply, span, SpanOutcome.SUPPRESSED)
        reply = replace(reply, state=ReplyState.GONE)
    edit = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert edit.outcome is Outcome.DUPLICATE
    assert edit.claimed is None
    assert edit.effects == ()


def test_an_edit_of_a_reply_waiting_for_its_replay_regenerates_without_a_rollback() -> None:
    """The regeneration answers the interrupted turn; nothing unfinished is ever restored."""
    reply, span = _turn()
    released = rl.release(replace(reply, event_id="$reply", placeholder_only=False), span, now_ns=NOW)
    assert released.reply is not None
    waiting = _span_after(released, span.span_id)
    edit_sources = SpanSources(pending=("$edit",), logical=("$source",))
    edit = rl.claim(
        _request("span-2", delivery_id="$edit", driving_edit_id="$edit", sources=edit_sources),
        _context(released.reply, waiting),
    )
    assert edit.claimed is not None
    assert edit.claimed.kind is SpanKind.REGENERATION
    assert edit.claimed.rollback is None
    assert edit.claimed.sources.pending == ("$edit",)


def test_regeneration_rerun_keeps_its_rollback() -> None:
    """A released regeneration is re-run as a regeneration with the same snapshot."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED)
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    reply2, span2 = _ended(regen.reply, regen.claimed, SpanOutcome.RELEASED)
    rerun = rl.claim(_request("span-3", delivery_id="$edit", driving_edit_id="$edit"), _context(reply2, span2))
    assert rerun.claimed is not None
    assert rerun.claimed.kind is SpanKind.REGENERATION
    assert rerun.claimed.rollback == regen.claimed.rollback


@pytest.mark.parametrize("state", [ReplyState.GONE, ReplyState.COMPLETED, ReplyState.CANCELLED])
def test_a_retried_regeneration_whose_reply_ended_meanwhile_runs_nothing(state: ReplyState) -> None:
    """A deletion, departure, or Stop that ended the reply between the retry's source gate and its claim wins."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED)
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    released, last = _ended(regen.reply, regen.claimed, SpanOutcome.RELEASED)
    ended = replace(released, state=state)

    retry = rl.claim(_request("span-3", delivery_id="$edit", driving_edit_id="$edit"), _context(ended, last))

    assert retry.outcome is Outcome.DUPLICATE
    assert retry.claimed is None
    assert retry.reply == ended


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (SpanOutcome.COMPLETED, ReplyState.COMPLETED),
        (SpanOutcome.CANCELLED, ReplyState.CANCELLED),
        (SpanOutcome.FAILED, ReplyState.FAILED),
    ],
)
def test_a_retried_regeneration_its_reply_already_answered_is_a_duplicate(
    outcome: SpanOutcome,
    state: ReplyState,
) -> None:
    """A retry of the edit the reply's last span answered runs nothing again; an interrupted one re-runs."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED)
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    answered, last = _ended(regen.reply, regen.claimed, outcome)
    answered = replace(answered, state=state)

    retry = rl.claim(_request("span-3", delivery_id="$edit", driving_edit_id="$edit"), _context(answered, last))

    assert retry.outcome is Outcome.DUPLICATE
    assert retry.claimed is None
    assert retry.reply == answered
    assert retry.spans == ()
    assert retry.effects == ()


def test_approval_resume_claims_the_paused_reply() -> None:
    """A resume continues the paused reply as the current span."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1")
    transition = rl.claim(_request("span-2", approval_id="approval-1"), _context(reply, span))
    assert transition.claimed is not None
    assert transition.claimed.kind is SpanKind.APPROVAL_RESUME
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.ACTIVE


def test_approval_resume_with_debt_is_refused() -> None:
    """An unresolved pause row leaves the continuation ready for a later claim."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1")
    transition = rl.claim(_request("span-2", approval_id="approval-1"), _context(reply, span, debt=True))
    assert transition.outcome is Outcome.DEFERRED


def test_interactive_span_is_adopted_or_replayed_when_lost() -> None:
    """A selection's acknowledgement span becomes current, unless its bot instance is gone."""
    created = rl.interactive_acknowledgement(_request("ack-span"), shown="ack")
    assert created.reply is not None
    assert created.reply.placeholder_only
    ack = created.spans[0]
    adopted = rl.claim(
        _request("ignored", interactive_span_id="ack-span"),
        _context(created.reply, ack, interactive_span=ack),
    )
    assert adopted.claimed == ack
    assert adopted.reply is not None
    assert adopted.reply.current_span_id == "ack-span"

    lost = replace(ack, outcome=SpanOutcome.LOST, ended_at_ns=NOW)
    replayed = rl.claim(
        _request("span-2", interactive_span_id="ack-span"),
        _context(created.reply, lost, interactive_span=lost),
    )
    assert replayed.claimed is not None
    assert replayed.claimed.kind is SpanKind.REPLAY

    stopped = replace(created.reply, state=ReplyState.CANCELLED)
    refused = rl.claim(
        _request("ignored", interactive_span_id="ack-span"),
        _context(stopped, ack, interactive_span=ack),
    )
    # A Stop ended the selection's reply first: the selection runs nothing.
    assert refused.outcome is Outcome.DUPLICATE
    assert refused.claimed is None


# --- writes ----------------------------------------------------------------


def test_write_ahead_allocates_the_next_sequence_and_confirms_the_previous_edit() -> None:
    """Each progress edit is recorded before it is sent and confirmed by the next one."""
    reply, span = _turn()
    first = rl.write_ahead(
        reply,
        span,
        shown="p1",
        previous=None,
        active_generation=GEN,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert first.reply is not None
    assert first.reply.possibly_shown_seq == 1
    assert not first.reply.confirmed
    second = rl.write_ahead(
        first.reply,
        span,
        shown="p2",
        previous=rl.ProgressConfirmation(event_id="$reply", placeholder_only=False),
        active_generation=GEN,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert second.reply is not None
    assert second.reply.confirmed_seq == 1
    assert second.reply.possibly_shown_seq == 2
    assert second.reply.event_id == "$reply"
    assert second.reply.revision == reply.revision


def test_write_ahead_of_an_older_generation_is_refused() -> None:
    """A span whose bot instance was replaced can no longer write."""
    reply, span = _turn()
    transition = rl.write_ahead(
        reply,
        span,
        shown="p",
        previous=None,
        active_generation="gen-3",
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert transition.outcome is Outcome.STALE


def test_initial_create_acknowledgement_binds_the_event() -> None:
    """The create row's acknowledgement binds the reply's event and its placeholder state."""
    reply, span = _turn()
    initial = rl.enqueue_initial(
        reply,
        span,
        shown="ph",
        placeholder_only=True,
        prepared_revision=reply.revision,
        now_ns=NOW,
    )
    assert initial.row is not None
    assert initial.row.stage is WriteStage.INITIAL
    assert initial.reply is not None
    acked = rl.write_acknowledged(
        initial.reply,
        WriteFacts(WriteStage.INITIAL, initial.row.sequence, span.span_id, creates_event=True, placeholder_only=True),
        event_id="$reply",
        membership_current=True,
        now_ns=NOW,
    )
    assert acked.reply is not None
    assert acked.reply.event_id == "$reply"
    assert acked.reply.placeholder_only
    assert acked.reply.confirmed
    # A second create of one reply keeps the first binding and redacts the stray event.
    stray = rl.write_acknowledged(
        acked.reply,
        WriteFacts(WriteStage.INITIAL, 1, span.span_id, creates_event=True, placeholder_only=True),
        event_id="$other",
        membership_current=True,
        now_ns=NOW,
    )
    assert stray.unmodeled is not None
    assert stray.reply is not None
    assert stray.reply.event_id == "$reply"
    assert "$other" in stray.reply.redaction_pending


def test_late_create_of_a_gone_reply_is_queued_for_redaction() -> None:
    """An event created after the reply was given up is removed."""
    reply, span = _turn()
    gone = replace(reply, state=ReplyState.GONE, current_span_id=None)
    acked = rl.write_acknowledged(
        gone,
        WriteFacts(WriteStage.INITIAL, 1, span.span_id, creates_event=True, placeholder_only=True),
        event_id="$late",
        membership_current=True,
        now_ns=NOW,
    )
    assert acked.reply is not None
    assert acked.reply.redaction_pending == ("$late",)


# --- finish, stopped, fail, suppress --------------------------------------


def test_finish_completes_and_settles() -> None:
    """A finished answer is the span's FINAL and settles its sources."""
    reply, span = _turn()
    transition = rl.finish(reply, span, _write(reply, ReplyState.COMPLETED), now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED
    assert transition.reply.current_span_id is None
    assert transition.row is not None
    assert transition.row.stage is WriteStage.FINAL
    assert SettleSources(span.span_id) in transition.effects
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.COMPLETED


def test_finish_with_a_stop_committed_meanwhile_recomputes() -> None:
    """A payload rendered before a Stop committed is refused; the re-render is cancelled."""
    reply, span = _turn()
    write = _write(reply, ReplyState.COMPLETED)
    stopped = rl.stop(reply, span, StopFacts(receipt_order=9, span_live=True), now_ns=NOW)
    assert stopped.reply is not None
    assert rl.finish(stopped.reply, span, write, now_ns=NOW).outcome is Outcome.RECOMPUTE
    assert rl._expected_terminal_state(stopped.reply, ReplyState.COMPLETED) is ReplyState.CANCELLED
    # Rendered at the Stop's revision, which the span learned from its own write, it still renders again.
    seen = rl.finish(stopped.reply, span, _write(stopped.reply, ReplyState.COMPLETED), now_ns=NOW)
    assert seen.outcome is Outcome.RECOMPUTE
    final = rl.finish(stopped.reply, span, _write(stopped.reply, ReplyState.CANCELLED), now_ns=NOW)
    assert final.reply is not None
    assert final.reply.state is ReplyState.CANCELLED
    assert not final.reply.unapplied_stop


def test_failure_rendered_at_a_stops_revision_recomputes() -> None:
    """A stream error rendered after the span learned the Stop's revision renders again, cancelled."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    stopped = rl.stop(reply, span, StopFacts(receipt_order=9, span_live=True), now_ns=NOW)
    assert stopped.reply is not None
    failed = _write(stopped.reply, ReplyState.FAILED)
    assert rl.fail(stopped.reply, span, failed, phase="delivery", now_ns=NOW).outcome is Outcome.RECOMPUTE
    cancelled = rl.fail(stopped.reply, span, _write(stopped.reply, ReplyState.CANCELLED), phase="delivery", now_ns=NOW)
    assert cancelled.reply is not None
    assert cancelled.reply.state is ReplyState.CANCELLED
    assert not cancelled.reply.unapplied_stop


def test_finish_of_a_stale_span_cancels_it() -> None:
    """Only the current span changes the answer; a stale live one is cancelled."""
    reply, span = _turn()
    other = replace(span, span_id="span-x")
    transition = rl.finish(reply, other, _write(reply, ReplyState.COMPLETED), now_ns=NOW)
    assert transition.outcome is Outcome.STALE
    assert transition.effects == (CancelSpan("span-x"),)


def test_approval_resume_finish_leaves_settlement_to_the_continuation() -> None:
    """A resumed reply's FINAL hands no sources over; the continuation's finish settles them."""
    reply, span = _turn()
    # A resume runs while its continuation holds the reply.
    reply = _held(reply)
    resume = replace(span, kind=SpanKind.APPROVAL_RESUME, approval_id="approval-1")
    transition = rl.finish(reply, resume, _write(reply, ReplyState.COMPLETED), now_ns=NOW)
    assert transition.row is not None
    assert transition.effects == ()
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED


def test_stopped_cancels_with_the_shown_content() -> None:
    """A Stop reaching the running span ends it cancelled with a terminal row."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(receipt_order=3, span_live=True), now_ns=NOW)
    assert stop.effects == (CancelSpan(span.span_id, by_stop=True),)
    assert stop.reply is not None
    transition = rl.stopped(stop.reply, span, _write(stop.reply, ReplyState.CANCELLED), now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.CANCELLED
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.CANCELLED


def test_stopped_regeneration_before_its_first_acknowledged_write_restores_silently() -> None:
    """The old answer stays as it was, with no note."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, presentation="old")
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    stop = rl.stop(regen.reply, regen.claimed, StopFacts(receipt_order=8, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    transition = rl.stopped(stop.reply, regen.claimed, None, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED
    assert transition.reply.presentation == "old"
    assert transition.row is None
    assert _span_after(transition, "span-2").outcome is SpanOutcome.RESTORED


def _regenerating() -> tuple[Reply, Span]:
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, presentation="old", event_id="$reply")
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    return regen.reply, regen.claimed


def test_stop_after_a_regenerations_progress_edit_landed_cancels_instead_of_restoring() -> None:
    """The terminal write's confirmation of a landed edit means the room no longer shows the old answer."""
    reply, span = _regenerating()
    stop = rl.stop(reply, span, StopFacts(receipt_order=8, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    write = replace(
        _write(stop.reply, ReplyState.CANCELLED, shown="new partial"),
        confirms=rl.ProgressConfirmation(event_id="$reply", placeholder_only=False),
    )
    ahead = rl.write_ahead(
        stop.reply,
        span,
        shown="new partial",
        previous=None,
        active_generation=GEN,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert ahead.reply is not None
    transition = rl.stopped(ahead.reply, span, replace(write, prepared_revision=ahead.reply.revision), now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.CANCELLED
    assert transition.row is not None
    assert _span_after(transition, "span-2").outcome is SpanOutcome.CANCELLED


def test_dispatch_failure_of_a_regeneration_before_its_first_write_keeps_the_old_answer() -> None:
    """A regeneration that failed to start restores the old answer, with no error note over it."""
    reply, span = _regenerating()
    transition = rl.dispatch_failed(reply, span, error_text="setup failed", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED
    assert transition.reply.presentation == "old"
    assert transition.reply.owed_write is None
    assert _span_after(transition, "span-2").outcome is SpanOutcome.RESTORED
    assert transition.effects == (SettleSources("span-2"),)


def _progress(reply: Reply, span: Span, shown: str = "new partial") -> Reply:
    """Write a progress edit ahead that Matrix has not acknowledged."""
    ahead = rl.write_ahead(
        reply,
        span,
        shown=shown,
        previous=None,
        active_generation=GEN,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert ahead.reply is not None
    return ahead.reply


def test_a_stop_after_an_unacknowledged_progress_edit_does_not_restore() -> None:
    """A progress edit Matrix may already show counts, so the Stop ends the reply cancelled instead of restoring."""
    reply, span = _regenerating()
    stop = rl.stop(reply, span, StopFacts(receipt_order=8, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    transition = rl.stopped(_progress(stop.reply, span), span, None, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.CANCELLED
    assert transition.reply.owed_write == rl.OwedWrite("span-2", rl._NOTE_CANCELLED)
    assert _span_after(transition, "span-2").outcome is SpanOutcome.CANCELLED


def test_a_stop_with_no_span_running_restores_a_regeneration_that_wrote_nothing() -> None:
    """A regeneration a restart left waiting for its replay still holds the answer it would replace."""
    reply, span = _regenerating()
    lost = rl.owner_lost(reply, span, rl.OwnerLostFacts(active_generation="gen-next", sources_pending=True), now_ns=NOW)
    assert lost.reply is not None
    waiting = _span_after(lost, "span-2")
    transition = rl.stop(lost.reply, waiting, StopFacts(receipt_order=8, span_live=False), now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED
    assert transition.reply.presentation == "old"
    assert transition.reply.owed_write is None
    assert not transition.reply.unapplied_stop
    assert transition.effects == (SettleSources("span-2"),)


def test_a_restart_applies_a_stop_its_regeneration_never_saw_as_a_live_stop_would() -> None:
    """The Stop committed, the process died before the span saw it: the answer the regeneration never replaced stands."""
    reply, span = _regenerating()
    stop = rl.stop(reply, span, StopFacts(receipt_order=8, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    for pending in (True, False):
        restarted = rl.owner_lost(
            stop.reply,
            span,
            rl.OwnerLostFacts(active_generation="gen-next", sources_pending=pending),
            now_ns=NOW,
        )
        assert restarted.reply is not None
        assert restarted.reply.state is ReplyState.COMPLETED
        assert restarted.reply.presentation == "old"
        assert restarted.reply.owed_write is None
        assert not restarted.reply.unapplied_stop
        assert restarted.effects == (SettleSources("span-2"),)


def test_a_released_resumes_sources_settle_like_any_once_its_approval_is_gone() -> None:
    """A release hands the resume's sources back to replay; a superseded replay or a removed entity settles them."""
    reply, span, _transition = _paused()
    resume = rl.claim(_request("resume", delivery_id="$source", approval_id="approval-1"), _context(reply, span))
    assert resume.reply is not None
    assert resume.claimed is not None
    released = rl.approval_released(resume.reply, resume.claimed, now_ns=NOW)
    assert released.reply is not None
    # The release deleted the continuation, so the store loads the reply unheld.
    unheld = _held(released.reply, None)
    ended = _span_after(released, "resume")
    superseded = rl.replay_superseded(unheld, ended, durable_write_debt=False, now_ns=NOW)
    assert SettleSources("resume", answered=False) in superseded.effects
    removed = rl.removed_entity(unheld, ended, now_ns=NOW)
    assert removed.effects == (SettleSources("resume", answered=False),)


def test_an_edit_superseding_an_unanswered_regeneration_keeps_the_original_rollback() -> None:
    """The newer regeneration goes back to the answer the first one was replacing, not to its unfinished state."""
    reply, span = _regenerating()
    released = rl.release(reply, span, now_ns=NOW)
    assert released.reply is not None
    newer = rl.claim(
        _request("span-3", delivery_id="$edit-2", driving_edit_id="$edit-2"),
        _context(released.reply, _span_after(released, "span-2")),
    )
    assert newer.claimed is not None
    assert newer.claimed.rollback == span.rollback
    assert newer.reply is not None
    failed = rl.dispatch_failed(newer.reply, newer.claimed, error_text="setup failed", now_ns=NOW)
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.COMPLETED
    assert failed.reply.presentation == "old"


def test_a_refused_final_of_a_regeneration_ends_the_reply_failed_with_its_note() -> None:
    """A regeneration's answer Matrix refused for good says delivery failed, over the answer it was replacing."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    shown = replace(reply, state=ReplyState.COMPLETED, presentation="old", event_id="$reply")
    shown = replace(shown, possibly_shown="old", possibly_shown_seq=1, reply_sequence=1, confirmed_seq=1)
    claimed = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(shown, span))
    assert claimed.reply is not None
    assert claimed.claimed is not None
    final = rl.finish(claimed.reply, claimed.claimed, _write(claimed.reply, ReplyState.COMPLETED, "new"), now_ns=NOW)
    assert final.reply is not None
    assert final.row is not None
    facts = WriteFacts(WriteStage.FINAL, final.row.sequence, "span-2", creates_event=False, placeholder_only=False)

    refused = rl.write_failed(final.reply, _span_after(final, "span-2"), rl.FailedWrite(facts, "refused"), now_ns=NOW)

    assert refused.reply is not None
    assert refused.reply.state is ReplyState.FAILED
    assert refused.reply.owed_write == rl.OwedWrite("span-2", rl._NOTE_DELIVERY_FAILED)


def test_stopped_without_a_recorded_stop_ends_the_reply_unmodeled() -> None:
    """A span cannot report a Stop its reply never recorded; the reply ends failed rather than raising."""
    reply, span = _turn()
    transition = rl.stopped(reply, span, _write(reply, ReplyState.CANCELLED), now_ns=NOW)
    assert transition.unmodeled is not None
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED


def test_an_interrupted_regeneration_that_wrote_nothing_leaves_the_answer_without_a_note() -> None:
    """The note an interruption renders would replace the answer the regeneration never touched; the retry reruns it."""
    reply, span = _regenerating()
    note = replace(_write(reply, ReplyState.ACTIVE, shown="interrupted"), prepared_revision=reply.revision)

    transition = rl.fail(reply, span, note, phase="pre_delivery", now_ns=NOW)

    assert transition.row is None
    assert transition.reply is not None
    assert transition.reply.presentation == reply.presentation
    assert transition.reply.current_span_id is None
    assert _span_after(transition, "span-2").outcome is SpanOutcome.RELEASED
    assert transition.effects == ()


def test_pre_delivery_failure_releases_the_span_and_keeps_the_placeholder() -> None:
    """Main retries the sources, and the retry streams into the kept placeholder."""
    reply, span = _turn()
    transition = rl.fail(reply, span, None, phase="pre_delivery", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.ACTIVE
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.RELEASED
    assert transition.effects == ()


def test_pre_delivery_failure_of_a_resumed_reply_writes_its_note_without_settling() -> None:
    """The interruption note is an edit row; the sources stay pending."""
    reply, span = _turn()
    transition = rl.fail(reply, span, _write(reply, ReplyState.ACTIVE), phase="pre_delivery", now_ns=NOW)
    assert transition.row is not None
    assert transition.row.stage is WriteStage.EDIT
    assert transition.effects == ()


def test_delivery_failure_writes_a_failed_terminal_row() -> None:
    """An error during delivery ends the reply failed with its note as the FINAL."""
    reply, span = _turn()
    transition = rl.fail(reply, span, _write(reply, ReplyState.FAILED), phase="delivery", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.row is not None
    assert transition.row.stage is WriteStage.FINAL


def test_failure_after_the_terminal_row_is_a_duplicate() -> None:
    """A gateway error report after the terminal row was enqueued changes nothing."""
    reply, span = _turn()
    finished = rl.finish(reply, span, _write(reply, ReplyState.COMPLETED), now_ns=NOW)
    assert finished.reply is not None
    ended = _span_after(finished, span.span_id)
    again = rl.fail(finished.reply, ended, _write(finished.reply, ReplyState.FAILED), phase="delivery", now_ns=NOW)
    assert again.outcome is Outcome.DUPLICATE


def test_failure_with_an_unapplied_stop_cancels() -> None:
    """A Stop takes precedence over an error."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(receipt_order=2, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    cancelled = rl.fail(stop.reply, span, _write(stop.reply, ReplyState.CANCELLED), phase="delivery", now_ns=NOW)
    assert cancelled.reply is not None
    assert cancelled.reply.state is ReplyState.CANCELLED
    assert cancelled.row is not None


@pytest.mark.parametrize(
    "exit_without_write",
    [
        lambda reply, span: rl.fail(reply, span, None, phase="pre_delivery", now_ns=NOW),
        lambda reply, span: rl.release(reply, span, now_ns=NOW),
    ],
    ids=["error_before_delivery", "release"],
)
def test_an_exit_that_rendered_nothing_still_honors_a_recorded_stop(
    exit_without_write: Callable[[Reply, Span], rl.Transition],
) -> None:
    """A Stop outranks a retry: the reply ends cancelled, its sources settle, and it owes the cancel note."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(receipt_order=2, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    transition = exit_without_write(stop.reply, span)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.CANCELLED
    assert not transition.reply.unapplied_stop
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_CANCELLED)
    assert transition.effects == (SettleSources(span.span_id),)
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.CANCELLED


@pytest.mark.parametrize(
    ("event_id", "placeholder_only", "expected_state", "redacted", "owed"),
    [
        (None, False, ReplyState.GONE, (), None),
        ("$reply", True, ReplyState.GONE, ("$reply",), None),
        ("$reply", False, ReplyState.CANCELLED, (), rl.OwedWrite("span-1", rl._NOTE_INTERRUPTED)),
    ],
)
def test_suppression_follows_mains_branches(
    event_id: str | None,
    placeholder_only: bool,
    expected_state: ReplyState,
    redacted: tuple[str, ...],
    owed: rl.OwedWrite | None,
) -> None:
    """A suppressed answer redacts a placeholder and keeps substantive content, ended by a note instead of streaming."""
    reply, span = _turn()
    reply = replace(reply, event_id=event_id, placeholder_only=placeholder_only)
    transition = rl.suppress(reply, span, reason="suppressed", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is expected_state
    assert transition.reply.redaction_pending == redacted
    assert transition.reply.owed_write == owed


def test_suppressing_after_visible_progress_ends_what_it_showed() -> None:
    """Streamed progress stays with a terminal note, the cancel note after a Stop; it never stays streaming."""
    reply, span = _turn()
    shown = replace(_progress(reply, span), event_id="$reply", placeholder_only=False)
    suppressed = rl.suppress(shown, span, reason="suppressed", now_ns=NOW)
    assert suppressed.reply is not None
    assert suppressed.reply.state is ReplyState.CANCELLED
    assert suppressed.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_INTERRUPTED)
    stop = rl.stop(shown, span, StopFacts(receipt_order=8, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    stopped = rl.suppress(stop.reply, span, reason="suppressed", now_ns=NOW)
    assert stopped.reply is not None
    assert stopped.reply.state is ReplyState.CANCELLED
    assert stopped.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_CANCELLED)
    assert not stopped.reply.unapplied_stop
    regenerating, regeneration = _regenerating()
    partial = rl.suppress(_progress(regenerating, regeneration), regeneration, reason="suppressed", now_ns=NOW)
    assert partial.reply is not None
    assert partial.reply.owed_write == rl.OwedWrite("span-2", rl._NOTE_INTERRUPTED)


def test_hook_failure_of_substantive_content_fails_the_reply() -> None:
    """A before-response hook exception leaves shown content and fails the reply with a note."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    transition = rl.suppress(reply, span, reason="hook_failed", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_INTERRUPTED)
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.FAILED


def test_release_and_superseded_keep_sources_pending() -> None:
    """Shutdown and retries leave the reply active with pending sources."""
    reply, span = _turn()
    for outcome in (SpanOutcome.RELEASED, SpanOutcome.SUPERSEDED):
        transition = rl.release(reply, span, outcome=outcome, now_ns=NOW)
        assert transition.reply is not None
        assert transition.reply.state is ReplyState.ACTIVE
        assert transition.effects == ()


# --- pause and approvals --------------------------------------------------


def _paused(*, in_place: bool = False) -> tuple[Reply, Span, rl.Transition]:
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    transition = rl.pause(
        reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=reply.revision, stage=WriteStage.EDIT),
        in_place=in_place,
        now_ns=NOW,
    )
    assert transition.reply is not None
    # The continuation created with the pause holds the reply; the store reads that hold onto it.
    return _held(transition.reply), span, transition


def _held(reply: Reply, approval_id: str | None = "approval-1") -> Reply:
    """Return the reply as the store loads it while ``approval_id``'s continuation holds it."""
    return replace(reply, approval_id=approval_id)


def test_suppressing_a_span_approved_in_place_leaves_the_reply_to_its_approval() -> None:
    """Even before its create is acknowledged, the approval's failure settlement writes the end its finish waits for."""
    reply, span, _transition = _paused(in_place=True)
    unbound = replace(reply, event_id=None)
    approved = rl.resumed_in_place(unbound, span, approval_id="approval-1", now_ns=NOW)
    assert approved.reply is not None
    suppressed = rl.suppress(approved.reply, span, reason="suppressed", now_ns=NOW)
    assert suppressed.reply is not None
    assert suppressed.reply.state is ReplyState.ACTIVE
    assert suppressed.reply.current_span_id is None
    assert suppressed.effects == ()
    assert _span_after(suppressed, span.span_id).outcome is SpanOutcome.SUPPRESSED


def test_a_failed_approval_ends_a_resume_an_older_instance_left_current() -> None:
    """Nothing runs that resume any more: the failure ends it lost, and the reply no longer names the approval."""
    reply, _span, _transition = _paused()
    resume = rl.claim(
        _request("resume", delivery_id="$source", approval_id="approval-1"),
        _context(reply, _span),
    )
    assert resume.reply is not None
    assert resume.claimed is not None
    orphan = replace(resume.claimed, bot_generation=OLD_GEN)
    settled = rl.approval_settled(
        resume.reply,
        orphan,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="cancelled_by_user",
        answers_turn=True,
        now_ns=NOW,
    )
    assert settled.reply is not None
    assert settled.reply.state is ReplyState.CANCELLED
    assert settled.reply.current_span_id is None
    assert _span_after(settled, "resume").outcome is SpanOutcome.LOST


def test_sources_settled_with_a_stop_recorded_end_the_reply_cancelled() -> None:
    """A Stop recorded before the sources settled decides the end, not the interruption note."""
    reply, span = _interrupted()
    stopped = replace(reply, stop_receipt_order=4)
    ended = rl.sources_settled_without_reply(stopped, span, now_ns=NOW)
    assert ended.reply is not None
    assert ended.reply.state is ReplyState.CANCELLED
    assert not ended.reply.unapplied_stop
    assert ended.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_CANCELLED)


def test_pause_ends_the_span_and_writes_an_edit_row() -> None:
    """A pause is a durable edit row; the reply waits for its approval."""
    reply, span, transition = _paused()
    assert reply.state is ReplyState.PAUSED
    assert reply.approval_id == "approval-1"
    assert reply.current_span_id is None
    assert transition.row is not None
    assert transition.row.stage is WriteStage.EDIT
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.PAUSED


def test_pause_shown_by_the_replys_create_writes_no_row() -> None:
    """A reply that had no event shows its pause with its create; the pause then only records the wait."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply", possibly_shown="paused")
    transition = rl.pause(
        reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=reply.revision, stage=None),
        in_place=False,
        now_ns=NOW,
    )
    assert transition.row is None
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.PAUSED
    assert transition.reply.reply_sequence == reply.reply_sequence
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.PAUSED


def test_pause_in_place_keeps_the_span_current_and_resumes_in_place() -> None:
    """A response-local approval wait keeps the span; its decision returns the reply to active."""
    reply, span, transition = _paused(in_place=True)
    assert reply.current_span_id == span.span_id
    assert transition.spans == ()
    resumed = rl.resumed_in_place(reply, span, approval_id="approval-1", now_ns=NOW)
    assert resumed.reply is not None
    assert resumed.reply.state is ReplyState.ACTIVE


def test_a_resume_that_waited_in_place_stays_stoppable_through_its_approval() -> None:
    """The resume keeps its approval's hold after an in-place decision, so a Stop fences that approval."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1", event_id="$reply")
    resume = rl.claim(_request("span-2", approval_id="approval-1"), _context(reply, span))
    assert resume.reply is not None
    assert resume.claimed is not None
    waiting = rl.pause(
        resume.reply,
        resume.claimed,
        rl.PauseWrite(shown="again", prepared_revision=resume.reply.revision, stage=WriteStage.EDIT),
        in_place=True,
        now_ns=NOW,
    )
    assert waiting.reply is not None
    resumed = rl.resumed_in_place(waiting.reply, resume.claimed, approval_id="approval-1", now_ns=NOW)
    assert resumed.reply is not None
    assert resumed.reply.state is ReplyState.ACTIVE
    assert resumed.reply.approval_id == "approval-1"
    stop = rl.stop(
        resumed.reply,
        resume.claimed,
        StopFacts(receipt_order=9, span_live=True),
        now_ns=NOW,
    )
    assert stop.effects == (
        FenceApproval("approval-1", "cancelled_by_user"),
        CancelSpan(resume.claimed.span_id, by_stop=True),
        WakeApproval("approval-1"),
    )


def test_pause_with_an_unapplied_stop_defers_to_the_stop_path() -> None:
    """A Stop recorded before the pause wins."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(receipt_order=1, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    transition = rl.pause(
        stop.reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=stop.reply.revision, stage=WriteStage.EDIT),
        in_place=False,
        now_ns=NOW,
    )
    assert transition.outcome is Outcome.STOPPED


def test_stop_on_a_paused_reply_fences_and_wakes_its_approval() -> None:
    """A Stop on a paused reply requests approval failure; the settlement ends the reply."""
    reply, _span, _transition = _paused()
    stop = rl.stop(reply, None, StopFacts(receipt_order=4, span_live=False), now_ns=NOW)
    assert stop.effects == (FenceApproval("approval-1", "cancelled_by_user"), WakeApproval("approval-1"))
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.PAUSED
    settled = rl.approval_settled(
        stop.reply,
        None,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="cancelled_by_user",
        answers_turn=True,
        now_ns=NOW,
    )
    assert settled.reply is not None
    assert settled.reply.state is ReplyState.CANCELLED


def test_stop_on_an_in_place_wait_also_cancels_the_waiting_span() -> None:
    """A response-local wait's span is cancelled and its cards expire through the approval wake."""
    reply, span, _transition = _paused(in_place=True)
    stop = rl.stop(reply, span, StopFacts(receipt_order=4, span_live=True), now_ns=NOW)
    assert CancelSpan(span.span_id, by_stop=True) in stop.effects
    assert FenceApproval("approval-1", "cancelled_by_user") in stop.effects
    assert WakeApproval("approval-1") in stop.effects


def test_a_stopped_in_place_wait_leaves_its_sources_to_the_approval() -> None:
    """The cancelled wait shows the note, but the continuation's settlement, which its wake runs, settles the sources."""
    reply, span, _transition = _paused(in_place=True)
    stop = rl.stop(reply, span, StopFacts(receipt_order=4, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    stopped = rl.stopped(stop.reply, span, _write(stop.reply, ReplyState.CANCELLED), now_ns=NOW)
    assert stopped.reply is not None
    assert stopped.reply.state is ReplyState.CANCELLED
    assert stopped.row is not None
    assert not any(isinstance(effect, rl.SettleSources) for effect in stopped.effects)
    released = rl.stopped(stop.reply, span, None, now_ns=NOW)
    assert not any(isinstance(effect, rl.SettleSources) for effect in released.effects)

    # The continuation's finish releases its hold; a later edit's regeneration owns its sources.
    ended = _span_after(stopped, span.span_id)
    settled = rl.approval_settled(
        stopped.reply,
        ended,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="cancelled_by_user",
        answers_turn=True,
        now_ns=NOW,
    )
    assert settled.reply is not None
    assert settled.reply.state is ReplyState.CANCELLED
    # The finish deletes the continuation with it, so the store reads the reply free.
    regeneration = rl.claim(
        _request("span-2", delivery_id="$edit", driving_edit_id="$edit"),
        _context(_held(settled.reply, None), ended),
    )
    assert regeneration.reply is not None
    assert regeneration.claimed is not None
    answer = rl.finish(
        regeneration.reply,
        regeneration.claimed,
        _write(regeneration.reply, ReplyState.COMPLETED),
        now_ns=NOW,
    )
    assert rl.SettleSources(regeneration.claimed.span_id) in answer.effects


def test_a_wait_in_place_keeps_its_stop_button_until_its_span_ends() -> None:
    """The waiting span still runs, so the button stays through the wait and goes when the span ends."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    shown = rl.record_stop_button(reply, event_id="$button", membership_current=True, now_ns=NOW)
    assert shown.reply is not None
    waiting = rl.pause(
        shown.reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=shown.reply.revision, stage=WriteStage.EDIT),
        in_place=True,
        now_ns=NOW,
    )
    assert waiting.reply is not None
    waiting = replace(waiting, reply=_held(waiting.reply))
    assert waiting.reply.stop_button_event_id == "$button"
    resumed = rl.resumed_in_place(waiting.reply, span, approval_id="approval-1", now_ns=NOW)
    assert resumed.reply is not None
    assert resumed.reply.stop_button_event_id == "$button"
    stop = rl.stop(waiting.reply, span, StopFacts(receipt_order=4, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    ended = rl.stopped(stop.reply, span, _write(stop.reply, ReplyState.CANCELLED), now_ns=NOW)
    assert ended.reply is not None
    assert ended.reply.stop_button_event_id is None
    assert ended.reply.redaction_pending == ("$button",)


def test_a_button_acknowledged_during_a_wait_in_place_stays_until_the_span_ends() -> None:
    """A Stop button whose send lands after the span paused in place is the waiting reply's button."""
    reply, span = _turn()
    waiting = rl.pause(
        replace(reply, event_id="$reply"),
        span,
        rl.PauseWrite(shown="paused", prepared_revision=reply.revision, stage=WriteStage.EDIT),
        in_place=True,
        now_ns=NOW,
    )
    assert waiting.reply is not None
    late = rl.record_stop_button(waiting.reply, event_id="$button", membership_current=True, now_ns=NOW)
    assert late.reply is not None
    assert late.reply.stop_button_event_id == "$button"
    assert late.reply.redaction_pending == ()
    paused = rl.record_stop_button(
        replace(waiting.reply, current_span_id=None),
        event_id="$late",
        membership_current=True,
        now_ns=NOW,
    )
    assert paused.reply is not None
    assert paused.reply.redaction_pending == ("$late",)


def test_a_wait_in_place_an_older_instance_ran_ends_as_a_pause_at_start() -> None:
    """After a restart the reply waits for its decision as any pause does, and its Stop button goes."""
    reply, span = _turn()
    shown = rl.record_stop_button(
        replace(reply, event_id="$reply"),
        event_id="$button",
        membership_current=True,
        now_ns=NOW,
    )
    assert shown.reply is not None
    waiting = rl.pause(
        shown.reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=shown.reply.revision, stage=WriteStage.EDIT),
        in_place=True,
        now_ns=NOW,
    )
    assert waiting.reply is not None
    restarted = rl.owner_lost(
        _held(waiting.reply),
        span,
        rl.OwnerLostFacts(active_generation="gen-3", sources_pending=True),
        now_ns=NOW,
    )
    assert restarted.outcome is Outcome.APPLIED
    assert restarted.reply is not None
    assert restarted.reply.state is ReplyState.PAUSED
    assert restarted.reply.approval_id == "approval-1"
    assert restarted.reply.current_span_id is None
    assert restarted.reply.stop_button_event_id is None
    assert restarted.reply.redaction_pending == ("$button",)
    assert _span_after(restarted, span.span_id).outcome is SpanOutcome.PAUSED
    assert restarted.effects == ()


def test_an_approval_failure_note_freezes_the_reply_against_a_later_stop() -> None:
    """The note is the reply's terminal write: a Stop after it is satisfied, and the finish changes nothing."""
    reply, span, transition = _paused()
    ended = _span_after(transition, span.span_id)
    note = rl.approval_failure_note(
        reply,
        ended,
        approval_id="approval-1",
        shown="failed",
        state=ReplyState.FAILED,
        prepared_revision=reply.revision,
        span_has_final=False,
        now_ns=NOW,
    )
    assert note.reply is not None
    assert note.reply.state is ReplyState.FAILED
    stop = rl.stop(note.reply, ended, StopFacts(receipt_order=9, span_live=False), now_ns=NOW)
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.FAILED
    assert not stop.effects
    finished = rl.approval_settled(
        stop.reply,
        ended,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="failed",
        answers_turn=True,
        now_ns=NOW,
    )
    # The note already ended the reply, so the finish only settles the sources.
    assert finished.outcome is Outcome.DUPLICATE
    assert finished.reply == stop.reply


def test_a_later_stop_on_a_cancelled_resume_still_reaches_its_approval() -> None:
    """A resume already cancelled leaves its reply to the approval; a newer Stop fences it again, settling nothing."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1", event_id="$reply")
    resume = rl.claim(_request("span-2", approval_id="approval-1"), _context(reply, span))
    assert resume.reply is not None
    assert resume.claimed is not None
    first = rl.stop(
        resume.reply,
        resume.claimed,
        StopFacts(receipt_order=4, span_live=True),
        now_ns=NOW,
    )
    assert first.reply is not None
    cancelled = rl.stopped(first.reply, resume.claimed, None, now_ns=NOW)
    assert cancelled.reply is not None
    ended = _span_after(cancelled, resume.claimed.span_id)
    again = rl.stop(cancelled.reply, ended, StopFacts(receipt_order=6, span_live=False), now_ns=NOW)
    assert again.effects == (FenceApproval("approval-1", "cancelled_by_user"), WakeApproval("approval-1"))
    assert again.reply is not None
    assert again.reply.state is ReplyState.ACTIVE


def test_a_reply_shows_one_stop_button() -> None:
    """A second button recorded on a running reply queues the first for removal; a reply that ended removes it."""
    reply, _span = _turn()
    first = rl.record_stop_button(reply, event_id="$button-1", membership_current=True, now_ns=NOW)
    assert first.reply is not None
    second = rl.record_stop_button(first.reply, event_id="$button-2", membership_current=True, now_ns=NOW)
    assert second.reply is not None
    assert second.reply.stop_button_event_id == "$button-2"
    assert second.reply.redaction_pending == ("$button-1",)
    ended = rl.record_stop_button(
        replace(reply, state=ReplyState.COMPLETED),
        event_id="$button-3",
        membership_current=True,
        now_ns=NOW,
    )
    assert ended.reply is not None
    assert ended.reply.stop_button_event_id is None
    assert ended.reply.redaction_pending == ("$button-3",)


def test_permanently_failed_pause_row_fences_the_approval() -> None:
    """A pause nobody saw fails the reply like a failed approval handoff."""
    reply, span, transition = _paused()
    assert transition.row is not None
    ended = _span_after(transition, span.span_id)
    failed = rl.write_failed(
        reply,
        ended,
        rl.FailedWrite(
            WriteFacts(
                WriteStage.EDIT,
                transition.row.sequence,
                span.span_id,
                creates_event=False,
                placeholder_only=False,
            ),
            "refused",
        ),
        now_ns=NOW,
    )
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.FAILED
    assert failed.reply.owed_write is not None
    assert failed.reply.owed_write.note == rl._NOTE_APPROVAL_FAILED
    assert failed.effects == (FenceApproval("approval-1", "failed"), WakeApproval("approval-1"))


def test_approval_settlement_guards() -> None:
    """Approval events from another approval are stale, and so are those of an approval that no longer holds the reply."""
    reply, _span, _transition = _paused()
    other = rl.approval_settled(
        reply,
        None,
        approval_id="other",
        paused_span_id="span-1",
        result="failed",
        disposition="failed",
        answers_turn=True,
        now_ns=NOW,
    )
    assert other.outcome is Outcome.STALE
    released = rl.approval_settled(
        _held(reply, None),
        None,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="failed",
        answers_turn=True,
        now_ns=NOW,
    )
    assert released.outcome is Outcome.STALE
    # A finish settles what its pause held whatever the reply does.
    assert other.effects == released.effects == (SettleSources("span-1"),)
    failed = rl.approval_settled(
        reply,
        None,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="failed",
        answers_turn=True,
        now_ns=NOW,
    )
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.FAILED


def test_approval_released_ends_the_resume_for_replay() -> None:
    """A continuation released to replay leaves its sources pending and the reply active."""
    reply, span, _transition = _paused()
    claim = rl.claim(
        _request("span-2", approval_id="approval-1"),
        _context(reply, replace(span, outcome=SpanOutcome.PAUSED)),
    )
    assert claim.reply is not None
    assert claim.claimed is not None
    released = rl.approval_released(claim.reply, claim.claimed, now_ns=NOW)
    assert released.reply is not None
    assert released.reply.state is ReplyState.ACTIVE
    assert _span_after(released, "span-2").outcome is SpanOutcome.RELEASED


# --- stop ------------------------------------------------------------------


def test_stop_without_a_live_span_cancels_directly_and_owes_a_note() -> None:
    """An ownerless Stop settles the last span and owes the cancel note."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.RELEASED)
    stop = rl.stop(reply, None, StopFacts(receipt_order=6, span_live=False), now_ns=NOW)
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.CANCELLED
    assert stop.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_CANCELLED)
    assert stop.effects == (SettleSources(span.span_id),)
    flushed = rl.flush_owed_write(
        stop.reply,
        span,
        shown="cancelled",
        prepared_revision=stop.reply.revision,
        span_has_final=False,
        now_ns=NOW,
    )
    assert flushed.row is not None
    assert flushed.row.stage is WriteStage.FINAL
    assert flushed.reply is not None
    assert flushed.reply.owed_write is None


def test_stop_guards() -> None:
    """Older Stops are duplicates; a terminal reply keeps its answer."""
    reply, span = _turn()
    first = rl.stop(reply, span, StopFacts(5, span_live=True), now_ns=NOW)
    assert first.reply is not None
    assert rl.stop(first.reply, span, StopFacts(4, span_live=True), now_ns=NOW).outcome is Outcome.DUPLICATE
    completed = replace(reply, state=ReplyState.COMPLETED, current_span_id=None)
    terminal = rl.stop(completed, None, StopFacts(9, span_live=False), now_ns=NOW)
    assert terminal.reply is not None
    assert terminal.reply.state is ReplyState.COMPLETED
    assert not terminal.reply.unapplied_stop


def test_stop_button_is_redacted_when_the_reply_leaves_active() -> None:
    """I8: leaving active queues the button's redaction."""
    reply, span = _turn()
    with_button = rl.record_stop_button(reply, event_id="$button", membership_current=True, now_ns=NOW)
    assert with_button.reply is not None
    finished = rl.finish(with_button.reply, span, _write(with_button.reply, ReplyState.COMPLETED), now_ns=NOW)
    assert finished.reply is not None
    assert finished.reply.redaction_pending == ("$button",)
    assert finished.reply.stop_button_event_id is None
    late = rl.record_stop_button(finished.reply, event_id="$late-button", membership_current=True, now_ns=NOW)
    assert late.reply is not None
    assert "$late-button" in late.reply.redaction_pending


# --- deletion, departure, settlement, startup -----------------------------


def test_deleting_every_source_cancels_the_running_span() -> None:
    """Decision 4: the live span is cancelled and its reply removed."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    transition = rl.sources_deleted(reply, span, now_ns=NOW)
    assert CancelSpan(span.span_id) in transition.effects
    # The deleted sources settle, and nothing answered their turn.
    assert SettleSources(span.span_id, answered=False) in transition.effects
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.GONE
    assert transition.reply.redaction_pending == ("$reply",)


def test_a_regeneration_moves_the_reply_to_the_membership_its_edit_was_admitted_in() -> None:
    """After the bot left and rejoined, the regeneration's rows belong to the membership the edit arrived in."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, event_id="$reply")
    regeneration = rl.claim(
        _request("span-2", delivery_id="$edit", driving_edit_id="$edit", membership_epoch=2),
        _context(reply, span),
    )
    assert regeneration.reply is not None
    assert regeneration.reply.membership_epoch == 2


def test_deleting_sources_during_a_regeneration_keeps_the_earlier_answer() -> None:
    """Before the regeneration wrote anything, the answer the edit was replacing stands; afterwards the reply goes."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, presentation="answer", event_id="$reply")
    regeneration = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regeneration.reply is not None
    assert regeneration.claimed is not None
    kept = rl.sources_deleted(regeneration.reply, regeneration.claimed, now_ns=NOW)
    assert kept.reply is not None
    assert kept.reply.state is ReplyState.COMPLETED
    assert kept.reply.presentation == "answer"
    assert kept.reply.redaction_pending == ()
    assert CancelSpan("span-2") in kept.effects
    assert _span_after(kept, "span-2").outcome is SpanOutcome.RESTORED
    # Written ahead, so Matrix may show it though no confirmation says so yet.
    shown = replace(regeneration.reply, possibly_shown_seq=regeneration.reply.reply_sequence + 1)
    removed = rl.sources_deleted(shown, regeneration.claimed, now_ns=NOW)
    assert removed.reply is not None
    assert removed.reply.state is ReplyState.GONE
    # A restart that lost the regeneration before it wrote leaves it waiting for replay, still holding the answer.
    lost = rl.owner_lost(
        regeneration.reply,
        regeneration.claimed,
        rl.OwnerLostFacts(active_generation="gen-next", sources_pending=True),
        now_ns=NOW,
    )
    assert lost.reply is not None
    waiting = _span_after(lost, "span-2")
    assert waiting.outcome is SpanOutcome.LOST
    after_restart = rl.sources_deleted(lost.reply, waiting, now_ns=NOW)
    assert after_restart.reply is not None
    assert after_restart.reply.state is ReplyState.COMPLETED
    assert after_restart.reply.redaction_pending == ()
    # The answer stands, but nothing answers the deleted message's edit.
    assert after_restart.effects == (SettleSources("span-2", answered=False),)


def _regeneration_that_showed_partial_text() -> tuple[Reply, Span]:
    """Return a completed answer's regeneration after it wrote new text ahead, as a restart leaves it."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, presentation="answer", event_id="$reply")
    regeneration = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regeneration.reply is not None
    assert regeneration.claimed is not None
    wrote = rl.write_ahead(
        regeneration.reply,
        regeneration.claimed,
        shown="partial",
        previous=None,
        active_generation=GEN,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert wrote.reply is not None
    return wrote.reply, regeneration.claimed


def test_a_regeneration_a_restart_lost_after_it_wrote_keeps_what_it_showed() -> None:
    """Once a regeneration showed new text, neither its restart nor a re-run that writes nothing puts the old answer back."""
    reply, regeneration = _regeneration_that_showed_partial_text()
    settled = rl.owner_lost(
        reply,
        regeneration,
        rl.OwnerLostFacts(active_generation="gen-next", sources_pending=False),
        now_ns=NOW,
    )
    assert settled.reply is not None
    assert settled.reply.state is ReplyState.FAILED
    assert settled.reply.owed_write == rl.OwedWrite("span-2", rl._NOTE_RESTART)

    lost = rl.owner_lost(
        reply,
        regeneration,
        rl.OwnerLostFacts(active_generation="gen-next", sources_pending=True),
        now_ns=NOW,
    )
    assert lost.reply is not None
    waiting = _span_after(lost, "span-2")
    rerun = rl.claim(_request("span-3", delivery_id="$edit", driving_edit_id="$edit"), _context(lost.reply, waiting))
    assert rerun.reply is not None
    assert rerun.claimed is not None
    assert rerun.claimed.kind is SpanKind.REGENERATION
    assert rerun.claimed.rollback is None
    # A provider error before the re-run's first chunk leaves its sources for a retry.
    failed = rl.fail(rerun.reply, rerun.claimed, None, phase="pre_delivery", now_ns=NOW)
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.ACTIVE
    assert failed.effects == ()
    # A Stop ends it cancelled, below what the room shows.
    stop = rl.stop(rerun.reply, rerun.claimed, StopFacts(9, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    cancelled = rl.stopped(stop.reply, rerun.claimed, None, now_ns=NOW)
    assert cancelled.reply is not None
    assert cancelled.reply.state is ReplyState.CANCELLED
    assert cancelled.reply.owed_write == rl.OwedWrite("span-3", rl._NOTE_CANCELLED)


def test_deleting_sources_keeps_paused_and_completed_replies() -> None:
    """Replies an approval holds and finished answers survive their sources' deletion.

    That includes one whose resume already ended while its approval's
    settlement is still to come.
    """
    reply, _span, _transition = _paused()
    assert rl.sources_deleted(reply, None, now_ns=NOW).outcome is Outcome.DUPLICATE
    settling = replace(reply, state=ReplyState.ACTIVE, current_span_id=None)
    assert settling.approval_id is not None
    assert rl.sources_deleted(settling, None, now_ns=NOW).outcome is Outcome.DUPLICATE
    completed = replace(reply, state=ReplyState.COMPLETED)
    assert rl.sources_deleted(completed, None, now_ns=NOW).outcome is Outcome.DUPLICATE


def test_departure_ends_replies_without_touching_matrix() -> None:
    """A departed room's replies end gone, cancel their spans, and drop pending redactions."""
    reply, span = _turn()
    reply = replace(reply, redaction_pending=("$x",), stop_button_event_id="$button")
    transition = rl.departed(reply, span, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.GONE
    assert transition.reply.redaction_pending == ()
    assert transition.effects == (CancelSpan(span.span_id),)
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.RELEASED


def test_sources_settled_without_reply() -> None:
    """A source settled without an answer removes a placeholder and fails substantive content."""
    reply, span = _turn()
    gone = rl.sources_settled_without_reply(replace(reply, event_id="$reply", placeholder_only=True), span, now_ns=NOW)
    assert gone.reply is not None
    assert gone.reply.state is ReplyState.GONE
    assert gone.reply.redaction_pending == ("$reply",)
    failed = rl.sources_settled_without_reply(replace(reply, event_id="$reply"), span, now_ns=NOW)
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.FAILED
    # The in-progress status it shows ends with the interrupted note.
    assert failed.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_INTERRUPTED)


def test_sources_settled_without_reply_leave_a_held_reply_to_its_approval() -> None:
    """A resume a restart lost still has its approval, whose settlement ends the reply."""
    reply, span = _turn()
    reply, span = _ended(replace(reply, event_id="$reply", approval_id="approval-1"), span, SpanOutcome.LOST)
    held = rl.sources_settled_without_reply(reply, span, now_ns=NOW)
    assert held.outcome is Outcome.DUPLICATE
    assert held.reply == reply
    assert held.effects == ()


def _interrupted() -> tuple[Reply, Span]:
    """Return a reply whose span a restart lost while its sources stayed pending."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.LOST)
    return replace(reply, event_id="$reply"), span


def test_a_superseded_replay_ends_the_interrupted_reply_and_settles_its_sources() -> None:
    """The replay a newer message superseded settles its sources with the reply, in one transition."""
    reply, span = _interrupted()
    superseded = rl.replay_superseded(reply, span, durable_write_debt=False, now_ns=NOW)
    assert superseded.outcome is Outcome.APPLIED
    assert superseded.reply is not None
    assert superseded.reply.state is ReplyState.FAILED
    assert superseded.effects == (SettleSources(span.span_id, answered=False),)
    placeholder = rl.replay_superseded(
        replace(reply, placeholder_only=True),
        span,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert placeholder.reply is not None
    assert placeholder.reply.state is ReplyState.GONE
    assert placeholder.effects == (SettleSources(span.span_id, answered=False),)


def test_a_replay_whose_reply_owes_a_write_is_never_superseded() -> None:
    """The replay resolves what the reply owes Matrix, so a newer message does not supersede it."""
    reply, span = _interrupted()
    for owing in (
        rl.replay_superseded(reply, span, durable_write_debt=True, now_ns=NOW),
        rl.replay_superseded(
            replace(reply, owed_write=rl.OwedWrite(span.span_id, rl._NOTE_RESTART)),
            span,
            durable_write_debt=False,
            now_ns=NOW,
        ),
    ):
        assert owing.outcome is Outcome.DEFERRED
        assert owing.effects == ()
    answered = rl.replay_superseded(
        replace(reply, state=ReplyState.COMPLETED),
        span,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert answered.outcome is Outcome.DUPLICATE


def test_a_dropped_replay_ends_the_reply_its_earlier_span_left() -> None:
    """Ingress settled the replay's sources without a turn, so the reply ends interrupted; it owes no row first."""
    reply, span = _interrupted()
    owing = replace(reply, owed_write=rl.OwedWrite(span.span_id, rl._NOTE_RESTART))
    dropped = rl.replay_dropped(owing, span, sources_pending=False, now_ns=NOW)
    assert dropped.outcome is Outcome.APPLIED
    assert dropped.reply is not None
    assert dropped.reply.state is ReplyState.FAILED
    assert dropped.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_INTERRUPTED)
    # Settling again is idempotent, and it is what records the turn answered.
    assert dropped.effects == (SettleSources(span.span_id, answered=False),)
    # A placeholder is removed, unless an edit Matrix has not confirmed may show more.
    unconfirmed = replace(reply, placeholder_only=True, possibly_shown_seq=3, confirmed_seq=2)
    shown = rl.replay_dropped(unconfirmed, span, sources_pending=False, now_ns=NOW)
    assert shown.reply is not None
    assert shown.reply.state is ReplyState.FAILED
    placeholder = rl.replay_dropped(replace(unconfirmed, confirmed_seq=3), span, sources_pending=False, now_ns=NOW)
    assert placeholder.reply is not None
    assert placeholder.reply.state is ReplyState.GONE
    kept = (
        rl.replay_dropped(reply, span, sources_pending=True, now_ns=NOW),
        rl.replay_dropped(replace(reply, approval_id="approval-1"), span, sources_pending=False, now_ns=NOW),
        rl.replay_dropped(replace(reply, current_span_id="span-2"), span, sources_pending=False, now_ns=NOW),
    )
    assert [transition.outcome for transition in kept] == [Outcome.DEFERRED] * 3
    answered = rl.replay_dropped(replace(reply, state=ReplyState.COMPLETED), span, sources_pending=False, now_ns=NOW)
    assert answered.outcome is Outcome.DUPLICATE


def test_owner_lost_marks_pending_work_for_replay() -> None:
    """A reply an older instance left running is lost and replayed when its sources are pending."""
    reply, span = _turn()
    stale = replace(span, bot_generation=OLD_GEN)
    transition = rl.owner_lost(reply, stale, rl.OwnerLostFacts(active_generation=GEN, sources_pending=True), now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.ACTIVE
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.LOST


def test_owner_lost_fails_settled_orphans_with_a_restart_note() -> None:
    """An orphan whose sources settled ends failed and owes the restart note; one that never wrote anything goes."""
    reply, span = _turn()
    stale = replace(span, bot_generation=OLD_GEN)
    facts = rl.OwnerLostFacts(active_generation=GEN, sources_pending=False)
    transition = rl.owner_lost(replace(reply, event_id="$reply"), stale, facts, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_RESTART)
    silent = rl.owner_lost(reply, stale, facts, now_ns=NOW)
    assert silent.reply is not None
    assert silent.reply.state is ReplyState.GONE
    assert silent.reply.owed_write is None


def test_owner_lost_applies_an_unapplied_stop() -> None:
    """A Stop the old instance never applied cancels the reply at startup."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(2, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    stale = replace(span, bot_generation=OLD_GEN)
    transition = rl.owner_lost(
        stop.reply,
        stale,
        rl.OwnerLostFacts(active_generation=GEN, sources_pending=True),
        now_ns=NOW,
    )
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.CANCELLED
    assert SettleSources(span.span_id) in transition.effects


def test_owner_lost_leaves_approval_resumes_to_approval_recovery() -> None:
    """Main's approval recovery still owns an interrupted resume."""
    reply, span = _turn()
    resume = replace(span, kind=SpanKind.APPROVAL_RESUME, bot_generation=OLD_GEN)
    transition = rl.owner_lost(
        reply,
        resume,
        rl.OwnerLostFacts(active_generation=GEN, sources_pending=True),
        now_ns=NOW,
    )
    assert transition.outcome is Outcome.DUPLICATE


def test_a_refused_terminal_row_owes_the_delivery_failed_note() -> None:
    """However Matrix refused the answer, on a placeholder or as the reply's only message, the user is told."""
    reply, span = _turn()
    for refused in (replace(reply, event_id="$reply", placeholder_only=True), reply):
        failed = rl._terminal_write_failed(refused, span, now_ns=NOW)
        assert failed.reply is not None
        assert failed.reply.state is ReplyState.FAILED
        assert failed.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_DELIVERY_FAILED)
        assert failed.reply.redaction_pending == ()


def test_dispatch_failure_fails_the_reply_and_owes_its_error() -> None:
    """A dispatch failure after a claim ends the span and owes the error notice."""
    reply, span = _turn()
    transition = rl.dispatch_failed(reply, span, error_text="boom", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_ERROR, "boom")
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.FAILED


def test_removed_entity_fails_without_writing() -> None:
    """An entity removed from the configuration leaves its messages as they are, and its turn unanswered."""
    reply, span = _turn()
    transition = rl.removed_entity(reply, span, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write is None
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.LOST
    assert transition.effects == (SettleSources(span.span_id, answered=False),)
    # A retry's sources wait on the last span; they settle the same way.
    released = rl.release(reply, span, now_ns=NOW)
    assert released.reply is not None
    waiting = rl.removed_entity(released.reply, _span_after(released, span.span_id), now_ns=NOW)
    assert waiting.effects == (SettleSources(span.span_id, answered=False),)


def test_removed_entity_leaves_an_ended_reply_as_it_is() -> None:
    """An ended reply keeps what it owes Matrix, which its entity's bot writes if the entity comes back."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.FAILED)
    owing = replace(
        reply,
        state=ReplyState.FAILED,
        redaction_pending=("$button",),
        owed_write=rl.OwedWrite(span.span_id, "error"),
    )
    assert rl.removed_entity(owing, span, now_ns=NOW).outcome is Outcome.DUPLICATE


def test_removed_entity_leaves_a_held_reply_to_its_approval() -> None:
    """The discard ends it unanswered; an owner that comes back settles it with its note instead."""
    reply, span, transition = _paused()
    removed = rl.removed_entity(reply, _span_after(transition, span.span_id), now_ns=NOW)
    assert removed.reply is not None
    assert removed.reply.state is ReplyState.PAUSED
    assert removed.effects == ()
    discarded = rl.approval_settled(
        removed.reply,
        _span_after(transition, span.span_id),
        approval_id="approval-1",
        paused_span_id=span.span_id,
        result="failed",
        disposition="failed",
        answers_turn=False,
        now_ns=NOW,
    )
    assert discarded.reply is not None
    assert discarded.reply.state is ReplyState.FAILED
    assert discarded.effects == (SettleSources(span.span_id, answered=False),)
    # An owner that comes back first writes the approval's note and finishes it with its turn answered.
    paused = _span_after(transition, span.span_id)
    noted = rl.approval_failure_note(
        removed.reply,
        paused,
        approval_id="approval-1",
        shown="approval failed",
        state=ReplyState.FAILED,
        prepared_revision=removed.reply.revision,
        span_has_final=False,
        now_ns=NOW,
    )
    assert noted.reply is not None
    assert noted.row is not None
    assert noted.row.stage is WriteStage.FINAL
    finished = rl.approval_settled(
        noted.reply,
        paused,
        approval_id="approval-1",
        paused_span_id=span.span_id,
        result="failed",
        disposition="failed",
        answers_turn=True,
        now_ns=NOW,
    )
    assert finished.effects == (SettleSources(span.span_id),)


def test_a_restart_leaves_a_span_approved_in_place_to_its_approval() -> None:
    """A Stop fenced the approval and the process died: approval recovery, not the restart, settles the reply."""
    reply, span, _transition = _paused(in_place=True)
    approved = rl.resumed_in_place(reply, span, approval_id="approval-1", now_ns=NOW)
    assert approved.reply is not None
    stop = rl.stop(approved.reply, span, StopFacts(receipt_order=8, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    restarted = rl.owner_lost(
        stop.reply,
        span,
        rl.OwnerLostFacts(active_generation="gen-next", sources_pending=True),
        now_ns=NOW,
    )
    assert restarted.reply is not None
    assert restarted.reply.state is ReplyState.ACTIVE
    assert restarted.reply.unapplied_stop
    assert restarted.effects == ()
    assert _span_after(restarted, span.span_id).outcome is SpanOutcome.LOST


def test_releasing_an_approval_whose_in_place_span_a_restart_ended_leaves_the_reply_to_the_replay() -> None:
    """No span runs for it any more: the reply stays active with its sources pending, and the replay answers."""
    reply, span, _transition = _paused(in_place=True)
    approved = rl.resumed_in_place(reply, span, approval_id="approval-1", now_ns=NOW)
    assert approved.reply is not None
    restarted = rl.owner_lost(
        approved.reply,
        span,
        rl.OwnerLostFacts(active_generation="gen-next", sources_pending=True),
        now_ns=NOW,
    )
    assert restarted.reply is not None
    assert _span_after(restarted, span.span_id).outcome is SpanOutcome.LOST

    released = rl.approval_released(restarted.reply, None, now_ns=NOW)

    assert released.outcome is Outcome.APPLIED
    assert released.reply is not None
    assert released.reply.state is ReplyState.ACTIVE
    assert released.reply.current_span_id is None
    assert released.spans == ()
    assert released.effects == ()


def test_releasing_an_approval_a_stop_covers_ends_the_reply_instead_of_replaying() -> None:
    """The Stop reached the reply after a shutdown interrupted its resume: the replay would run what the user stopped."""
    reply, span, _transition = _paused()
    resume = rl.claim(_request("resume", delivery_id="$source", approval_id="approval-1"), _context(reply, span))
    assert resume.reply is not None
    assert resume.claimed is not None
    stop = rl.stop(
        resume.reply,
        resume.claimed,
        StopFacts(receipt_order=8, span_live=False),
        now_ns=NOW,
    )
    assert stop.reply is not None
    released = rl.approval_released(stop.reply, resume.claimed, now_ns=NOW)
    assert released.reply is not None
    assert released.reply.state is ReplyState.CANCELLED
    assert released.reply.owed_write == rl.OwedWrite("resume", rl._NOTE_CANCELLED)
    assert released.effects == (SettleSources("resume"),)
    assert _span_after(released, "resume").outcome is SpanOutcome.CANCELLED


def test_an_approval_settles_its_turn_unanswered_when_nothing_answers_it() -> None:
    """Deleted sources or a discarded owner settle the paused span's sources without answering their turn."""
    reply, span, transition = _paused()
    paused = _span_after(transition, span.span_id)
    for answers_turn in (True, False):
        settled = rl.approval_settled(
            reply,
            paused,
            approval_id="approval-1",
            paused_span_id=span.span_id,
            result="failed",
            disposition="failed",
            answers_turn=answers_turn,
            now_ns=NOW,
        )
        assert settled.effects[0] == SettleSources(span.span_id, answered=answers_turn)


def test_span_outcome_is_written_once() -> None:
    """Ending a span twice keeps its first outcome."""
    _reply, span = _turn()
    ended = replace(span, outcome=SpanOutcome.COMPLETED, ended_at_ns=NOW)
    assert rl._end(ended, SpanOutcome.FAILED, NOW + 1) == ended


# --- regressions from review ----------------------------------------------


def test_progress_confirmation_clears_placeholder_only_before_a_failed_final() -> None:
    """A placeholder later replaced by real progress is substantive content, not a placeholder."""
    reply, span = _turn()
    initial = rl.enqueue_initial(
        reply,
        span,
        shown="ph",
        placeholder_only=True,
        prepared_revision=reply.revision,
        now_ns=NOW,
    )
    assert initial.reply is not None
    assert initial.row is not None
    acked = rl.write_acknowledged(
        initial.reply,
        WriteFacts(WriteStage.INITIAL, initial.row.sequence, span.span_id, creates_event=True, placeholder_only=True),
        event_id="$reply",
        membership_current=True,
        now_ns=NOW,
    )
    assert acked.reply is not None
    progress = rl.write_ahead(
        acked.reply,
        span,
        shown="p1",
        previous=None,
        active_generation=GEN,
        durable_write_debt=False,
        now_ns=NOW,
    )
    assert progress.reply is not None
    final = rl.finish(
        progress.reply,
        span,
        replace(
            _write(progress.reply, ReplyState.COMPLETED),
            confirms=rl.ProgressConfirmation(event_id="$reply", placeholder_only=False),
        ),
        now_ns=NOW,
    )
    assert final.reply is not None
    assert not final.reply.placeholder_only
    ended = _span_after(final, span.span_id)
    failed = rl._terminal_write_failed(final.reply, ended, now_ns=NOW)
    assert failed.reply is not None
    # The progress stays its event's content, never redacted as a placeholder; the note ends it.
    assert failed.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_DELIVERY_FAILED)
    assert failed.reply.state is ReplyState.FAILED
    assert failed.reply.redaction_pending == ()


def test_regeneration_replaces_a_frozen_display() -> None:
    """A regenerated answer is shown, not the old post-hook display; the rollback keeps the old one."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, frozen_display="old frozen")
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    assert regen.reply.frozen_display is None
    assert regen.claimed.rollback is not None
    assert regen.claimed.rollback.frozen_display == "old frozen"
    final = rl.finish(regen.reply, regen.claimed, _write(regen.reply, ReplyState.COMPLETED, "new"), now_ns=NOW)
    assert final.reply is not None
    assert final.reply.frozen_display is None
    assert final.reply.presentation == "new"


@pytest.mark.parametrize("stage", [WriteStage.EDIT, WriteStage.INITIAL])
def test_failed_pause_row_of_an_in_place_wait_fences_and_cancels_the_waiter(stage: WriteStage) -> None:
    """A refused pause of a response-local wait, or a pause that was the reply's create, fails the handoff."""
    reply, span = _turn()
    if stage is WriteStage.EDIT:
        reply = replace(reply, event_id="$reply")
    paused = rl.pause(
        reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=reply.revision, stage=stage),
        in_place=True,
        now_ns=NOW,
    )
    assert paused.reply is not None
    assert paused.row is not None
    failed = rl.write_failed(
        _held(paused.reply),
        span,
        rl.FailedWrite(
            WriteFacts(
                stage,
                paused.row.sequence,
                span.span_id,
                creates_event=stage is WriteStage.INITIAL,
                placeholder_only=False,
            ),
            "refused",
        ),
        now_ns=NOW,
    )
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.FAILED
    assert failed.reply.current_span_id is None
    assert FenceApproval("approval-1", "failed") in failed.effects
    assert CancelSpan(span.span_id) in failed.effects
    assert _span_after(failed, span.span_id).outcome is SpanOutcome.FAILED


def test_late_create_after_departure_binds_without_redaction() -> None:
    """A create acknowledged after the room was left owes nothing to it."""
    reply, span = _turn()
    departed = rl.departed(reply, span, now_ns=NOW)
    assert departed.reply is not None
    acked = rl.write_acknowledged(
        departed.reply,
        WriteFacts(WriteStage.INITIAL, 1, span.span_id, creates_event=True, placeholder_only=True),
        event_id="$late",
        membership_current=False,
        now_ns=NOW,
    )
    assert acked.reply is not None
    assert acked.reply.event_id == "$late"
    assert acked.reply.redaction_pending == ()


def test_interactive_acknowledgement_records_its_create() -> None:
    """The acknowledgement's create is the reply's first sequenced write."""
    created = rl.interactive_acknowledgement(_request("ack-span"), shown="ack")
    assert created.row is not None
    assert created.row.stage is WriteStage.INITIAL
    assert created.reply is not None
    assert created.reply.possibly_shown == "ack"
    assert created.reply.possibly_shown_seq == created.row.sequence == 1


def test_stop_after_restart_during_an_approval_resume_fences_the_approval() -> None:
    """A resume an older bot instance left running belongs to approval recovery, not the direct Stop path."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1")
    resume = rl.claim(_request("span-2", approval_id="approval-1"), _context(reply, span))
    assert resume.reply is not None
    assert resume.claimed is not None
    stale_resume = replace(resume.claimed, bot_generation=OLD_GEN)
    stop = rl.stop(resume.reply, stale_resume, StopFacts(3, span_live=False), now_ns=NOW)
    assert stop.effects == (FenceApproval("approval-1", "cancelled_by_user"), WakeApproval("approval-1"))
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.ACTIVE
    released = rl.approval_released(replace(stop.reply, state=ReplyState.CANCELLED), stale_resume, now_ns=NOW)
    assert released.outcome is Outcome.DUPLICATE


def test_direct_stop_ends_a_span_nobody_runs() -> None:
    """A Stop on a selection not yet admitted, or on an older instance's span, ends that span."""
    created = rl.interactive_acknowledgement(_request("ack-span"), shown="ack")
    assert created.reply is not None
    ack = created.spans[0]
    stop = rl.stop(created.reply, ack, StopFacts(2, span_live=False), now_ns=NOW)
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.CANCELLED
    assert _span_after(stop, "ack-span").outcome is SpanOutcome.CANCELLED


def test_a_selection_whose_sources_settle_before_its_claim_removes_its_acknowledgement() -> None:
    """A selection that will never run ends its reply instead of leaving the acknowledgement showing."""
    created = rl.interactive_acknowledgement(_request("ack-span"), shown="ack")
    assert created.reply is not None
    assert created.row is not None
    ack = created.spans[0]
    acked = rl.write_acknowledged(
        created.reply,
        rl.WriteFacts(
            stage=WriteStage.INITIAL,
            sequence=created.row.sequence,
            span_id=ack.span_id,
            creates_event=True,
            placeholder_only=True,
        ),
        event_id="$ack",
        membership_current=True,
        now_ns=NOW,
    )
    assert acked.reply is not None
    for settled in (
        rl.sources_settled_without_reply(acked.reply, ack, now_ns=NOW),
        rl.replay_dropped(acked.reply, ack, sources_pending=False, now_ns=NOW),
    ):
        assert settled.reply is not None
        assert settled.reply.state is ReplyState.GONE
        assert settled.reply.redaction_pending == ("$ack",)
        assert _span_after(settled, "ack-span").outcome is SpanOutcome.SUPPRESSED
    # The selection's claim, arriving later, runs nothing.
    gone = rl.sources_settled_without_reply(acked.reply, ack, now_ns=NOW)
    assert gone.reply is not None
    ended = _span_after(gone, "ack-span")
    late = rl.claim(_request("ack-span"), _context(gone.reply, ended, interactive_span=ended))
    assert late.outcome is Outcome.DUPLICATE


def test_superseded_span_whose_sources_settle_ends_the_reply() -> None:
    """A rebuild that is then ignored leaves no reply waiting forever."""
    reply, span = _turn()
    reply, span = _ended(replace(reply, event_id="$reply"), span, SpanOutcome.SUPERSEDED)
    settled = rl.sources_settled_without_reply(reply, span, now_ns=NOW)
    assert settled.reply is not None
    assert settled.reply.state is ReplyState.FAILED
    orphan = rl.owner_lost(reply, span, rl.OwnerLostFacts(active_generation=GEN, sources_pending=False), now_ns=NOW)
    assert orphan.reply is not None
    assert orphan.reply.state is ReplyState.FAILED


def test_a_regeneration_whose_model_fails_before_it_shows_anything_is_retried() -> None:
    """An error before delivery returns the edit for a retry, which regenerates with the same rollback."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED, presentation="answer", event_id="$reply")
    regen = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert regen.reply is not None
    assert regen.claimed is not None
    failed = rl.fail(regen.reply, regen.claimed, None, phase="pre_delivery", now_ns=NOW)
    assert failed.reply is not None
    assert failed.effects == ()
    released = _span_after(failed, "span-2")
    assert released.outcome is SpanOutcome.RELEASED
    retry = rl.claim(_request("span-3", delivery_id="$edit", driving_edit_id="$edit"), _context(failed.reply, released))
    assert retry.claimed is not None
    assert retry.claimed.kind is SpanKind.REGENERATION
    assert retry.claimed.rollback == regen.claimed.rollback
    # A retry that never comes puts the earlier answer back.
    dropped = rl.replay_dropped(failed.reply, released, sources_pending=False, now_ns=NOW)
    assert dropped.reply is not None
    assert dropped.reply.state is ReplyState.COMPLETED
    assert dropped.reply.presentation == "answer"


def test_approval_failure_after_an_applied_stop_is_a_failure() -> None:
    """Only a Stop still to apply, or a user's cancellation, makes a failed approval read as cancelled."""
    reply, _span, _transition = _paused()
    stopped_before = replace(reply, stop_receipt_order=3, stop_applied_receipt_order=3)
    failed = rl.approval_settled(
        stopped_before,
        None,
        approval_id="approval-1",
        paused_span_id="span-1",
        result="failed",
        disposition="failed",
        answers_turn=True,
        now_ns=NOW,
    )
    assert failed.reply is not None
    assert failed.reply.state is ReplyState.FAILED


# --- unmodeled events ------------------------------------------------------


def test_an_unmodeled_event_ends_the_reply_failed_and_settles_its_sources() -> None:
    """Nothing raises: the reply ends failed with the error note, its span cancelled, its sources settled once."""
    reply, span = _turn()
    transition = rl._unmodeled(reply, span, reason="test", now_ns=NOW)
    assert transition.unmodeled == "test"
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl._NOTE_ERROR)
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.FAILED
    assert transition.effects == (CancelSpan(span.span_id), SettleSources(span.span_id))


def test_an_unmodeled_event_on_a_held_reply_fails_its_approval() -> None:
    """The approval's failure settlement settles the sources it holds."""
    reply, _span, transition = _paused()
    ended = rl._unmodeled(reply, _span_after(transition, "span-1"), reason="test", now_ns=NOW)
    assert FenceApproval("approval-1", "failed") in ended.effects
    assert WakeApproval("approval-1") in ended.effects
    assert not any(isinstance(effect, SettleSources) for effect in ended.effects)


def test_an_unmodeled_event_on_an_ended_reply_changes_nothing() -> None:
    """A reply that already ended stays as it ended."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    ended = replace(reply, state=ReplyState.COMPLETED)
    transition = rl._unmodeled(ended, span, reason="test", now_ns=NOW)
    assert transition.outcome is Outcome.DUPLICATE
    assert transition.reply == ended
    assert transition.unmodeled == "test"
