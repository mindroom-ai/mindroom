"""The paused-reply rules decide each trigger's next step from the continuation's facts alone."""

from __future__ import annotations

from dataclasses import replace
from itertools import product
from typing import TYPE_CHECKING

import pytest

from mindroom.approval.continuation_rules import (
    _PUBLICATION_INTERRUPTED,
    CliExit,
    ContinuationFacts,
    DiscardUnavailable,
    Dispatch,
    Fence,
    Finish,
    OwnerUnavailable,
    Release,
    Resume,
    Resumed,
    Settle,
    Wait,
    WakeSources,
    _final_answer,
    _FinalAnswer,
    _Holder,
    _Trigger,
    next_step,
)
from mindroom.event_journal import ApprovalFailure, DeliveryStage, MatrixDelivery
from mindroom.reply_lifecycle import ApprovalFailureKind, SpanOutcome

if TYPE_CHECKING:
    from mindroom.approval.continuation_rules import Step

_ERROR = ApprovalFailure(ApprovalFailureKind.ERROR, "The model provider returned HTTP 502.")
_STOP = ApprovalFailure(ApprovalFailureKind.USER_STOP)
_SHUTDOWN = ApprovalFailure(ApprovalFailureKind.SHUTDOWN)
_REFUSAL = "matrix event exceeds the hard size limit"
_FAILURES = tuple(
    ApprovalFailure(kind, "detail" if kind is ApprovalFailureKind.ERROR else None) for kind in ApprovalFailureKind
)
_CLAIM_OUTCOMES = (None, *SpanOutcome)


def _facts(
    state: str = "waiting",
    *,
    holder: _Holder = _Holder.NONE,
    final: _FinalAnswer = _FinalAnswer.NONE,
    failure: ApprovalFailure | None = None,
    claim_outcome: SpanOutcome | None = None,
    cli: bool = False,
) -> ContinuationFacts:
    return ContinuationFacts(
        state=state,
        holder=holder,
        final=final,
        failure=failure if failure is not None or state != "failing" else _ERROR,
        refusal=_REFUSAL if final is _FinalAnswer.REFUSED else None,
        claim_outcome=claim_outcome,
        cli=cli,
    )


def _every_facts() -> list[ContinuationFacts]:
    """Return every combination of facts a continuation can have."""
    return [
        _facts(state, holder=holder, final=final, failure=failure, claim_outcome=claim_outcome, cli=cli)
        for state, holder, final, claim_outcome, cli in product(
            ("waiting", "ready", "claimed", "failing"),
            _Holder,
            _FinalAnswer,
            _CLAIM_OUTCOMES,
            (False, True),
        )
        for failure in (_FAILURES if state == "failing" else (None,))
    ]


def _every_trigger() -> list[_Trigger]:
    """Return every trigger the rules answer."""
    resumed: list[_Trigger] = [Resumed("completed"), Resumed("suspended"), Resumed("suspended", advanced=True)]
    resumed += [Resumed(ending, failure) for ending in ("failed", "raised", "cancelled") for failure in _FAILURES]
    cli: list[_Trigger] = [
        CliExit(suspended=suspended, live=live, shutting_down=shutting_down, failure=_ERROR)
        for suspended, live, shutting_down in product((False, True), repeat=3)
    ]
    return [Dispatch(), OwnerUnavailable(), *resumed, *cli]


_EVERY_FACTS = _every_facts()
_EVERY_TRIGGER = _every_trigger()


@pytest.mark.parametrize("trigger", _EVERY_TRIGGER, ids=repr)
def test_every_input_has_one_step_within_the_triggers_vocabulary(trigger: _Trigger) -> None:
    """Each trigger answers every combination of facts, only with the steps its owner can run."""
    allowed: dict[type, set[type]] = {
        Dispatch: {Wait, Resume, Finish, Settle, Release},
        Resumed: {Wait, Resume, Finish, Settle, Fence},
        OwnerUnavailable: {Wait, Finish, Settle, DiscardUnavailable},
        CliExit: {Wait, Finish, Settle, Fence, WakeSources},
    }[type(trigger)]
    for facts in _EVERY_FACTS:
        step = next_step(facts, trigger)
        assert type(step) in allowed, (facts, step)
        # Only a final answer that reached Matrix is finalized, and never by a minimal-mode exit or a live resume.
        if isinstance(step, Finish) and step.finalize:
            assert facts.final in {_FinalAnswer.ANSWERED, _FinalAnswer.ENDED}, (facts, step)
            assert not isinstance(trigger, CliExit)
            assert not (isinstance(trigger, Resumed) and trigger.ending in {"completed", "failed", "suspended"})
        # A queued final answer is delivery recovery's: only a minimal-mode exit ends its paused reply around it.
        if facts.final is _FinalAnswer.PENDING and not isinstance(trigger, CliExit):
            assert isinstance(step, Wait) or (
                isinstance(trigger, Resumed) and trigger.ending in {"completed", "failed"}
            )


@pytest.mark.parametrize("facts", _EVERY_FACTS, ids=repr)
def test_a_settlement_names_the_failure_a_failing_continuation_holds(facts: ContinuationFacts) -> None:
    """Every trigger that settles a failing paused reply settles it with the failure it already holds."""
    if facts.state != "failing":
        return
    for trigger in (
        Dispatch(),
        OwnerUnavailable(),
        CliExit(suspended=False, live=True, shutting_down=False, failure=_STOP),
    ):
        step = next_step(facts, trigger)
        if isinstance(step, Settle):
            assert step.failure == facts.failure


DISPATCH_ROWS: list[tuple[str, ContinuationFacts, Step]] = [
    ("answered_finalizes", _facts("claimed", final=_FinalAnswer.ANSWERED), Finish(finalize=True)),
    ("answered_while_failing_finalizes", _facts("failing", final=_FinalAnswer.ANSWERED), Finish(finalize=True)),
    ("ended_claim_finalizes", _facts("claimed", final=_FinalAnswer.ENDED), Finish(finalize=True)),
    ("ended_while_failing_settles", _facts("failing", final=_FinalAnswer.ENDED, failure=_STOP), Settle(_STOP)),
    ("refused_claim_settles_shutdown", _facts("claimed", final=_FinalAnswer.REFUSED), Settle(_SHUTDOWN)),
    ("refused_while_failing_settles", _facts("failing", final=_FinalAnswer.REFUSED, failure=_STOP), Settle(_STOP)),
    ("pending_waits", _facts("claimed", final=_FinalAnswer.PENDING), Wait()),
    ("pending_while_failing_waits", _facts("failing", final=_FinalAnswer.PENDING), Wait()),
    ("waiting_without_lease_waits", _facts("waiting"), Wait()),
    ("waiting_leased_by_another_settles", _facts("waiting", holder=_Holder.OTHER), Settle(_PUBLICATION_INTERRUPTED)),
    (
        "waiting_leased_by_this_runtime_settles",
        _facts("waiting", holder=_Holder.SELF),
        Settle(_PUBLICATION_INTERRUPTED),
    ),
    ("ready_resumes", _facts("ready"), Resume()),
    ("claimed_releases", _facts("claimed", holder=_Holder.OTHER), Release(_SHUTDOWN)),
    ("claimed_by_this_runtime_releases", _facts("claimed", holder=_Holder.SELF), Release(_SHUTDOWN)),
    ("claimed_still_running_releases", _facts("claimed", claim_outcome=SpanOutcome.LOST), Release(_SHUTDOWN)),
    ("claim_failed_settles", _facts("claimed", claim_outcome=SpanOutcome.FAILED), Settle(_SHUTDOWN)),
    ("claim_cancelled_settles", _facts("claimed", claim_outcome=SpanOutcome.CANCELLED), Settle(_SHUTDOWN)),
    ("claim_suppressed_settles", _facts("claimed", claim_outcome=SpanOutcome.SUPPRESSED), Settle(_SHUTDOWN)),
    ("shutdown_releases", _facts("failing", failure=_SHUTDOWN), Release(_SHUTDOWN)),
    (
        "shutdown_whose_claim_failed_settles",
        _facts("failing", failure=_SHUTDOWN, claim_outcome=SpanOutcome.FAILED),
        Settle(_SHUTDOWN),
    ),
    *[
        (f"{failure.kind}_settles", _facts("failing", failure=failure), Settle(failure))
        for failure in _FAILURES
        if failure.kind is not ApprovalFailureKind.SHUTDOWN
    ],
]


@pytest.mark.parametrize(("facts", "step"), [row[1:] for row in DISPATCH_ROWS], ids=[row[0] for row in DISPATCH_ROWS])
def test_dispatch(facts: ContinuationFacts, step: Step) -> None:
    """The owning bot's source worker finishes, waits for, resumes, settles, or releases the paused reply."""
    assert next_step(facts, Dispatch()) == step


RESUMED_ROWS: list[tuple[str, ContinuationFacts, Resumed, Step]] = [
    (
        "completed_finishes",
        _facts("claimed", final=_FinalAnswer.ANSWERED),
        Resumed("completed"),
        Finish(finalize=False),
    ),
    ("completed_without_final_finishes", _facts("claimed"), Resumed("completed"), Finish(finalize=False)),
    ("failed_settles_its_failure", _facts("claimed"), Resumed("failed", _STOP), Settle(_STOP)),
    (
        "failed_with_refused_final_settles",
        _facts("claimed", final=_FinalAnswer.REFUSED),
        Resumed("failed", _ERROR),
        Settle(_ERROR),
    ),
    ("suspended_waits", _facts("waiting"), Resumed("suspended", advanced=True), Wait()),
    ("advanced_to_ready_resumes", _facts("ready"), Resumed("suspended", advanced=True), Resume()),
    ("not_advanced_ready_waits", _facts("ready"), Resumed("suspended"), Wait()),
    ("suspended_with_queued_answer_waits", _facts("claimed", final=_FinalAnswer.PENDING), Resumed("suspended"), Wait()),
    (
        "suspended_with_refused_answer_waits",
        _facts("claimed", final=_FinalAnswer.REFUSED),
        Resumed("suspended"),
        Wait(),
    ),
    ("raised_settles", _facts("claimed"), Resumed("raised", _ERROR), Settle(_ERROR)),
    (
        "raised_with_refused_final_settles",
        _facts("claimed", final=_FinalAnswer.REFUSED),
        Resumed("raised", _ERROR),
        Settle(_ERROR),
    ),
    (
        "raised_after_answer_finalizes",
        _facts("claimed", final=_FinalAnswer.ANSWERED),
        Resumed("raised", _ERROR),
        Finish(finalize=True),
    ),
    (
        "raised_after_ending_finalizes",
        _facts("failing", final=_FinalAnswer.ENDED),
        Resumed("raised", _ERROR),
        Finish(finalize=True),
    ),
    (
        "raised_with_queued_answer_waits",
        _facts("claimed", final=_FinalAnswer.PENDING),
        Resumed("raised", _ERROR),
        Wait(),
    ),
    ("cancelled_fences", _facts("claimed"), Resumed("cancelled", _STOP), Fence(_STOP)),
    ("cancelled_by_shutdown_fences", _facts("claimed"), Resumed("cancelled", _SHUTDOWN), Fence(_SHUTDOWN)),
    (
        "cancelled_with_refused_final_fences",
        _facts("claimed", final=_FinalAnswer.REFUSED),
        Resumed("cancelled", _STOP),
        Fence(_STOP),
    ),
    (
        "cancelled_after_answer_finalizes",
        _facts("claimed", final=_FinalAnswer.ANSWERED),
        Resumed("cancelled", _STOP),
        Finish(finalize=True),
    ),
    (
        "cancelled_with_queued_answer_waits",
        _facts("claimed", final=_FinalAnswer.PENDING),
        Resumed("cancelled", _STOP),
        Wait(),
    ),
]


@pytest.mark.parametrize(
    ("facts", "trigger", "step"),
    [row[1:] for row in RESUMED_ROWS],
    ids=[row[0] for row in RESUMED_ROWS],
)
def test_resumed(facts: ContinuationFacts, trigger: Resumed, step: Step) -> None:
    """A live resume's ending finishes, settles, fences, waits for, or resumes the paused reply."""
    assert next_step(facts, trigger) == step


OWNER_ROWS: list[tuple[str, ContinuationFacts, Step]] = [
    ("without_final_discards", _facts("waiting"), DiscardUnavailable()),
    ("claimed_without_final_discards", _facts("claimed"), DiscardUnavailable()),
    ("pending_waits", _facts("claimed", final=_FinalAnswer.PENDING), Wait()),
    ("answered_finalizes", _facts("claimed", final=_FinalAnswer.ANSWERED), Finish(finalize=True)),
    ("ended_finishes", _facts("failing", final=_FinalAnswer.ENDED), Finish(finalize=False)),
    (
        "refused_settles_with_the_refusal",
        _facts("claimed", final=_FinalAnswer.REFUSED),
        Settle(ApprovalFailure(ApprovalFailureKind.ERROR, _REFUSAL)),
    ),
    ("refused_while_failing_settles", _facts("failing", final=_FinalAnswer.REFUSED, failure=_STOP), Settle(_STOP)),
]


@pytest.mark.parametrize(("facts", "step"), [row[1:] for row in OWNER_ROWS], ids=[row[0] for row in OWNER_ROWS])
def test_owner_unavailable(facts: ContinuationFacts, step: Step) -> None:
    """A paused reply whose entity is gone is discarded with a notice, or ends through its final answer."""
    assert next_step(facts, OwnerUnavailable()) == step


def _exit(*, suspended: bool = False, live: bool = True, shutting_down: bool = False) -> CliExit:
    return CliExit(suspended=suspended, live=live, shutting_down=shutting_down, failure=_ERROR)


CLI_ROWS: list[tuple[str, ContinuationFacts, CliExit, Step]] = [
    ("suspended_ready_wakes", _facts("ready", cli=True), _exit(suspended=True), WakeSources()),
    ("suspended_waiting_waits", _facts("waiting", cli=True), _exit(suspended=True), Wait()),
    ("suspended_failing_waits", _facts("failing", cli=True), _exit(suspended=True), Wait()),
    ("recovered_lifecycle_waits", _facts("claimed", holder=_Holder.SELF, cli=True), _exit(live=False), Wait()),
    ("standard_reply_waits", _facts("claimed", holder=_Holder.SELF), _exit(), Wait()),
    ("claimed_by_another_runtime_waits", _facts("claimed", holder=_Holder.OTHER, cli=True), _exit(), Wait()),
    ("claimed_without_holder_waits", _facts("claimed", cli=True), _exit(), Wait()),
    ("failing_settles", _facts("failing", failure=_STOP, cli=True), _exit(), Settle(_STOP)),
    (
        "failing_with_ended_final_settles",
        _facts("failing", final=_FinalAnswer.ENDED, failure=_STOP, cli=True),
        _exit(),
        Settle(_STOP),
    ),
    ("failing_during_shutdown_waits", _facts("failing", cli=True), _exit(shutting_down=True), Wait()),
    (
        "failing_answered_finishes",
        _facts("failing", final=_FinalAnswer.ANSWERED, cli=True),
        _exit(),
        Finish(finalize=False),
    ),
    (
        "answered_finishes",
        _facts("claimed", holder=_Holder.SELF, final=_FinalAnswer.ANSWERED, cli=True),
        _exit(),
        Finish(finalize=False),
    ),
    (
        "ended_finishes",
        _facts("claimed", holder=_Holder.SELF, final=_FinalAnswer.ENDED, cli=True),
        _exit(),
        Finish(finalize=False),
    ),
    (
        "pending_finishes",
        _facts("claimed", holder=_Holder.SELF, final=_FinalAnswer.PENDING, cli=True),
        _exit(),
        Finish(finalize=False),
    ),
    (
        "refused_fences",
        _facts("claimed", holder=_Holder.SELF, final=_FinalAnswer.REFUSED, cli=True),
        _exit(),
        Fence(_ERROR),
    ),
    ("no_final_fences", _facts("claimed", holder=_Holder.SELF, cli=True), _exit(), Fence(_ERROR)),
    ("waiting_fences", _facts("waiting", cli=True), _exit(), Fence(_ERROR)),
    (
        "no_final_during_shutdown_waits",
        _facts("claimed", holder=_Holder.SELF, cli=True),
        _exit(shutting_down=True),
        Wait(),
    ),
]


@pytest.mark.parametrize(("facts", "trigger", "step"), [row[1:] for row in CLI_ROWS], ids=[row[0] for row in CLI_ROWS])
def test_cli_exit(facts: ContinuationFacts, trigger: CliExit, step: Step) -> None:
    """A minimal-mode response's approval scope ends: wake, finish, settle, fence, or leave its paused reply."""
    assert next_step(facts, trigger) == step


@pytest.mark.parametrize("facts", _EVERY_FACTS, ids=repr)
def test_cli_exit_rows_come_before_every_other_rule(facts: ContinuationFacts) -> None:
    """A minimal-mode exit never resumes, releases, or discards, whatever the paused reply's final answer."""
    for trigger in (_exit(), _exit(suspended=True), _exit(live=False), _exit(shutting_down=True)):
        assert not isinstance(next_step(facts, trigger), (Resume, Release, DiscardUnavailable))


def _delivery(*, acknowledged: bool, refused: bool) -> MatrixDelivery:
    return MatrixDelivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        membership_epoch=0,
        thread_id=None,
        transaction_id="txn",
        payload={},
        edits_event_id="$waiting",
        acknowledged_event_id="$final" if acknowledged else None,
        created_at_ns=1,
        permanent_failure_reason=_REFUSAL if refused else None,
    )


@pytest.mark.parametrize(
    ("delivery", "span_outcome", "answer"),
    [
        (None, None, _FinalAnswer.NONE),
        (_delivery(acknowledged=False, refused=False), SpanOutcome.COMPLETED, _FinalAnswer.PENDING),
        (_delivery(acknowledged=False, refused=False), SpanOutcome.FAILED, _FinalAnswer.PENDING),
        (_delivery(acknowledged=False, refused=True), SpanOutcome.COMPLETED, _FinalAnswer.REFUSED),
        (_delivery(acknowledged=True, refused=False), SpanOutcome.COMPLETED, _FinalAnswer.ANSWERED),
        (_delivery(acknowledged=True, refused=False), SpanOutcome.CANCELLED, _FinalAnswer.ENDED),
        (_delivery(acknowledged=True, refused=False), None, _FinalAnswer.ENDED),
    ],
)
def test_final_answer(delivery: MatrixDelivery | None, span_outcome: SpanOutcome | None, answer: _FinalAnswer) -> None:
    """The final answer is none, queued, acknowledged with its span completed or not, or refused for good."""
    assert _final_answer(delivery, span_outcome) is answer


def test_a_refusal_is_the_settlement_reason_only_without_a_failure() -> None:
    """An unavailable owner's refused final answer settles with the refusal unless the paused reply already fails."""
    refused = _facts("claimed", final=_FinalAnswer.REFUSED)
    assert next_step(refused, OwnerUnavailable()) == Settle(ApprovalFailure(ApprovalFailureKind.ERROR, _REFUSAL))
    assert next_step(replace(refused, state="failing", failure=_STOP), OwnerUnavailable()) == Settle(_STOP)
