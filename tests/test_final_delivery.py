"""Direct tests for a turn's typed ending and the delivery facts it reports."""

from __future__ import annotations

import pytest

from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.event_journal import ApprovalFailure
from mindroom.final_delivery import (
    AwaitingApproval,
    Cancelled,
    Completed,
    Failed,
    FailureCause,
    FinalDeliveryOutcome,
    Owed,
    StreamTransportOutcome,
    Suppressed,
    Suppression,
    TurnEnding,
    approval_failure,
    hook_failure_reason,
    outcome_label,
)
from mindroom.reply_lifecycle import ApprovalFailureKind


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, None),
        ([], None),
        ({}, None),
        ({"run_id": 12}, None),
        ({"run_id": ""}, None),
        ({"run_id": "run-final"}, "run-final"),
    ],
)
def test_final_delivery_normalizes_response_run_id(metadata: object, expected: str | None) -> None:
    """Malformed wire identities must never become response linkage."""
    outcome = FinalDeliveryOutcome(
        ending=Completed(),
        event_id="$response",
        extra_content={AI_RUN_METADATA_KEY: metadata},
    )
    assert outcome.response_run_id == expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            FinalDeliveryOutcome(
                ending=Completed(),
                event_id="$final",
                is_visible_response=True,
                final_visible_body="hello",
                delivery_kind="sent",
            ),
            True,
            id="successful-visible-body",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                ending=Failed(FailureCause.DELIVERY_FAILED),
                event_id="$existing",
                is_visible_response=True,
            ),
            False,
            id="preserved-existing-body",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                ending=Failed(FailureCause.DELIVERY_FAILED),
                event_id="$placeholder",
                is_visible_response=True,
                final_visible_body="Response delivery failed.",
                delivery_kind="edited",
            ),
            False,
            id="delivered-failure-note",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                ending=Completed(),
                event_id="$existing",
                is_visible_response=True,
                delivery_kind="edited",
            ),
            False,
            id="empty-existing-stream",
        ),
    ],
)
def test_final_delivery_outcome_identifies_substantive_delivery(
    outcome: FinalDeliveryOutcome,
    expected: bool,
) -> None:
    """Only a confirmed completed delivery with nonblank body text is substantive."""
    assert outcome.delivered_substantive_content is expected


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        pytest.param(Cancelled("user_stop"), "cancelled_by_user", id="stop"),
        pytest.param(Cancelled("sync_restart"), "sync_restart_cancelled", id="restart"),
        pytest.param(Cancelled("interrupted"), "interrupted", id="interrupted"),
        pytest.param(Suppressed(Suppression.HOOK), "suppressed_by_hook", id="hook-suppressed"),
        pytest.param(Suppressed(Suppression.SILENT_NO_REPORT), "silent_no_report", id="silent-no-report"),
        pytest.param(Suppressed(Suppression.EMPTY_PROMPT), "empty_prompt", id="empty-prompt"),
        pytest.param(Suppressed(Suppression.PARTICIPATION_DECLINED), None, id="declined-fires-no-hook"),
        pytest.param(Failed(FailureCause.ERROR, "boom with secret"), "error", id="error-never-shows-text"),
        pytest.param(Failed(FailureCause.DELIVERY_FAILED), "delivery_failed", id="delivery-failed"),
        pytest.param(Failed(FailureCause.NOTHING_TO_SHOW), "nothing_to_show", id="nothing-to-show"),
        pytest.param(Completed(), None, id="completed"),
        pytest.param(AwaitingApproval(), None, id="awaiting-approval"),
        pytest.param(Owed(), None, id="owed"),
    ],
)
def test_the_cancelled_hook_reason_is_a_fixed_code(ending: TurnEnding, expected: str | None) -> None:
    """``message:cancelled`` reports one code per ending, never an error's text."""
    assert hook_failure_reason(ending) == expected


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        pytest.param(Cancelled("user_stop"), ApprovalFailure(ApprovalFailureKind.USER_STOP), id="stop"),
        pytest.param(Cancelled("sync_restart"), ApprovalFailure(ApprovalFailureKind.RESTART), id="restart"),
        pytest.param(Cancelled("interrupted"), ApprovalFailure(ApprovalFailureKind.INTERRUPTED), id="interrupted"),
        pytest.param(
            Failed(FailureCause.ERROR, "tool crashed"),
            ApprovalFailure(ApprovalFailureKind.ERROR, "tool crashed"),
            id="error-keeps-detail",
        ),
        pytest.param(
            Failed(FailureCause.NOTHING_TO_SHOW),
            ApprovalFailure(ApprovalFailureKind.ERROR),
            id="other-failure-has-no-code",
        ),
        pytest.param(
            Suppressed(Suppression.HOOK),
            ApprovalFailure(ApprovalFailureKind.ERROR),
            id="hook-suppression-fails-without-a-code",
        ),
    ],
)
def test_an_ending_fails_its_approval_without_internal_codes(
    ending: Cancelled | Failed | Suppressed,
    expected: ApprovalFailure,
) -> None:
    """Only an error carries its detail to the approval's cards and note; every other ending shows fixed text."""
    assert approval_failure(ending) == expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(None, "no_visible_response", id="none"),
        pytest.param(FinalDeliveryOutcome(ending=Suppressed(Suppression.HOOK)), "suppressed", id="suppressed"),
        pytest.param(
            FinalDeliveryOutcome(
                ending=Cancelled("user_stop"),
                event_id="$e",
                is_visible_response=True,
                delivery_kind="edited",
            ),
            "cancelled",
            id="cancelled-beats-delivery-kind",
        ),
        pytest.param(FinalDeliveryOutcome(ending=Failed(FailureCause.ERROR)), "error", id="failed"),
        pytest.param(FinalDeliveryOutcome(ending=AwaitingApproval()), "suspended", id="awaiting"),
        pytest.param(FinalDeliveryOutcome(ending=Owed()), "suspended", id="owed"),
        pytest.param(
            FinalDeliveryOutcome(ending=Completed(), event_id="$e", is_visible_response=True, delivery_kind="sent"),
            "sent",
            id="completed-sent",
        ),
        pytest.param(
            FinalDeliveryOutcome(ending=Completed(), event_id="$e", is_visible_response=True),
            "visible_response_preserved",
            id="completed-kept",
        ),
        pytest.param(FinalDeliveryOutcome(ending=Completed()), "no_visible_response", id="completed-nothing"),
    ],
)
def test_outcome_label(outcome: FinalDeliveryOutcome | None, expected: str) -> None:
    """Pipeline timing labels each ending once."""
    assert outcome_label(outcome) == expected


@pytest.mark.parametrize(
    ("visible_body_state", "expected_event_id"),
    [
        ("visible_body", "$stream"),
        ("placeholder_only", None),
        ("none", None),
    ],
)
def test_stream_and_final_event_id_precedence_are_equivalent(
    visible_body_state: str,
    expected_event_id: str | None,
) -> None:
    """Streaming and blocking delivery expose the same final event-id precedence.

    A streamed event counts as the final response event only when it showed a
    substantive body, exactly like a blocking outcome counts its event only
    when it is a visible response.
    """
    stream_outcome = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        rendered_body="body",
        visible_body_state=visible_body_state,  # type: ignore[arg-type]
    )
    final_outcome = FinalDeliveryOutcome(
        ending=Completed(),
        event_id="$stream",
        is_visible_response=visible_body_state == "visible_body",
        final_visible_body="body" if visible_body_state == "visible_body" else None,
    )
    assert stream_outcome.visible_event_id == expected_event_id
    assert final_outcome.final_visible_event_id == expected_event_id
