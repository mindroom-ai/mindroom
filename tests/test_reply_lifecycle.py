"""Pure reply lifecycle rules, one test per rule and guard (DESIGN.md §6.4)."""

from __future__ import annotations

from dataclasses import replace

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
        requester_id="@user",
        visibility_policy=rl.VisibilityPolicy.NORMAL,
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


def test_claim_with_a_live_span_is_invalid() -> None:
    """Claims run under the conversation lock, so a live current span is a bug."""
    reply, span = _turn()
    with pytest.raises(rl.InvalidTransitionError):
        rl.claim(_request("span-2"), _context(reply, span))


def test_claim_on_a_terminal_reply_without_an_edit_is_invalid() -> None:
    """Only a regeneration claims a reply that already ended."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.COMPLETED)
    reply = replace(reply, state=ReplyState.COMPLETED)
    with pytest.raises(rl.InvalidTransitionError):
        rl.claim(_request("span-2"), _context(reply, span))


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


def test_regeneration_settles_an_older_stop() -> None:
    """An edit newer than a Stop applies it as settled."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.CANCELLED)
    reply = replace(reply, state=ReplyState.CANCELLED, stop_receipt_order=5)
    transition = rl.claim(
        _request("span-2", delivery_id="$edit", driving_edit_id="$edit"),
        _context(reply, span, edit_receipt_order=7),
    )
    assert transition.reply is not None
    assert transition.reply.stop_applied_receipt_order == 5


def test_regeneration_of_a_paused_reply_supersedes_its_approval() -> None:
    """Decision 1: the claim fences the approval for cleanup that publishes nothing."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1")
    transition = rl.claim(_request("span-2", delivery_id="$edit", driving_edit_id="$edit"), _context(reply, span))
    assert FenceApproval("approval-1", "superseded") in transition.effects
    assert WakeApproval("approval-1") in transition.effects
    assert transition.reply is not None
    assert transition.reply.approval_id is None
    assert transition.reply.state is ReplyState.ACTIVE


def test_regeneration_of_a_gone_reply_creates_a_new_one() -> None:
    """A detached reply is not reused; the edit gets a fresh reply."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.SUPPRESSED)
    reply = replace(reply, state=ReplyState.GONE)
    transition = rl.claim(
        _request("span-2", delivery_id="$edit", driving_edit_id="$edit", new_reply_id="reply-2"),
        _context(reply, span),
    )
    assert transition.reply is not None
    assert transition.reply.reply_id == "reply-2"


def test_regeneration_without_a_record_adopts_the_historical_reply() -> None:
    """A reply older than the records is regenerated with an unknown presentation."""
    transition = rl.claim(
        _request(delivery_id="$edit", driving_edit_id="$edit", historical_event_id="$answer"),
        _context(),
    )
    assert transition.reply is not None
    assert transition.reply.event_id == "$answer"
    claimed = transition.claimed
    assert claimed is not None
    assert claimed.rollback is not None
    assert not claimed.rollback.presentation_known
    assert claimed.rollback.state is ReplyState.COMPLETED


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


def test_approval_resume_claims_the_paused_reply() -> None:
    """A resume continues the paused reply as the current span."""
    reply, span = _turn()
    reply, span = _ended(reply, span, SpanOutcome.PAUSED)
    reply = replace(reply, state=ReplyState.PAUSED, approval_id="approval-1")
    transition = rl.claim(_request("span-2", approval_id="approval-1", approval_generation=3), _context(reply, span))
    assert transition.claimed is not None
    assert transition.claimed.kind is SpanKind.APPROVAL_RESUME
    assert transition.claimed.approval_generation == 3
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
    created = rl.interactive_acknowledgement(_request("ack-span"))
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


# --- writes ----------------------------------------------------------------


def test_write_ahead_allocates_the_next_sequence_and_confirms_the_previous_edit() -> None:
    """Each progress edit is recorded before it is sent and confirmed by the next one."""
    reply, span = _turn()
    first = rl.write_ahead(
        reply,
        span,
        shown="p1",
        previous_ok=False,
        previous_event_id=None,
        active_generation=GEN,
        now_ns=NOW,
    )
    assert first.reply is not None
    assert first.reply.possibly_shown_seq == 1
    assert not first.reply.confirmed
    second = rl.write_ahead(
        first.reply,
        span,
        shown="p2",
        previous_ok=True,
        previous_event_id="$reply",
        active_generation=GEN,
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
        previous_ok=False,
        previous_event_id=None,
        active_generation="gen-3",
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
        now_ns=NOW,
    )
    assert acked.reply is not None
    assert acked.reply.event_id == "$reply"
    assert acked.reply.placeholder_only
    assert acked.reply.confirmed
    with pytest.raises(rl.InvalidTransitionError):
        rl.write_acknowledged(
            acked.reply,
            WriteFacts(WriteStage.INITIAL, 1, span.span_id, creates_event=True, placeholder_only=True),
            event_id="$other",
            now_ns=NOW,
        )


def test_late_create_of_a_gone_reply_is_queued_for_redaction() -> None:
    """An event created after the reply was given up is removed."""
    reply, span = _turn()
    gone = replace(reply, state=ReplyState.GONE, current_span_id=None)
    acked = rl.write_acknowledged(
        gone,
        WriteFacts(WriteStage.INITIAL, 1, span.span_id, creates_event=True, placeholder_only=True),
        event_id="$late",
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
    assert transition.row.settles_sources
    assert SettleSources(span.span_id) in transition.effects
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.COMPLETED


def test_finish_with_a_stop_committed_meanwhile_recomputes() -> None:
    """A payload rendered before a Stop committed is refused; the re-render is cancelled."""
    reply, span = _turn()
    write = _write(reply, ReplyState.COMPLETED)
    stopped = rl.stop(reply, span, StopFacts(receipt_order=9, newer_edit=False, span_live=True), now_ns=NOW)
    assert stopped.reply is not None
    assert rl.finish(stopped.reply, span, write, now_ns=NOW).outcome is Outcome.RECOMPUTE
    assert rl.expected_terminal_state(stopped.reply, ReplyState.COMPLETED) is ReplyState.CANCELLED
    final = rl.finish(stopped.reply, span, _write(stopped.reply, ReplyState.CANCELLED), now_ns=NOW)
    assert final.reply is not None
    assert final.reply.state is ReplyState.CANCELLED
    assert not final.reply.unapplied_stop


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
    resume = replace(span, kind=SpanKind.APPROVAL_RESUME, approval_id="approval-1")
    transition = rl.finish(reply, resume, _write(reply, ReplyState.COMPLETED), now_ns=NOW)
    assert transition.row is not None
    assert not transition.row.settles_sources
    assert transition.effects == ()
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED


def test_stopped_cancels_with_the_shown_content() -> None:
    """A Stop reaching the running span ends it cancelled with a terminal row."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(receipt_order=3, newer_edit=False, span_live=True), now_ns=NOW)
    assert stop.effects == (CancelSpan(span.span_id),)
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
    stop = rl.stop(regen.reply, regen.claimed, StopFacts(receipt_order=8, newer_edit=False, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    transition = rl.stopped(stop.reply, regen.claimed, None, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.COMPLETED
    assert transition.reply.presentation == "old"
    assert transition.row is None
    assert _span_after(transition, "span-2").outcome is SpanOutcome.RESTORED


def test_stopped_without_a_recorded_stop_is_invalid() -> None:
    """A span cannot report a Stop its reply never recorded."""
    reply, span = _turn()
    with pytest.raises(rl.InvalidTransitionError):
        rl.stopped(reply, span, _write(reply, ReplyState.CANCELLED), now_ns=NOW)


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
    assert not transition.row.settles_sources
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
    stop = rl.stop(reply, span, StopFacts(receipt_order=2, newer_edit=False, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    assert rl.fail(stop.reply, span, None, phase="delivery", now_ns=NOW).outcome is Outcome.RECOMPUTE
    cancelled = rl.fail(stop.reply, span, _write(stop.reply, ReplyState.CANCELLED), phase="delivery", now_ns=NOW)
    assert cancelled.reply is not None
    assert cancelled.reply.state is ReplyState.CANCELLED


@pytest.mark.parametrize(
    ("event_id", "placeholder_only", "expected_state", "redacted"),
    [
        (None, False, ReplyState.GONE, ()),
        ("$reply", True, ReplyState.GONE, ("$reply",)),
        ("$reply", False, ReplyState.CANCELLED, ()),
    ],
)
def test_suppression_follows_mains_branches(
    event_id: str | None,
    placeholder_only: bool,
    expected_state: ReplyState,
    redacted: tuple[str, ...],
) -> None:
    """A suppressed answer redacts a placeholder, sends nothing new, and keeps substantive content."""
    reply, span = _turn()
    reply = replace(reply, event_id=event_id, placeholder_only=placeholder_only)
    transition = rl.suppress(reply, span, reason="suppressed", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is expected_state
    assert transition.reply.redaction_pending == redacted


def test_hook_failure_of_substantive_content_fails_the_reply() -> None:
    """A before-response hook exception leaves shown content and fails the reply."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    transition = rl.suppress(reply, span, reason="hook_failed", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.FAILED


def test_silent_schedule_hook_failure_writes_a_notice() -> None:
    """A silent schedule with no visible event reports its hook failure durably."""
    reply, span = _turn()
    transition = rl.suppress(
        reply,
        span,
        reason="hook_failed",
        silent_notice=_write(reply, ReplyState.FAILED),
        now_ns=NOW,
    )
    assert transition.row is not None
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED


def test_release_and_superseded_keep_sources_pending() -> None:
    """Shutdown and retries leave the reply active with pending sources."""
    reply, span = _turn()
    for outcome in (SpanOutcome.RELEASED, SpanOutcome.SUPERSEDED):
        transition = rl.release(reply, span, outcome=outcome, now_ns=NOW)
        assert transition.reply is not None
        assert transition.reply.state is ReplyState.ACTIVE
        assert transition.effects == ()
    with pytest.raises(rl.InvalidTransitionError):
        rl.release(reply, span, outcome=SpanOutcome.COMPLETED, now_ns=NOW)


# --- pause and approvals --------------------------------------------------


def _paused(*, in_place: bool = False) -> tuple[Reply, Span, rl.Transition]:
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    transition = rl.pause(
        reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=reply.revision, stage=WriteStage.EDIT),
        approval_id="approval-1",
        in_place=in_place,
        now_ns=NOW,
    )
    assert transition.reply is not None
    return transition.reply, span, transition


def test_pause_ends_the_span_and_writes_an_edit_row() -> None:
    """A pause is a durable edit row; the reply waits for its approval."""
    reply, span, transition = _paused()
    assert reply.state is ReplyState.PAUSED
    assert reply.approval_id == "approval-1"
    assert reply.current_span_id is None
    assert transition.row is not None
    assert transition.row.stage is WriteStage.EDIT
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.PAUSED


def test_pause_in_place_keeps_the_span_current_and_resumes_in_place() -> None:
    """A response-local approval wait keeps the span; its decision returns the reply to active."""
    reply, span, transition = _paused(in_place=True)
    assert reply.current_span_id == span.span_id
    assert transition.spans == ()
    resumed = rl.resumed_in_place(reply, span, approval_id="approval-1", now_ns=NOW)
    assert resumed.reply is not None
    assert resumed.reply.state is ReplyState.ACTIVE


def test_pause_with_an_unapplied_stop_defers_to_the_stop_path() -> None:
    """A Stop recorded before the pause wins."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(receipt_order=1, newer_edit=False, span_live=True), now_ns=NOW)
    assert stop.reply is not None
    transition = rl.pause(
        stop.reply,
        span,
        rl.PauseWrite(shown="paused", prepared_revision=stop.reply.revision, stage=WriteStage.EDIT),
        approval_id="approval-1",
        in_place=False,
        now_ns=NOW,
    )
    assert transition.outcome is Outcome.RECOMPUTE


def test_stop_on_a_paused_reply_fences_and_wakes_its_approval() -> None:
    """A Stop on a paused reply requests approval failure; the settlement ends the reply."""
    reply, _span, _transition = _paused()
    stop = rl.stop(reply, None, StopFacts(receipt_order=4, newer_edit=False, span_live=False), now_ns=NOW)
    assert stop.effects == (FenceApproval("approval-1", "cancelled_by_user"), WakeApproval("approval-1"))
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.PAUSED
    settled = rl.approval_settled(
        stop.reply,
        None,
        approval_id="approval-1",
        result="failed",
        disposition="cancelled_by_user",
        now_ns=NOW,
    )
    assert settled.reply is not None
    assert settled.reply.state is ReplyState.CANCELLED


def test_stop_on_an_in_place_wait_also_cancels_the_waiting_span() -> None:
    """A response-local wait's span is cancelled and its cards expire through the approval wake."""
    reply, span, _transition = _paused(in_place=True)
    stop = rl.stop(reply, span, StopFacts(receipt_order=4, newer_edit=False, span_live=True), now_ns=NOW)
    assert CancelSpan(span.span_id) in stop.effects
    assert FenceApproval("approval-1", "cancelled_by_user") in stop.effects


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
    assert failed.reply.owed_write.note == rl.NOTE_APPROVAL_FAILED
    assert failed.effects == (FenceApproval("approval-1", "failed"),)


def test_approval_settlement_guards() -> None:
    """Approval events from another approval are stale; superseded failures never touch the reply."""
    reply, _span, _transition = _paused()
    assert (
        rl.approval_settled(reply, None, approval_id="other", result="failed", disposition="failed", now_ns=NOW).outcome
        is Outcome.STALE
    )
    superseded = rl.approval_settled(
        reply,
        None,
        approval_id="approval-1",
        result="failed",
        disposition="superseded",
        now_ns=NOW,
    )
    assert superseded.outcome is Outcome.DUPLICATE
    failed = rl.approval_settled(
        reply,
        None,
        approval_id="approval-1",
        result="failed",
        disposition="failed",
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
    stop = rl.stop(reply, None, StopFacts(receipt_order=6, newer_edit=False, span_live=False), now_ns=NOW)
    assert stop.reply is not None
    assert stop.reply.state is ReplyState.CANCELLED
    assert stop.reply.owed_write == rl.OwedWrite(span.span_id, rl.NOTE_CANCELLED)
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
    assert not flushed.row.settles_sources
    assert flushed.reply is not None
    assert flushed.reply.owed_write is None


def test_stop_guards() -> None:
    """Older Stops and Stops a newer edit superseded are duplicates; a terminal reply keeps its answer."""
    reply, span = _turn()
    assert rl.stop(reply, span, StopFacts(1, newer_edit=True, span_live=True), now_ns=NOW).outcome is Outcome.DUPLICATE
    first = rl.stop(reply, span, StopFacts(5, newer_edit=False, span_live=True), now_ns=NOW)
    assert first.reply is not None
    assert (
        rl.stop(first.reply, span, StopFacts(4, newer_edit=False, span_live=True), now_ns=NOW).outcome
        is Outcome.DUPLICATE
    )
    completed = replace(reply, state=ReplyState.COMPLETED, current_span_id=None)
    terminal = rl.stop(completed, None, StopFacts(9, newer_edit=False, span_live=False), now_ns=NOW)
    assert terminal.reply is not None
    assert terminal.reply.state is ReplyState.COMPLETED
    assert not terminal.reply.unapplied_stop


def test_stop_button_is_redacted_when_the_reply_leaves_active() -> None:
    """I8: leaving active queues the button's redaction."""
    reply, span = _turn()
    with_button = rl.record_stop_button(reply, event_id="$button", now_ns=NOW)
    assert with_button.reply is not None
    finished = rl.finish(with_button.reply, span, _write(with_button.reply, ReplyState.COMPLETED), now_ns=NOW)
    assert finished.reply is not None
    assert finished.reply.redaction_pending == ("$button",)
    assert finished.reply.stop_button_event_id is None
    late = rl.record_stop_button(finished.reply, event_id="$late-button", now_ns=NOW)
    assert late.reply is not None
    assert "$late-button" in late.reply.redaction_pending


# --- deletion, departure, settlement, startup -----------------------------


def test_deleting_every_source_cancels_the_running_span() -> None:
    """Decision 4: the live span is cancelled and its reply removed."""
    reply, span = _turn()
    reply = replace(reply, event_id="$reply")
    transition = rl.sources_deleted(reply, span, now_ns=NOW)
    assert CancelSpan(span.span_id) in transition.effects
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.GONE
    assert transition.reply.redaction_pending == ("$reply",)


def test_deleting_sources_keeps_paused_and_completed_replies() -> None:
    """Paused replies and finished answers survive their sources' deletion."""
    reply, _span, _transition = _paused()
    assert rl.sources_deleted(reply, None, now_ns=NOW).outcome is Outcome.DUPLICATE
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


def test_owner_lost_marks_pending_work_for_replay() -> None:
    """A reply an older instance left running is lost and replayed when its sources are pending."""
    reply, span = _turn()
    stale = replace(span, bot_generation=OLD_GEN)
    transition = rl.owner_lost(reply, stale, rl.OwnerLostFacts(active_generation=GEN, sources_pending=True), now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.ACTIVE
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.LOST


def test_owner_lost_fails_settled_orphans_with_a_restart_note() -> None:
    """An orphan whose sources settled ends failed and owes the restart note."""
    reply, span = _turn()
    stale = replace(span, bot_generation=OLD_GEN)
    transition = rl.owner_lost(
        reply,
        stale,
        rl.OwnerLostFacts(active_generation=GEN, sources_pending=False),
        now_ns=NOW,
    )
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl.NOTE_RESTART)


def test_owner_lost_applies_an_unapplied_stop() -> None:
    """A Stop the old instance never applied cancels the reply at startup."""
    reply, span = _turn()
    stop = rl.stop(reply, span, StopFacts(2, newer_edit=False, span_live=True), now_ns=NOW)
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


def test_terminal_write_failed_on_a_placeholder() -> None:
    """Delivery failures on a placeholder owe the retry note; other failures remove it."""
    reply, span = _turn()
    on_placeholder = replace(reply, event_id="$reply", placeholder_only=True)
    delivery = rl.terminal_write_failed(on_placeholder, span, reason="delivery_failed", first_create=False, now_ns=NOW)
    assert delivery.reply is not None
    assert delivery.reply.owed_write == rl.OwedWrite(span.span_id, rl.NOTE_DELIVERY_FAILED)
    other = rl.terminal_write_failed(on_placeholder, span, reason="too large", first_create=False, now_ns=NOW)
    assert other.reply is not None
    assert other.reply.state is ReplyState.GONE
    assert other.reply.redaction_pending == ("$reply",)
    first_create = rl.terminal_write_failed(reply, span, reason="x", first_create=True, now_ns=NOW)
    assert first_create.reply is not None
    assert first_create.reply.state is ReplyState.GONE


def test_dispatch_failure_fails_the_reply_and_owes_its_error() -> None:
    """A dispatch failure after a claim ends the span and owes the error notice."""
    reply, span = _turn()
    transition = rl.dispatch_failed(reply, span, error_text="boom", now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write == rl.OwedWrite(span.span_id, rl.NOTE_ERROR, "boom")
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.FAILED


def test_removed_entity_fails_without_writing() -> None:
    """An entity removed from the configuration leaves its messages as they are."""
    reply, span = _turn()
    transition = rl.removed_entity(reply, span, now_ns=NOW)
    assert transition.reply is not None
    assert transition.reply.state is ReplyState.FAILED
    assert transition.reply.owed_write is None
    assert _span_after(transition, span.span_id).outcome is SpanOutcome.LOST


def test_span_outcome_is_written_once() -> None:
    """Ending a span twice is a programming error."""
    _reply, span = _turn()
    ended = replace(span, outcome=SpanOutcome.COMPLETED)
    with pytest.raises(rl.InvalidTransitionError):
        rl._end(ended, SpanOutcome.FAILED, NOW)
