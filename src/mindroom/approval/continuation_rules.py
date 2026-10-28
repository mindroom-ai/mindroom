"""What a paused reply does next: pure rules over its continuation's facts and the trigger that asks.

A paused reply is a reply whose run waits for tool approval; its approval
continuation holds the run. ``ApprovalContinuationRunner`` and the
unavailable-owner recovery read the facts, ask ``next_step``, and run the step
it returns. Reply presentation and unapplied Stops stay with
``reply_lifecycle``; these rules decide only the paused reply's next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, assert_never

from mindroom.event_journal import ApprovalFailure
from mindroom.reply_lifecycle import ApprovalFailureKind, SpanOutcome

if TYPE_CHECKING:
    from mindroom.event_journal import ApprovalContinuation, MatrixDelivery
    from mindroom.event_journal.approval_continuations import ApprovalContinuationState


class _FinalAnswer(StrEnum):
    """What became of the run's final answer: the FINAL delivery the continuation names."""

    NONE = "none"
    # Queued, not acknowledged.
    PENDING = "pending"
    # Acknowledged, and the span that wrote it completed.
    ANSWERED = "answered"
    # Acknowledged, and the span that wrote it did not complete.
    ENDED = "ended"
    # Permanently refused.
    REFUSED = "refused"


def _final_answer(final: MatrixDelivery | None, span_outcome: SpanOutcome | None) -> _FinalAnswer:
    """Classify the FINAL delivery a continuation names, given the outcome of the span that wrote it."""
    if final is None:
        return _FinalAnswer.NONE
    if final.permanently_failed:
        return _FinalAnswer.REFUSED
    if final.acknowledged_event_id is None:
        return _FinalAnswer.PENDING
    return _FinalAnswer.ANSWERED if span_outcome is SpanOutcome.COMPLETED else _FinalAnswer.ENDED


class _Holder(StrEnum):
    """Which bot instance holds a waiting reply's publication lease or a claimed reply's claim."""

    NONE = "none"
    SELF = "self"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ContinuationFacts:
    """What the rules read of one continuation."""

    state: ApprovalContinuationState
    holder: _Holder
    final: _FinalAnswer
    # Why a failing continuation failed; every failing continuation has one.
    failure: ApprovalFailure | None = None
    # Why Matrix refused the final answer, when it did.
    refusal: str | None = None
    # How the claim's span ended; ``None`` while it runs or before any claim.
    claim_outcome: SpanOutcome | None = None
    # Whether the continuation pauses a minimal-mode (CLI) call.
    cli: bool = False


def continuation_facts(
    continuation: ApprovalContinuation,
    *,
    runtime_generation: str | None,
    final: MatrixDelivery | None,
    final_span_outcome: SpanOutcome | None,
    claim_outcome: SpanOutcome | None = None,
) -> ContinuationFacts:
    """Return the facts of ``continuation`` as the bot instance ``runtime_generation`` reads them, with its FINAL."""
    holder = (
        _Holder.NONE
        if continuation.runtime_generation is None
        else _Holder.SELF
        if continuation.runtime_generation == runtime_generation
        else _Holder.OTHER
    )
    return ContinuationFacts(
        state=continuation.state,
        holder=holder,
        final=_final_answer(final, final_span_outcome),
        failure=continuation.failure,
        refusal=None if final is None else final.permanent_failure_reason,
        claim_outcome=claim_outcome,
        cli=continuation.cli_call is not None,
    )


# ---------------------------------------------------------------------------
# Triggers


@dataclass(frozen=True, slots=True)
class Dispatch:
    """The owning bot's source worker runs the paused reply's source."""


@dataclass(frozen=True, slots=True)
class Resumed:
    """A live resume of the claimed paused reply ended.

    ``completed`` delivered its answer; ``suspended`` paused again, or left an
    answer it already queued to delivery; ``failed`` ended with ``failure``
    without raising; ``raised`` raised ``failure``; ``cancelled`` was cancelled
    with ``failure``.
    """

    ending: Literal["completed", "suspended", "failed", "raised", "cancelled"]
    failure: ApprovalFailure | None = None
    # Whether a suspended resume advanced the paused reply past the generation it claimed.
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class OwnerUnavailable:
    """Orchestrator recovery of a paused reply whose entity has no bot to run it."""


@dataclass(frozen=True, slots=True)
class CliExit:
    """A minimal-mode response's approval scope ended after it paused a call.

    It exists only while minimal-mode approvals pause their reply.
    """

    # Whether the response suspended for the approval instead of ending.
    suspended: bool
    # Whether the scope is the live response's, rather than a recovered lifecycle's.
    live: bool
    shutting_down: bool
    # How the response ended when it ended without an answer.
    failure: ApprovalFailure


type _Trigger = Dispatch | Resumed | OwnerUnavailable | CliExit


# ---------------------------------------------------------------------------
# Steps


@dataclass(frozen=True, slots=True)
class Wait:
    """Do nothing now; a later trigger decides."""


@dataclass(frozen=True, slots=True)
class Resume:
    """Claim the ready generation and resume its run, after the authorization recheck."""


@dataclass(frozen=True, slots=True)
class Finish:
    """End the paused reply through its acknowledged or refused final answer.

    ``finalize`` first runs the response lifecycle's finalization (post-response
    effects and cancellation hooks) for the recovered answer.
    """

    finalize: bool


@dataclass(frozen=True, slots=True)
class Settle:
    """Fence the state unless failing, expire the cards, cancel delegations, and end the paused reply.

    The reply ends without a note when it is gone or its final answer settled,
    and with the failure's note otherwise.
    """

    failure: ApprovalFailure


@dataclass(frozen=True, slots=True)
class Release:
    """Fence, expire the cards, cancel delegations, and hand the sources back to replay."""

    failure: ApprovalFailure


@dataclass(frozen=True, slots=True)
class Fence:
    """Fence the observed state with ``failure``; the source worker settles it."""

    failure: ApprovalFailure


@dataclass(frozen=True, slots=True)
class DiscardUnavailable:
    """Fence, expire the cards, cancel delegations, notify through an available principal, then discard."""


@dataclass(frozen=True, slots=True)
class WakeSources:
    """Hand the ready paused reply's sources to its source worker."""


type Step = Wait | Resume | Finish | Settle | Release | Fence | DiscardUnavailable | WakeSources

_PUBLICATION_INTERRUPTED = ApprovalFailure(
    ApprovalFailureKind.ERROR,
    "Tool approval card publication was interrupted and denied safely.",
)
_SHUTDOWN = ApprovalFailure(ApprovalFailureKind.SHUTDOWN)
# A resume span that ended this way wrote its own ending; only a restart's or a release's hands work back.
_UNANSWERED_RESUME_OUTCOMES = frozenset({SpanOutcome.FAILED, SpanOutcome.CANCELLED, SpanOutcome.SUPPRESSED})


def next_step(facts: ContinuationFacts, trigger: _Trigger) -> Step:
    """Return what the paused reply does next."""
    match trigger:
        case CliExit():
            return _cli_exit(facts, trigger)
        case Dispatch():
            return _dispatch(facts)
        case Resumed():
            return _resumed(facts, trigger)
        case OwnerUnavailable():
            return _owner_unavailable(facts)
        case _:
            assert_never(trigger)


def _failure(facts: ContinuationFacts) -> ApprovalFailure:
    assert facts.failure is not None, "a failing continuation holds its failure"
    return facts.failure


def _refused(facts: ContinuationFacts) -> Settle:
    """Settle a paused reply whose final answer Matrix refused for good; the refusal settles its FINAL, so it ends."""
    return Settle(facts.failure or ApprovalFailure(ApprovalFailureKind.ERROR, facts.refusal))


def _dispatch(facts: ContinuationFacts) -> Step:  # noqa: PLR0911 - one return per row of the rules table
    match facts.final:
        case _FinalAnswer.ANSWERED:
            return Finish(finalize=True)
        case _FinalAnswer.ENDED:
            # A claimed reply's acknowledged final is its resume's outcome; a failing one's may be its settlement note.
            return Settle(_failure(facts)) if facts.state == "failing" else Finish(finalize=True)
        case _FinalAnswer.REFUSED:
            # A refusal settles the FINAL, so the settlement ends the paused reply.
            return Settle(facts.failure or _SHUTDOWN)
        case _FinalAnswer.PENDING:
            # Delivery recovery owns the outcome.
            return Wait()
        case _FinalAnswer.NONE:
            pass
    match facts.state:
        case "waiting":
            # A publication lease that is still held means its publication was interrupted.
            return Wait() if facts.holder is _Holder.NONE else Settle(_PUBLICATION_INTERRUPTED)
        case "ready":
            return Resume()
        case "failing" if _failure(facts).kind is not ApprovalFailureKind.SHUTDOWN:
            return Settle(_failure(facts))
    # A claim nothing runs any more, or a resume a shutdown cut short: replay continues its turn, unless the resume
    # already ended without its answer and so left nothing to continue.
    failure = facts.failure or _SHUTDOWN
    return Settle(failure) if facts.claim_outcome in _UNANSWERED_RESUME_OUTCOMES else Release(failure)


def _resumed(facts: ContinuationFacts, trigger: Resumed) -> Step:
    match trigger.ending:
        case "completed":
            # The resume's own lifecycle finalized its answer.
            return Finish(finalize=False)
        case "failed":
            assert trigger.failure is not None
            return Settle(trigger.failure)
        case "suspended":
            # A queued or delivered final answer leaves nothing to resume.
            advanced = trigger.advanced and facts.state == "ready" and facts.final is _FinalAnswer.NONE
            return Resume() if advanced else Wait()
    assert trigger.failure is not None
    match facts.final:
        case _FinalAnswer.ANSWERED | _FinalAnswer.ENDED:
            # The answer reached Matrix before the resume raised or was cancelled.
            return Finish(finalize=True)
        case _FinalAnswer.PENDING:
            return Wait()
    # A cancelled resume leaves the settlement to its source worker.
    return Fence(trigger.failure) if trigger.ending == "cancelled" else Settle(trigger.failure)


def _owner_unavailable(facts: ContinuationFacts) -> Step:
    match facts.final:
        case _FinalAnswer.NONE:
            return DiscardUnavailable()
        case _FinalAnswer.PENDING:
            return Wait()
        case _FinalAnswer.ANSWERED:
            return Finish(finalize=True)
        case _FinalAnswer.ENDED:
            return Finish(finalize=False)
        case _FinalAnswer.REFUSED:
            return _refused(facts)


def _cli_exit(facts: ContinuationFacts, trigger: CliExit) -> Step:
    if trigger.suspended:
        return WakeSources() if facts.state == "ready" else Wait()
    if not trigger.live or not facts.cli or (facts.state == "claimed" and facts.holder is not _Holder.SELF):
        # The enclosing response already finalized; a recovered lifecycle settles after its own final delivery.
        return Wait()
    if facts.state == "failing" and facts.final is not _FinalAnswer.ANSWERED:
        # A shutdown leaves the settlement to approval recovery at the next start.
        return Wait() if trigger.shutting_down else Settle(_failure(facts))
    if facts.final in {_FinalAnswer.PENDING, _FinalAnswer.ANSWERED, _FinalAnswer.ENDED}:
        return Finish(finalize=False)
    # No answer reached the room, including one Matrix refused for good: the approval fails.
    return Wait() if trigger.shutting_down else Fence(trigger.failure)
