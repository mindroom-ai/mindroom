"""What a paused reply does next, per trigger and journal fact, end to end through each trigger's owner.

Each case builds a paused reply's journal state with the shared approval helpers, runs one trigger at the
collaborator seam that owns it, and compares the whole observable outcome: whether the paused reply is kept
and in which state, how it was fenced, whether a recovered final answer was finalized, which note the reply
shows, the reason its cards were expired with, and whether its source went back to replay.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.approval.recovery import ApprovalRecovery
from mindroom.cancellation import request_task_cancel
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalDecision,
    ApprovalFailure,
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    replies,
)
from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowRequest
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.reply_lifecycle import ApprovalFailureKind, NoteKind, ReplyState
from mindroom.reply_presentation import Presentation, Segment, encode_presentation
from mindroom.response_runner import DeliveryProgress
from tests.approval_continuation_helpers import (
    advance_continuation,
    approval_continuation,
    freeze_resume_final,
    resumed_approval,
)
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import unwrap_extracted_collaborator
from tests.reply_span_helpers import paused_for_approval
from tests.response_runner_helpers import _bot, _target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import ApprovalContinuation, PrincipalStore
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner

pytestmark = pytest.mark.asyncio

_PUBLICATION_INTERRUPTED = "Tool approval card publication was interrupted and denied safely."
_PUBLICATION_FAILED = ApprovalFailure(ApprovalFailureKind.ERROR, _PUBLICATION_INTERRUPTED)
_UNAUTHORIZED = "Current authorization no longer permits this tool approval continuation."
_CLI_ENDED = "CLI approval response ended before final delivery."
# The bot instance a restart replaced, which published or claimed the paused reply before it.
_PREVIOUS = "previous-runtime"
# Why Matrix refuses a final answer for good.
_REFUSAL = "matrix event exceeds the hard size limit"
_STOP = ApprovalFailure(ApprovalFailureKind.USER_STOP)
_RESTART = ApprovalFailure(ApprovalFailureKind.RESTART)
_INTERRUPTED = ApprovalFailure(ApprovalFailureKind.INTERRUPTED)
_SHUTDOWN = ApprovalFailure(ApprovalFailureKind.SHUTDOWN)
_HTTP_502 = ApprovalFailure(ApprovalFailureKind.ERROR, "The model provider returned HTTP 502.")


@dataclass(frozen=True)
class Outcome:
    """Everything a decider's step leaves behind that a user or the next decider can observe."""

    # The paused reply's continuation state afterwards, or ``None`` once it ended.
    continuation: str | None
    # The failure the continuation is fenced with, while it is kept failing.
    failure: ApprovalFailure | None = None
    # How many times the reply's response lifecycle was finalized (post-response effects and hooks), live or recovered.
    finalized: int = 0
    # The notes the settlement wrote below the reply, with the error note's text.
    notes: tuple[tuple[NoteKind, str | None], ...] = ()
    # The reason its cards were expired with, ``None`` when they were not.
    cards: str | None = None
    # Whether its source is pending again, for replay or a later decision.
    source_pending: bool = True
    # The paused reply's state afterwards.
    reply: ReplyState = ReplyState.PAUSED


@dataclass
class _Observer:
    notes: list[tuple[NoteKind, str | None]] = field(default_factory=list)
    expire: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=True))
    lifecycle: MagicMock = field(
        default_factory=lambda: MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome)),
    )
    wakes: list[tuple[str, ...]] = field(default_factory=list)


@contextmanager
def _observed(bot: AgentBot) -> Iterator[_Observer]:
    """Record the notes, card expiries, finalizations, and wakes a decider causes, keeping their real effects."""
    runner = _runner(bot)
    observer = _Observer()
    write_note = DeliveryGateway.write_approval_failure_note

    async def record_note(
        gateway: DeliveryGateway,
        event_id: str,
        *,
        approval_id: str,
        note: NoteKind,
        text: str,
        target: MessageTarget,
    ) -> bool:
        observer.notes.append((note, text if note is NoteKind.ERROR else None))
        return await write_note(gateway, event_id, approval_id=approval_id, note=note, text=text, target=target)

    with (
        patch.object(DeliveryGateway, "write_approval_failure_note", new=record_note),
        patch(
            "mindroom.approval.manager.get_approval_store",
            return_value=MagicMock(cards=None, expire_continuation_cards=observer.expire),
        ),
        patch.object(runner, "build_lifecycle", return_value=observer.lifecycle),
        patch.object(
            bot._journal_dispatcher,
            "retry_turn_sources",
            side_effect=lambda _room_id, sources: observer.wakes.append(sources),
        ),
    ):
        yield observer


def _new_bot(tmp_path: Path) -> AgentBot:
    """Return the replying bot, whose homeserver accepts every send."""
    bot = _bot(tmp_path)
    unique_room_send_responses(bot.client)
    return bot


def _runner(bot: AgentBot) -> ResponseRunner:
    return unwrap_extracted_collaborator(bot._response_runner)


def _store(bot: AgentBot) -> PrincipalStore:
    return _runner(bot).deps.replies.store


def _self(bot: AgentBot) -> str:
    return _runner(bot).deps.approval_runtime_generation


def _recovery_target() -> MessageTarget:
    return _target(thread_id="$thread", reply_to_event_id="$source")


async def _outcome(bot: AgentBot, observer: _Observer, approval_id: str = "approval") -> Outcome:
    store = _store(bot)
    current = await store.approvals.approval_continuation(approval_id)
    reply = await store.replies.for_event("$waiting")
    assert reply is not None
    return Outcome(
        continuation=None if current is None else current.state,
        failure=None if current is None else current.failure,
        finalized=observer.lifecycle.finalize.await_count,
        notes=tuple(observer.notes),
        cards=None if observer.expire.await_args is None else observer.expire.await_args.args[1],
        source_pending=await store.is_pending("$source"),
        reply=reply.state,
    )


# ---------------------------------------------------------------------------
# Building paused replies


async def _admit_source(store: PrincipalStore) -> None:
    await store.admit(
        InboundEvent(
            event_id="$source",
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1,
            source={"event_id": "$source", "content": {"body": "run it"}},
        ),
    )


async def _paused(bot: AgentBot, **changes: object) -> ApprovalContinuation:
    """Pause a reply for one continuation in ``changes``' state, as a response or a claim leaves it."""
    store = _store(bot)
    await _admit_source(store)
    continuation = await paused_for_approval(store, approval_continuation(**changes))
    assert continuation is not None
    return continuation


async def _claimed(bot: AgentBot, *, by: str) -> ApprovalContinuation:
    return await _paused(bot, state="claimed", runtime_generation=by)


async def _fence(bot: AgentBot, continuation: ApprovalContinuation, failure: ApprovalFailure) -> ApprovalContinuation:
    failing = await _store(bot).approvals.request_approval_failure(
        continuation.approval_id,
        failure,
        expected_state=continuation.state,
        expected_generation=continuation.generation,
        expected_runtime_generation=continuation.runtime_generation,
    )
    assert failing is not None
    return failing


async def _end_claim_span(bot: AgentBot, continuation: ApprovalContinuation) -> None:
    """End the claim's resume span failed without an answer, as a crash after its failure and before its fence."""
    store = _store(bot)
    assert continuation.claim_span_id is not None
    span = await store.replies.span(continuation.claim_span_id)
    assert span is not None
    reply = await store.replies.load(span.reply_id)
    assert reply is not None
    transition = rl.fail(reply, span, None, now_ns=time.time_ns())
    await store._backend.write(lambda transaction: replies.apply(transaction, store._principal_id, transition))


async def _freeze_final(bot: AgentBot, claimed: ApprovalContinuation, *, completed: bool) -> None:
    """Write the claim's FINAL reply row: its completed answer, or the cancelled ending a Stop gave it."""
    store = _store(bot)
    if completed:
        await freeze_resume_final(store, claimed, text="finished", payload={"body": "finished"})
        return
    assert claimed.claim_span_id is not None
    span = await store.replies.span(claimed.claim_span_id)
    assert span is not None
    shown = encode_presentation(Presentation(segments=(Segment(kind="answer", text="partial", span_id=span.span_id),)))

    def decide(reply: rl.Reply, current: rl.Span) -> rl.Transition:
        write = rl.TerminalWrite(shown=shown, prepared_revision=reply.revision, state=ReplyState.CANCELLED)
        return rl._terminal_row(reply, current, write, rl.SpanOutcome.CANCELLED, time.time_ns(), stop_applied=True)

    enqueued = await store.enqueue_reply_row(
        ReplyRowRequest(reply_id=span.reply_id, span_id=span.span_id, decide=decide, stage=rl.WriteStage.FINAL),
        PreparedReplyRow(payload={"body": "partial"}),
    )
    assert enqueued is not None


async def _final(bot: AgentBot, claimed: ApprovalContinuation, final: str) -> None:
    """Give a claimed continuation its final answer: ``pending``, ``answered``, ``ended``, or ``refused``."""
    store = _store(bot)
    await _freeze_final(bot, claimed, completed=final != "ended")
    if final == "pending":
        return
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
    if final == "refused":
        await store.record_permanent_matrix_delivery_failure(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            reason=_REFUSAL,
        )
        return
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final",
        delivered_projections=(),
    )


@contextmanager
def _final_delivery_stalled() -> Iterator[None]:
    """Keep a queued FINAL unacknowledged through delivery recovery, as a homeserver that does not answer."""
    with patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock()):
        yield


# ---------------------------------------------------------------------------
# Triggers


async def _dispatch(bot: AgentBot, owned: ApprovalContinuation) -> str | None:
    """Run the owning bot's source worker on the paused reply's source."""
    runner = _runner(bot)
    target = _recovery_target()
    request = runner._approvals._approval_response_request(owned, target=target)
    return await runner._approvals.run_owned_approvals(owned, request=request, target=target)


async def _completed_resume(bot: AgentBot, approval_id: str = "approval") -> Callable[..., Awaitable[object]]:
    """Return a resume run that answers, as ``_execute_claimed_approval`` reports a delivered answer."""
    store = _store(bot)

    async def answer(*_args: object, **_kwargs: object) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        current = await store.approvals.approval_continuation(approval_id)
        assert current is not None
        await _final(bot, current, "answered")
        return FinalDeliveryOutcome(terminal_status="completed", event_id="$waiting", is_visible_response=True), current

    return answer


# ---------------------------------------------------------------------------
# dispatch: the owning bot's source worker


@dataclass(frozen=True)
class _DispatchCase:
    id: str
    build: Callable[[AgentBot], Awaitable[ApprovalContinuation]]
    expected: Outcome
    stall_final: bool = False


async def _waiting(bot: AgentBot, lease: str | None) -> ApprovalContinuation:
    return await _paused(bot, state="waiting", runtime_generation=lease)


async def _claimed_with_final(bot: AgentBot, final: str, *, by: str = _PREVIOUS) -> ApprovalContinuation:
    claimed = await _claimed(bot, by=by)
    await _final(bot, claimed, final)
    return claimed


async def _claimed_span_ended(bot: AgentBot) -> ApprovalContinuation:
    claimed = await _claimed(bot, by=_PREVIOUS)
    await _end_claim_span(bot, claimed)
    return claimed


async def _failing(
    bot: AgentBot,
    failure: ApprovalFailure,
    *,
    final: str | None = None,
    span_ended: bool = False,
) -> ApprovalContinuation:
    claimed = await _claimed(bot, by=_PREVIOUS)
    if span_ended:
        await _end_claim_span(bot, claimed)
    if final is not None:
        await _final(bot, claimed, final)
    return await _fence(bot, claimed, failure)


async def _failing_waiting(bot: AgentBot, failure: ApprovalFailure) -> ApprovalContinuation:
    waiting = await _waiting(bot, None)
    return await _fence(bot, waiting, failure)


def _settled_error(reason: str) -> Outcome:
    """Return a paused reply settled with an error note, its cards expired with the same reason."""
    return Outcome(
        continuation=None,
        notes=((NoteKind.ERROR, reason),),
        cards=reason,
        source_pending=False,
        reply=ReplyState.FAILED,
    )


DISPATCH_CASES = (
    _DispatchCase(
        "waiting_without_lease_waits",
        lambda bot: _waiting(bot, None),
        Outcome(continuation="waiting"),
    ),
    _DispatchCase(
        "waiting_leased_by_another_runtime_settles_publication_interrupted",
        lambda bot: _waiting(bot, _PREVIOUS),
        _settled_error(_PUBLICATION_INTERRUPTED),
    ),
    _DispatchCase(
        "waiting_leased_by_this_runtime_settles_publication_interrupted",
        lambda bot: _waiting(bot, _self(bot)),
        _settled_error(_PUBLICATION_INTERRUPTED),
    ),
    _DispatchCase(
        "claimed_by_another_runtime_without_final_releases_to_replay",
        lambda bot: _claimed(bot, by=_PREVIOUS),
        Outcome(continuation=None, cards=_SHUTDOWN.reason, reply=ReplyState.ACTIVE),
    ),
    _DispatchCase(
        "claimed_by_this_runtime_without_final_releases_to_replay",
        lambda bot: _claimed(bot, by=_self(bot)),
        Outcome(continuation=None, cards=_SHUTDOWN.reason, reply=ReplyState.ACTIVE),
    ),
    _DispatchCase(
        "claimed_whose_resume_ended_unanswered_settles_shutdown",
        _claimed_span_ended,
        _settled_error(_SHUTDOWN.reason),
    ),
    _DispatchCase(
        "claimed_with_pending_final_waits",
        lambda bot: _claimed_with_final(bot, "pending"),
        Outcome(continuation="claimed", reply=ReplyState.COMPLETED),
        stall_final=True,
    ),
    _DispatchCase(
        "claimed_with_answered_final_finishes_and_finalizes",
        lambda bot: _claimed_with_final(bot, "answered"),
        Outcome(continuation=None, finalized=1, source_pending=False, reply=ReplyState.COMPLETED),
    ),
    _DispatchCase(
        "claimed_with_ended_final_finishes_and_finalizes",
        lambda bot: _claimed_with_final(bot, "ended"),
        Outcome(continuation=None, finalized=1, source_pending=False, reply=ReplyState.CANCELLED),
    ),
    _DispatchCase(
        "claimed_with_refused_final_finishes",
        lambda bot: _claimed_with_final(bot, "refused"),
        Outcome(continuation=None, cards=_REFUSAL, source_pending=False, reply=ReplyState.FAILED),
    ),
    _DispatchCase(
        "failing_user_stop_settles_cancelled",
        lambda bot: _failing_waiting(bot, _STOP),
        Outcome(
            continuation=None,
            notes=((NoteKind.CANCELLED, None),),
            cards=_STOP.reason,
            source_pending=False,
            reply=ReplyState.CANCELLED,
        ),
    ),
    _DispatchCase(
        "failing_restart_settles_restart_note",
        lambda bot: _failing(bot, _RESTART),
        Outcome(
            continuation=None,
            notes=((NoteKind.RESTART, None),),
            cards=_RESTART.reason,
            source_pending=False,
            reply=ReplyState.FAILED,
        ),
    ),
    _DispatchCase(
        "failing_interrupted_settles_interrupted_note",
        lambda bot: _failing(bot, _INTERRUPTED),
        Outcome(
            continuation=None,
            notes=((NoteKind.INTERRUPTED, None),),
            cards=_INTERRUPTED.reason,
            source_pending=False,
            reply=ReplyState.FAILED,
        ),
    ),
    _DispatchCase(
        "failing_error_settles_its_text",
        lambda bot: _failing_waiting(bot, _HTTP_502),
        _settled_error(_HTTP_502.reason),
    ),
    _DispatchCase(
        "failing_shutdown_without_final_releases_to_replay",
        lambda bot: _failing(bot, _SHUTDOWN),
        Outcome(continuation=None, cards=_SHUTDOWN.reason, reply=ReplyState.ACTIVE),
    ),
    _DispatchCase(
        "failing_shutdown_whose_resume_ended_unanswered_settles_shutdown",
        lambda bot: _failing(bot, _SHUTDOWN, span_ended=True),
        _settled_error(_SHUTDOWN.reason),
    ),
    _DispatchCase(
        "failing_with_answered_final_finishes_and_finalizes",
        lambda bot: _failing_with_final(bot, "answered"),
        Outcome(continuation=None, finalized=1, source_pending=False, reply=ReplyState.COMPLETED),
    ),
    _DispatchCase(
        "failing_with_ended_final_finishes",
        lambda bot: _failing_with_final(bot, "ended"),
        Outcome(continuation=None, cards=_HTTP_502.reason, source_pending=False, reply=ReplyState.CANCELLED),
    ),
    _DispatchCase(
        "failing_with_refused_final_finishes",
        lambda bot: _failing_with_final(bot, "refused"),
        Outcome(continuation=None, cards=_HTTP_502.reason, source_pending=False, reply=ReplyState.FAILED),
    ),
    _DispatchCase(
        "failing_with_pending_answer_waits",
        lambda bot: _failing_with_final(bot, "pending"),
        Outcome(continuation="failing", failure=_HTTP_502, reply=ReplyState.COMPLETED),
        stall_final=True,
    ),
)


async def _failing_with_final(bot: AgentBot, final: str) -> ApprovalContinuation:
    """Fence a claim with an error, then give it its final answer, as a failure racing a resume's answer."""
    claimed = await _claimed(bot, by=_PREVIOUS)
    failing = await _fence(bot, claimed, _HTTP_502)
    await _final(bot, claimed, final)
    return failing


@pytest.mark.parametrize("case", DISPATCH_CASES, ids=lambda case: case.id)
async def test_dispatch(tmp_path: Path, case: _DispatchCase) -> None:
    """The owning bot's source worker decides a paused reply that is not ready to resume."""
    bot = _new_bot(tmp_path)
    owned = await case.build(bot)
    with _observed(bot) as observer:
        if case.stall_final:
            with _final_delivery_stalled():
                await _dispatch(bot, owned)
        else:
            await _dispatch(bot, owned)
    assert await _outcome(bot, observer) == case.expected


async def test_dispatch_resumes_a_ready_reply(tmp_path: Path) -> None:
    """A ready paused reply is claimed and resumed, and its answered resume finishes it."""
    bot = _new_bot(tmp_path)
    runner = _runner(bot)
    owned = await _paused(bot)
    await bot._take_over_replies()
    with (
        _observed(bot) as observer,
        patch.object(runner._approvals, "_execute_claimed_approval", side_effect=await _completed_resume(bot)),
    ):
        assert await _dispatch(bot, owned) == "$waiting"
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        finalized=1,
        source_pending=False,
        reply=ReplyState.COMPLETED,
    )


async def test_dispatch_settles_a_ready_reply_its_requester_may_no_longer_drive(tmp_path: Path) -> None:
    """A ready paused reply whose requester lost access is settled with an error instead of resumed."""
    bot = _new_bot(tmp_path)
    owned = await _paused(bot)
    with (
        _observed(bot) as observer,
        patch(
            "mindroom.approval.continuation_runner.is_sender_allowed_for_entity_replies_in_room",
            return_value=False,
        ),
    ):
        assert await _dispatch(bot, owned) == "$waiting"
    assert await _outcome(bot, observer) == _settled_error(_UNAUTHORIZED)


# ---------------------------------------------------------------------------
# resumed: a live resume of a claimed paused reply ended


def _outcome_of(status: str, reason: str | None = None) -> Callable[..., Awaitable[object]]:
    """Return a resume run that ends with ``status``, as the gateway reports a delivery it did not complete."""

    async def end(
        claimed: ApprovalContinuation,
        **_kwargs: object,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        outcome = FinalDeliveryOutcome(
            terminal_status=status,
            event_id="$waiting",
            is_visible_response=True,
            failure_reason=reason,
        )
        return outcome, claimed

    return end


async def _resume(bot: AgentBot, run: Callable[..., Awaitable[object]], **patches: object) -> str | None:
    """Claim the ready paused reply with its resume span and run its resume, which ``run`` stands in for."""
    runner = _runner(bot)
    owned = await _paused(bot)
    async with resumed_approval(bot, owned) as claimed:
        with patch.object(runner._approvals, "_execute_claimed_approval", side_effect=run):
            if not patches:
                event_id, _suspended = await runner._approvals._run_owned_approval_continuation(
                    claimed,
                    target=_recovery_target(),
                )
                return event_id
            with patch.multiple(runner, **patches):
                event_id, _suspended = await runner._approvals._run_owned_approval_continuation(
                    claimed,
                    target=_recovery_target(),
                )
                return event_id


_FAILED_RESUMES = (
    (
        "error",
        _outcome_of("error", "The model provider returned HTTP 502."),
        Outcome(
            continuation=None,
            finalized=1,
            notes=((NoteKind.ERROR, "The model provider returned HTTP 502."),),
            cards=_HTTP_502.reason,
            source_pending=False,
            reply=ReplyState.FAILED,
        ),
    ),
    (
        "user_stop",
        _outcome_of("cancelled", "cancelled_by_user"),
        Outcome(
            continuation=None,
            finalized=1,
            notes=((NoteKind.CANCELLED, None),),
            cards=_STOP.reason,
            source_pending=False,
            reply=ReplyState.CANCELLED,
        ),
    ),
    (
        "restart",
        _outcome_of("cancelled", "sync_restart_cancelled"),
        Outcome(
            continuation=None,
            finalized=1,
            notes=((NoteKind.RESTART, None),),
            cards=_RESTART.reason,
            source_pending=False,
            reply=ReplyState.FAILED,
        ),
    ),
    (
        "interrupted",
        _outcome_of("cancelled", "interrupted"),
        Outcome(
            continuation=None,
            finalized=1,
            notes=((NoteKind.INTERRUPTED, None),),
            cards=_INTERRUPTED.reason,
            source_pending=False,
            reply=ReplyState.FAILED,
        ),
    ),
)


@pytest.mark.parametrize(
    ("run", "expected"),
    [case[1:] for case in _FAILED_RESUMES],
    ids=[c[0] for c in _FAILED_RESUMES],
)
async def test_resumed_failed_without_raising_settles_its_failure(
    tmp_path: Path,
    run: Callable[..., Awaitable[object]],
    expected: Outcome,
) -> None:
    """A live resume that ended failed or cancelled, without raising, settles the paused reply with that failure."""
    bot = _new_bot(tmp_path)
    with _observed(bot) as observer:
        await _resume(bot, run)
    assert await _outcome(bot, observer) == expected


async def test_resumed_completed_finishes_without_finalizing_again(tmp_path: Path) -> None:
    """A live resume that delivered its answer finishes the paused reply; its own lifecycle finalized it once."""
    bot = _new_bot(tmp_path)
    with _observed(bot) as observer:
        assert await _resume(bot, await _completed_resume(bot)) == "$waiting"
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        finalized=1,
        source_pending=False,
        reply=ReplyState.COMPLETED,
    )


async def test_resumed_raised_fences_with_error_and_settles_its_text(tmp_path: Path) -> None:
    """A resume that raised before its answer fences the observed claim with its error and settles it."""
    bot = _new_bot(tmp_path)
    with _observed(bot) as observer:
        assert (
            await _resume(
                bot,
                await _completed_resume(bot),
                refresh_mid_turn_context_for_approval=AsyncMock(side_effect=RuntimeError("context refresh failed")),
            )
            == "$waiting"
        )
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        notes=((NoteKind.ERROR, "context refresh failed"),),
        cards="context refresh failed",
        source_pending=False,
        reply=ReplyState.FAILED,
    )


async def test_resumed_raised_after_its_answer_finalizes_it_again(tmp_path: Path) -> None:
    """FINAL acknowledged, then finalization raised: the frozen answer is finalized again and finishes."""
    bot = _new_bot(tmp_path)
    with _observed(bot) as observer:
        calls = 0

        async def finalize_once_failing(outcome: FinalDeliveryOutcome, **_kwargs: object) -> FinalDeliveryOutcome:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "post-response effects failed"
                raise RuntimeError(msg)
            return outcome

        observer.lifecycle.finalize.side_effect = finalize_once_failing
        assert await _resume(bot, await _completed_resume(bot)) == "$waiting"
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        finalized=2,
        source_pending=False,
        reply=ReplyState.COMPLETED,
    )


async def test_resumed_with_refused_final_finishes_its_paused_reply(tmp_path: Path) -> None:
    """A live resume whose answer Matrix refused for good finishes its paused reply, as every other path does."""
    bot = _new_bot(tmp_path)
    runner = _runner(bot)
    store = _store(bot)
    owned = await _paused(bot)
    await bot._take_over_replies()

    async def refused(
        claimed: ApprovalContinuation,
        **_kwargs: object,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        current = await store.approvals.approval_continuation(claimed.approval_id)
        assert current is not None
        await _final(bot, current, "refused")
        return (
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$waiting",
                is_visible_response=True,
                failure_reason="delivery_failed",
            ),
            current,
        )

    with (
        _observed(bot) as observer,
        patch.object(runner._approvals, "_execute_claimed_approval", side_effect=refused),
    ):
        assert await _dispatch(bot, owned) == "$waiting"
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        finalized=1,
        cards=_REFUSAL,
        source_pending=False,
        reply=ReplyState.FAILED,
    )


async def _cancelled_resume(
    bot: AgentBot,
    *,
    cancel_source: str | None,
    process_shutdown: bool,
    before_run: bool,
) -> str | None:
    """Cancel a resume while its run, or the context refresh before it, is in progress; return what it returned."""
    started = asyncio.Event()

    async def wait_until_cancelled(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    blocked = AsyncMock(side_effect=wait_until_cancelled)
    if before_run:
        task = asyncio.create_task(
            _resume(bot, await _completed_resume(bot), refresh_mid_turn_context_for_approval=blocked),
        )
    else:
        task = asyncio.create_task(_resume(bot, blocked))
    await started.wait()
    request_task_cancel(task, cancel_source=cancel_source, process_shutdown=process_shutdown)
    return await task


def _cancelled_settled(note: NoteKind, cards: str, reply: ReplyState) -> Outcome:
    return Outcome(
        continuation=None,
        finalized=1,
        notes=((note, None),),
        cards=cards,
        source_pending=False,
        reply=reply,
    )


@pytest.mark.parametrize(
    ("cancel_source", "expected"),
    [
        ("user_stop", _cancelled_settled(NoteKind.CANCELLED, _STOP.reason, ReplyState.CANCELLED)),
        ("sync_restart", _cancelled_settled(NoteKind.RESTART, _RESTART.reason, ReplyState.FAILED)),
        (None, _cancelled_settled(NoteKind.INTERRUPTED, _INTERRUPTED.reason, ReplyState.FAILED)),
    ],
    ids=["user_stop", "restart", "interrupted"],
)
async def test_resumed_cancelled_in_its_run_settles_with_its_note(
    tmp_path: Path,
    cancel_source: str | None,
    expected: Outcome,
) -> None:
    """A resume cancelled while its run is in progress ends cancelled without raising and settles its kind."""
    bot = _new_bot(tmp_path)
    with _observed(bot) as observer:
        returned = await _cancelled_resume(bot, cancel_source=cancel_source, process_shutdown=False, before_run=False)
    assert returned is None
    assert await _outcome(bot, observer) == expected


@pytest.mark.parametrize(
    ("cancel_source", "process_shutdown", "before_run", "failure"),
    [
        ("user_stop", False, True, _STOP),
        ("sync_restart", False, True, _RESTART),
        (None, False, True, _INTERRUPTED),
        ("sync_restart", True, True, _SHUTDOWN),
        ("sync_restart", True, False, _SHUTDOWN),
    ],
    ids=["user_stop", "restart", "interrupted", "shutdown_before_run", "shutdown_in_run"],
)
async def test_resumed_cancelled_outside_its_run_fences_its_kind_and_reraises(
    tmp_path: Path,
    cancel_source: str | None,
    process_shutdown: bool,
    before_run: bool,
    failure: ApprovalFailure,
) -> None:
    """A resume cancelled outside its run, or by a shutdown, fences the observed claim and re-raises; dispatch settles.

    Its resume span ends failed, except after a shutdown, which leaves it as a crash would for the next runtime.
    """
    bot = _new_bot(tmp_path)
    with _observed(bot) as observer, pytest.raises(asyncio.CancelledError):
        await _cancelled_resume(
            bot,
            cancel_source=cancel_source,
            process_shutdown=process_shutdown,
            before_run=before_run,
        )
    assert await _outcome(bot, observer) == Outcome(continuation="failing", failure=failure, reply=ReplyState.ACTIVE)
    failing = await _store(bot).approvals.approval_continuation("approval")
    assert failing is not None
    assert failing.claim_span_id is not None
    resume = await _store(bot).replies.span(failing.claim_span_id)
    assert resume is not None
    assert resume.outcome is (None if process_shutdown else rl.SpanOutcome.FAILED)


async def test_resumed_cancelled_after_a_queued_answer_waits_for_it(tmp_path: Path) -> None:
    """A resume cancelled after its answer was queued keeps its claim: delivery recovery owns the answer."""
    bot = _new_bot(tmp_path)
    store = _store(bot)
    started = asyncio.Event()

    async def queue_then_wait(claimed: ApprovalContinuation, **_kwargs: object) -> None:
        current = await store.approvals.approval_continuation(claimed.approval_id)
        assert current is not None
        await _final(bot, current, "pending")
        started.set()
        await asyncio.Event().wait()

    with _observed(bot) as observer, _final_delivery_stalled():
        task = asyncio.create_task(_resume(bot, queue_then_wait))
        await started.wait()
        request_task_cancel(task, cancel_source="user_stop")
        assert await task is None
    assert await _outcome(bot, observer) == Outcome(continuation="claimed", finalized=1, reply=ReplyState.COMPLETED)


async def test_resumed_advanced_to_a_ready_generation_resumes_it(tmp_path: Path) -> None:
    """A resume that paused again on calls already decided resumes the next generation at once."""
    bot = _new_bot(tmp_path)
    runner = _runner(bot)
    store = _store(bot)
    owned = await _paused(bot)
    await bot._take_over_replies()
    answer = await _completed_resume(bot)
    runs = 0

    async def advance_then_answer(claimed: ApprovalContinuation, **kwargs: object) -> object:
        nonlocal runs
        runs += 1
        if runs > 1:
            return await answer(claimed, **kwargs)
        advanced = await _advance(store, claimed, decided=True)
        return (
            FinalDeliveryOutcome(terminal_status="suspended", event_id="$waiting", is_visible_response=True),
            advanced,
        )

    with (
        _observed(bot) as observer,
        patch.object(runner._approvals, "_execute_claimed_approval", side_effect=advance_then_answer),
    ):
        assert await _dispatch(bot, owned) == "$waiting"
    assert runs == 2
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        finalized=2,
        source_pending=False,
        reply=ReplyState.COMPLETED,
    )


async def test_resumed_leaves_another_paused_reply_of_its_source_alone(tmp_path: Path) -> None:
    """A resume that paused again continues only its own paused reply, never another that owns its source."""
    bot = _new_bot(tmp_path)
    runner = _runner(bot)
    store = _store(bot)
    owned = await _paused(bot)
    await bot._take_over_replies()
    claim = AsyncMock(wraps=runner._approvals._claim_owned_approval)
    read_source = type(store.approvals).approval_continuation_for_source

    async def other_owner(approvals: object, source_event_id: str) -> ApprovalContinuation | None:
        current = await read_source(approvals, source_event_id)
        return None if current is None else replace(current, approval_id="other")

    async def advance(claimed: ApprovalContinuation, **_kwargs: object) -> object:
        advanced = await _advance(store, claimed, decided=True)
        return (
            FinalDeliveryOutcome(terminal_status="suspended", event_id="$waiting", is_visible_response=True),
            advanced,
        )

    with (
        _observed(bot),
        patch.object(runner._approvals, "_execute_claimed_approval", side_effect=advance),
        patch.object(runner._approvals, "_claim_owned_approval", new=claim),
    ):
        request = runner._approvals._approval_response_request(owned, target=_recovery_target())
        with patch.object(type(store.approvals), "approval_continuation_for_source", new=other_owner):
            assert (
                await runner._approvals.run_owned_approvals(owned, request=request, target=_recovery_target()) is None
            )
    claim.assert_awaited_once()


async def test_resumed_advanced_to_a_waiting_generation_waits(tmp_path: Path) -> None:
    """A resume that paused again on a call that needs a person waits for that decision."""
    bot = _new_bot(tmp_path)
    runner = _runner(bot)
    store = _store(bot)
    owned = await _paused(bot)
    await bot._take_over_replies()

    async def advance(claimed: ApprovalContinuation, **_kwargs: object) -> object:
        advanced = await _advance(store, claimed, decided=False)
        return (
            FinalDeliveryOutcome(terminal_status="suspended", event_id="$waiting", is_visible_response=True),
            advanced,
        )

    with (
        _observed(bot) as observer,
        patch.object(runner._approvals, "_execute_claimed_approval", side_effect=advance),
    ):
        assert await _dispatch(bot, owned) is None
    assert await _outcome(bot, observer) == Outcome(continuation="waiting", finalized=1)


async def _advance(store: PrincipalStore, claimed: ApprovalContinuation, *, decided: bool) -> ApprovalContinuation:
    """Record a claimed run's next pause on one call, and publish it as its cards would."""
    advanced = await advance_continuation(
        store,
        claimed.approval_id,
        claimant_generation=claimed.generation,
        run_id=claimed.run_id,
        session_id=claimed.session_id,
        calls=(
            ApprovalCall(
                tool_call_id="call-next",
                tool_name="run_shell_command",
                invoking_agent="general",
                toolkit_name="shell",
                expires_at_ns=time.time_ns() + 60_000_000_000,
                decision=ApprovalDecision.APPROVED if decided else None,
                human_approval_required=not decided,
            ),
        ),
    )
    assert advanced is not None
    activated = await store.approvals.activate_approval_continuation(
        claimed.approval_id,
        expected_generation=advanced.generation,
    )
    assert activated is not None
    return activated


# ---------------------------------------------------------------------------
# cli_exit: a minimal-mode response's approval scope ended

_CLI_CALL = {"tool": "mindroom-agent", "arguments": {"prompt": "deploy"}}


@dataclass(frozen=True)
class _CliCase:
    id: str
    build: Callable[[AgentBot], Awaitable[ApprovalContinuation]]
    expected: Outcome
    suspended: bool = False
    live: bool = True
    shutting_down: bool = False
    failure_reason: str | None = None
    wakes: tuple[tuple[str, ...], ...] = ()
    stall_final: bool = False


async def _cli(
    bot: AgentBot,
    *,
    final: str | None = None,
    by: str | None = None,
    **changes: object,
) -> ApprovalContinuation:
    """Pause a minimal-mode call's reply, claimed in place by ``by`` when given, with its final answer."""
    if by is not None:
        changes |= {"state": "claimed", "runtime_generation": by}
    continuation = await _paused(bot, cli_call=_CLI_CALL, **changes)
    if final is not None:
        await _final(bot, continuation, final)
    return continuation


async def _cli_failing(bot: AgentBot, failure: ApprovalFailure, *, final: str | None = None) -> ApprovalContinuation:
    claimed = await _cli(bot, by=_self(bot))
    failing = await _fence(bot, claimed, failure)
    if final is not None:
        await _final(bot, claimed, final)
    return failing


_FENCED_CLI = Outcome(
    continuation="failing",
    failure=ApprovalFailure(ApprovalFailureKind.ERROR, _CLI_ENDED),
    reply=ReplyState.ACTIVE,
)

CLI_CASES = (
    _CliCase(
        "suspended_ready_wakes_its_sources",
        lambda bot: _cli(bot),
        Outcome(continuation="ready"),
        suspended=True,
        wakes=(("$source",),),
    ),
    _CliCase(
        "suspended_waiting_leaves_it",
        lambda bot: _cli(bot, state="waiting"),
        Outcome(continuation="waiting"),
        suspended=True,
    ),
    _CliCase(
        "recovered_lifecycle_leaves_it",
        lambda bot: _cli(bot, by=_self(bot)),
        Outcome(continuation="claimed", reply=ReplyState.ACTIVE),
        live=False,
    ),
    _CliCase(
        "standard_reply_leaves_it",
        lambda bot: _paused(bot, state="claimed", runtime_generation=_self(bot)),
        Outcome(continuation="claimed", reply=ReplyState.ACTIVE),
    ),
    _CliCase(
        "claimed_by_another_runtime_leaves_it",
        lambda bot: _cli(bot, by=_PREVIOUS),
        Outcome(continuation="claimed", reply=ReplyState.ACTIVE),
    ),
    _CliCase(
        "failing_settles_with_its_failure",
        lambda bot: _cli_failing(bot, _STOP),
        Outcome(
            continuation=None,
            notes=((NoteKind.CANCELLED, None),),
            cards=_STOP.reason,
            source_pending=False,
            reply=ReplyState.CANCELLED,
        ),
    ),
    _CliCase(
        "failing_during_shutdown_leaves_it_to_recovery",
        lambda bot: _cli_failing(bot, _HTTP_502),
        Outcome(continuation="failing", failure=_HTTP_502, reply=ReplyState.ACTIVE),
        shutting_down=True,
    ),
    _CliCase(
        "failing_with_answered_final_finishes",
        lambda bot: _cli_failing(bot, _HTTP_502, final="answered"),
        Outcome(continuation=None, source_pending=False, reply=ReplyState.COMPLETED),
    ),
    _CliCase(
        "answered_final_finishes",
        lambda bot: _cli(bot, by=_self(bot), final="answered"),
        Outcome(continuation=None, source_pending=False, reply=ReplyState.COMPLETED),
    ),
    _CliCase(
        "ended_final_finishes",
        lambda bot: _cli(bot, by=_self(bot), final="ended"),
        Outcome(continuation=None, source_pending=False, reply=ReplyState.CANCELLED),
    ),
    _CliCase(
        "pending_final_waits_for_its_delivery",
        lambda bot: _cli(bot, by=_self(bot), final="pending"),
        Outcome(continuation="claimed", reply=ReplyState.COMPLETED),
        stall_final=True,
    ),
    _CliCase(
        "refused_final_fences",
        lambda bot: _cli(bot, by=_self(bot), final="refused"),
        replace(_FENCED_CLI, reply=ReplyState.FAILED),
        wakes=(("$source",),),
    ),
    _CliCase(
        "claimed_without_final_fences_with_the_response_failure",
        lambda bot: _cli(bot, by=_self(bot)),
        replace(_FENCED_CLI, failure=_HTTP_502),
        failure_reason=_HTTP_502.detail,
        wakes=(("$source",),),
    ),
    _CliCase(
        "claimed_without_final_fences_with_the_default_text",
        lambda bot: _cli(bot, by=_self(bot)),
        _FENCED_CLI,
        wakes=(("$source",),),
    ),
    _CliCase(
        "claimed_without_final_during_shutdown_leaves_it",
        lambda bot: _cli(bot, by=_self(bot)),
        Outcome(continuation="claimed", reply=ReplyState.ACTIVE),
        shutting_down=True,
    ),
    _CliCase(
        "waiting_without_final_fences",
        lambda bot: _cli(bot, state="waiting"),
        replace(_FENCED_CLI, reply=ReplyState.PAUSED),
        wakes=(("$source",),),
    ),
)


async def _cli_exit(bot: AgentBot, case: _CliCase) -> None:
    """End a minimal-mode response's approval scope, which paused a call, as ``case`` describes it."""
    progress = DeliveryProgress(failure_reason=case.failure_reason)
    waits = _runner(bot)._approvals._cli_approval_waits
    with patch("mindroom.cancellation.task_is_process_shutdown", return_value=case.shutting_down):
        await waits._settle("$source", suspended=case.suspended, progress=progress, settle_terminal=case.live)


@pytest.mark.parametrize("case", CLI_CASES, ids=lambda case: case.id)
async def test_cli_exit(tmp_path: Path, case: _CliCase) -> None:
    """A minimal-mode response's approval scope ended: wake, finish, settle, fence, or leave its paused reply."""
    bot = _new_bot(tmp_path)
    await case.build(bot)
    with _observed(bot) as observer:
        if case.stall_final:
            with _final_delivery_stalled():
                await _cli_exit(bot, case)
        else:
            await _cli_exit(bot, case)
    assert await _outcome(bot, observer) == case.expected
    assert tuple(observer.wakes) == case.wakes


# ---------------------------------------------------------------------------
# owner_unavailable: orchestrator recovery of a paused reply whose entity has no bot


def _recovery(bot: AgentBot, observer: _Observer, *, recover_final: AsyncMock) -> tuple[ApprovalRecovery, AsyncMock]:
    notice = AsyncMock(return_value=None)
    return (
        ApprovalRecovery(
            deliver_unavailable_notice=notice,
            journal_provider=lambda: bot._journal_store,
            recover_unavailable_final=recover_final,
            cancel_delegations=AsyncMock(),
            manager=MagicMock(expire_continuation_cards=observer.expire),
        ),
        notice,
    )


_UNAVAILABLE = "Requesting agent 'general' is no longer available."


async def test_owner_unavailable_without_final_discards_with_a_notice(tmp_path: Path) -> None:
    """A paused reply whose entity is gone and that has no answer is fenced, its cards expired, and a notice sent."""
    bot = _new_bot(tmp_path)
    continuation = await _waiting(bot, None)
    recover_final = AsyncMock(return_value=True)
    with _observed(bot) as observer:
        recovery, notice = _recovery(bot, observer, recover_final=recover_final)
        principal_id = _runner(bot).deps.approval_store.principal_id
        # The notice could not be sent yet, so the paused reply is kept for the next pass.
        assert not await recovery._discard_unavailable(principal_id, continuation, _UNAVAILABLE)
    notice.assert_awaited_once()
    assert notice.await_args.args[1] == _UNAVAILABLE
    recover_final.assert_not_awaited()
    assert await _outcome(bot, observer) == Outcome(
        continuation="failing",
        failure=ApprovalFailure(ApprovalFailureKind.ERROR, _UNAVAILABLE),
        cards=_UNAVAILABLE,
    )


@pytest.mark.parametrize("final", ["pending", "answered", "ended", "refused"])
async def test_owner_unavailable_with_final_recovers_it_as_its_entity(tmp_path: Path, final: str) -> None:
    """A paused reply whose entity is gone but that has a final answer recovers it under its original principal."""
    bot = _new_bot(tmp_path)
    continuation = await _claimed_with_final(bot, final)
    recover_final = AsyncMock(return_value=True)
    with _observed(bot) as observer:
        recovery, notice = _recovery(bot, observer, recover_final=recover_final)
        principal_id = _runner(bot).deps.approval_store.principal_id
        assert await recovery._discard_unavailable(principal_id, continuation, _UNAVAILABLE)
    notice.assert_not_awaited()
    recover_final.assert_awaited_once()
    assert observer.expire.await_count == 0


@pytest.mark.parametrize(
    ("final", "recovered", "expected"),
    [
        (None, False, Outcome(continuation="claimed", reply=ReplyState.ACTIVE)),
        ("pending", False, Outcome(continuation="claimed", reply=ReplyState.COMPLETED)),
        (
            "answered",
            True,
            Outcome(continuation=None, finalized=1, source_pending=False, reply=ReplyState.COMPLETED),
        ),
        ("ended", True, Outcome(continuation=None, source_pending=False, reply=ReplyState.CANCELLED)),
        (
            "refused",
            True,
            Outcome(
                continuation=None,
                cards=_REFUSAL,
                source_pending=False,
                reply=ReplyState.FAILED,
            ),
        ),
    ],
)
async def test_owner_unavailable_final_recovery(
    tmp_path: Path,
    final: str | None,
    recovered: bool,
    expected: Outcome,
) -> None:
    """The original principal recovers a gone entity's final answer: finish, finalize, or wait for its delivery."""
    bot = _new_bot(tmp_path)
    continuation = await _claimed(bot, by=_PREVIOUS)
    if final is not None:
        await _final(bot, continuation, final)
    with _observed(bot) as observer, _final_delivery_stalled():
        assert await _runner(bot).recover_approval_final(continuation.approval_id) is recovered
    assert await _outcome(bot, observer) == expected


# ---------------------------------------------------------------------------
# settle: the settlement every decider shares


async def test_settle_leaves_a_reply_whose_answer_stands(tmp_path: Path) -> None:
    """A settlement does nothing to a failing paused reply whose resume already froze a successful answer."""
    bot = _new_bot(tmp_path)
    continuation = await _failing_with_final(bot, "answered")
    with _observed(bot) as observer:
        assert not await _runner(bot)._approvals._settle(continuation, _HTTP_502)
    # Its answer is finalized when its source worker finishes it.
    assert await _outcome(bot, observer) == Outcome(
        continuation="failing",
        failure=_HTTP_502,
        reply=ReplyState.COMPLETED,
    )


async def test_settle_counts_a_gone_paused_reply_settled(tmp_path: Path) -> None:
    """A paused reply that already ended needs no settlement, so its owner's recovery stops retrying it."""
    bot = _new_bot(tmp_path)
    continuation = await _failing_waiting(bot, _STOP)
    approvals = _runner(bot)._approvals
    with _observed(bot):
        assert await approvals._settle(continuation, _STOP)
        assert await approvals._settle(continuation, _STOP)


async def test_settle_fences_only_the_state_its_decider_observed(tmp_path: Path) -> None:
    """A paused reply whose state changed since its decider read it is not fenced in its new state."""
    bot = _new_bot(tmp_path)
    observed = await _waiting(bot, _PREVIOUS)
    # Its publication finished after all: it became ready.
    assert await _store(bot).approvals.activate_approval_continuation("approval", expected_generation=0) is not None
    with _observed(bot) as observer:
        assert not await _runner(bot)._approvals._settle(observed, _PUBLICATION_FAILED)
    assert await _outcome(bot, observer) == Outcome(continuation="ready")


async def test_settle_expires_cards_with_the_failure_the_paused_reply_holds(tmp_path: Path) -> None:
    """A settlement asked for with another failure still ends the cards with the failure the reply already holds."""
    bot = _new_bot(tmp_path)
    continuation = await _failing_waiting(bot, _STOP)
    with _observed(bot) as observer:
        assert await _runner(bot)._approvals._settle(continuation, _HTTP_502)
    assert await _outcome(bot, observer) == Outcome(
        continuation=None,
        notes=((NoteKind.ERROR, _HTTP_502.reason),),
        cards=_STOP.reason,
        source_pending=False,
        reply=ReplyState.FAILED,
    )


async def test_cli_exit_of_a_paused_reply_already_ended_does_nothing(tmp_path: Path) -> None:
    """A minimal-mode scope that ends after its paused reply ended has nothing left to decide."""
    bot = _new_bot(tmp_path)
    await _admit_source(_store(bot))
    with _observed(bot) as observer:
        await _cli_exit(bot, _CliCase("gone", _cli, Outcome(continuation=None)))
    assert observer.notes == []
    assert observer.expire.await_count == 0
    assert observer.wakes == []
