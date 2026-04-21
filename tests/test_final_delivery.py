"""Canonical policy tests for terminal delivery states."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome, TurnDeliveryResolution

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class _PolicyExpectation:
    visible_response_event_id: str | None
    response_identity_event_id: str | None
    turn_completion_event_id: str | None
    emits_after_response: bool
    emits_cancelled_response: bool
    should_mark_handled: bool
    retryable: bool
    should_persist_response_identity: bool
    should_queue_thread_summary: bool
    should_register_interactive_follow_up: bool
    should_shield_late_failures: bool


def _final_visible_delivery() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.final_visible_delivery(
        final_visible_event_id="$final",
        final_visible_body="hello",
    )


def _kept_prior_visible_stream_after_completed_terminal_failure() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.keep_prior_visible_stream_after_completed_terminal_failure(
        last_physical_stream_event_id="$stream",
        final_visible_body="partial",
    )


def _kept_prior_visible_stream_after_cancel() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.keep_prior_visible_stream_after_cancel(
        last_physical_stream_event_id="$stream",
        final_visible_body="partial",
    )


def _kept_prior_visible_stream_after_error() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.keep_prior_visible_stream_after_error(
        last_physical_stream_event_id="$stream",
        final_visible_body="partial",
    )


def _cancelled_with_visible_response() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.cancelled_with_visible_response(
        final_visible_event_id="$existing",
        final_visible_body="existing reply",
    )


def _cancelled_with_visible_note() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.cancelled_with_visible_note(
        final_visible_event_id="$cancel-note",
        final_visible_body="Cancelled.",
    )


def _cancelled_without_visible_response() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.cancelled_without_visible_response()


def _suppressed_without_visible_response() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.suppressed_without_visible_response()


def _suppressed_redacted() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.suppressed_redacted(
        last_physical_stream_event_id="$suppressed",
    )


def _suppression_cleanup_failed() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.suppression_cleanup_failed(
        last_physical_stream_event_id="$suppressed",
    )


def _error_with_visible_response() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.error_with_visible_response(
        final_visible_event_id="$error",
        final_visible_body="Something went wrong.",
    )


def _error_without_visible_response() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome.error_without_visible_response()


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        pytest.param(
            _final_visible_delivery,
            _PolicyExpectation(
                visible_response_event_id="$final",
                response_identity_event_id="$final",
                turn_completion_event_id="$final",
                emits_after_response=True,
                emits_cancelled_response=False,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=True,
                should_queue_thread_summary=True,
                should_register_interactive_follow_up=True,
                should_shield_late_failures=True,
            ),
            id="final_visible_delivery",
        ),
        pytest.param(
            _kept_prior_visible_stream_after_completed_terminal_failure,
            _PolicyExpectation(
                visible_response_event_id="$stream",
                response_identity_event_id="$stream",
                turn_completion_event_id="$stream",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=True,
                should_queue_thread_summary=True,
                should_register_interactive_follow_up=True,
                should_shield_late_failures=True,
            ),
            id="kept_prior_visible_stream_after_completed_terminal_failure",
        ),
        pytest.param(
            _kept_prior_visible_stream_after_cancel,
            _PolicyExpectation(
                visible_response_event_id="$stream",
                response_identity_event_id=None,
                turn_completion_event_id="$stream",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=True,
            ),
            id="kept_prior_visible_stream_after_cancel",
        ),
        pytest.param(
            _kept_prior_visible_stream_after_error,
            _PolicyExpectation(
                visible_response_event_id="$stream",
                response_identity_event_id="$stream",
                turn_completion_event_id="$stream",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=True,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=True,
                should_shield_late_failures=True,
            ),
            id="kept_prior_visible_stream_after_error",
        ),
        pytest.param(
            _cancelled_with_visible_response,
            _PolicyExpectation(
                visible_response_event_id="$existing",
                response_identity_event_id=None,
                turn_completion_event_id="$existing",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=True,
            ),
            id="cancelled_with_visible_response",
        ),
        pytest.param(
            _cancelled_with_visible_note,
            _PolicyExpectation(
                visible_response_event_id="$cancel-note",
                response_identity_event_id=None,
                turn_completion_event_id="$cancel-note",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=True,
            ),
            id="cancelled_with_visible_note",
        ),
        pytest.param(
            _cancelled_without_visible_response,
            _PolicyExpectation(
                visible_response_event_id=None,
                response_identity_event_id=None,
                turn_completion_event_id=None,
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=False,
                retryable=True,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=False,
            ),
            id="cancelled_without_visible_response",
        ),
        pytest.param(
            _suppressed_without_visible_response,
            _PolicyExpectation(
                visible_response_event_id=None,
                response_identity_event_id=None,
                turn_completion_event_id=None,
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=False,
            ),
            id="suppressed_without_visible_response",
        ),
        pytest.param(
            _suppressed_redacted,
            _PolicyExpectation(
                visible_response_event_id=None,
                response_identity_event_id=None,
                turn_completion_event_id=None,
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=False,
            ),
            id="suppressed_redacted",
        ),
        pytest.param(
            _suppression_cleanup_failed,
            _PolicyExpectation(
                visible_response_event_id="$suppressed",
                response_identity_event_id=None,
                turn_completion_event_id="$suppressed",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=True,
            ),
            id="suppression_cleanup_failed",
        ),
        pytest.param(
            _error_with_visible_response,
            _PolicyExpectation(
                visible_response_event_id="$error",
                response_identity_event_id=None,
                turn_completion_event_id="$error",
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=True,
                retryable=False,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=True,
            ),
            id="error_with_visible_response",
        ),
        pytest.param(
            _error_without_visible_response,
            _PolicyExpectation(
                visible_response_event_id=None,
                response_identity_event_id=None,
                turn_completion_event_id=None,
                emits_after_response=False,
                emits_cancelled_response=True,
                should_mark_handled=False,
                retryable=True,
                should_persist_response_identity=False,
                should_queue_thread_summary=False,
                should_register_interactive_follow_up=False,
                should_shield_late_failures=False,
            ),
            id="error_without_visible_response",
        ),
    ],
)
def test_final_delivery_policy_table(
    builder: Callable[[], FinalDeliveryOutcome],
    expected: _PolicyExpectation,
) -> None:
    """Every terminal state should project policy from one canonical row."""
    outcome = builder()

    assert outcome.visible_response_event_id == expected.visible_response_event_id
    assert outcome.response_identity_event_id == expected.response_identity_event_id
    assert outcome.turn_completion_event_id == expected.turn_completion_event_id
    assert outcome.emits_after_response is expected.emits_after_response
    assert outcome.emits_cancelled_response is expected.emits_cancelled_response
    assert outcome.should_mark_handled is expected.should_mark_handled
    assert outcome.retryable is expected.retryable
    assert outcome.should_persist_response_identity is expected.should_persist_response_identity
    assert outcome.should_queue_thread_summary is expected.should_queue_thread_summary
    assert outcome.should_register_interactive_follow_up is expected.should_register_interactive_follow_up
    assert outcome.should_shield_late_failures is expected.should_shield_late_failures


def test_stream_transport_outcome_rejects_rendered_body_without_visible_state() -> None:
    """Rendered text without a visible-body state is not a valid transport snapshot."""
    with pytest.raises(ValueError, match="visible_body_state 'none' cannot carry a rendered_body"):
        StreamTransportOutcome(
            last_physical_stream_event_id=None,
            terminal_operation="none",
            terminal_result="not_attempted",
            terminal_status="completed",
            rendered_body="hello",
            visible_body_state="none",
        )


def test_stream_transport_outcome_accepts_placeholder_only_visible_state() -> None:
    """Placeholder-only visibility must remain distinct from visible body."""
    outcome = StreamTransportOutcome(
        last_physical_stream_event_id="$thinking",
        terminal_operation="edit",
        terminal_result="failed",
        terminal_status="completed",
        rendered_body="Thinking...",
        visible_body_state="placeholder_only",
        failure_reason="terminal_update_failed",
    )

    assert outcome.last_physical_stream_event_id == "$thinking"
    assert outcome.has_any_physical_stream_event is True
    assert outcome.has_rendered_visible_body is False


@pytest.mark.parametrize(
    ("outcome", "expected_visible", "expected_identity", "expected_completion", "expected_handled"),
    [
        pytest.param(
            _final_visible_delivery(),
            "$final",
            "$final",
            "$final",
            True,
            id="final_visible_delivery",
        ),
        pytest.param(
            _kept_prior_visible_stream_after_cancel(),
            "$stream",
            None,
            "$stream",
            True,
            id="kept_prior_visible_stream_after_cancel",
        ),
        pytest.param(
            _cancelled_with_visible_note(),
            "$cancel-note",
            None,
            "$cancel-note",
            True,
            id="cancelled_with_visible_note",
        ),
        pytest.param(
            _error_without_visible_response(),
            None,
            None,
            None,
            False,
            id="error_without_visible_response",
        ),
    ],
)
def test_turn_delivery_resolution_projects_from_policy_row(
    outcome: FinalDeliveryOutcome,
    expected_visible: str | None,
    expected_identity: str | None,
    expected_completion: str | None,
    expected_handled: bool,
) -> None:
    """Turn delivery resolution should be a pure projection of one policy row."""
    resolution = TurnDeliveryResolution.from_outcome(outcome)

    assert resolution.state == outcome.state
    assert resolution.visible_response_event_id == expected_visible
    assert resolution.response_identity_event_id == expected_identity
    assert resolution.turn_completion_event_id == expected_completion
    assert resolution.should_mark_handled is expected_handled
    assert resolution.retryable is outcome.retryable
    assert resolution.has_visible_output is (expected_visible is not None)
