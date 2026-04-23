"""Canonical policy tests for terminal delivery states."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome

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


def _outcome(
    state: str,
    *,
    terminal_status: str,
    final_visible_event_id: str | None = None,
    last_physical_stream_event_id: str | None = None,
    final_visible_body: str | None = None,
    delivery_kind: str | None = None,
    failure_reason: str | None = None,
) -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        state=state,
        terminal_status=terminal_status,
        final_visible_event_id=final_visible_event_id,
        last_physical_stream_event_id=last_physical_stream_event_id,
        final_visible_body=final_visible_body,
        delivery_kind=delivery_kind,
        failure_reason=failure_reason,
    )


def _final_visible_delivery() -> FinalDeliveryOutcome:
    return _outcome(
        "final_visible_delivery",
        terminal_status="completed",
        final_visible_event_id="$final",
        final_visible_body="hello",
        delivery_kind="sent",
    )


def _kept_prior_visible_stream_after_completed_terminal_failure() -> FinalDeliveryOutcome:
    return _outcome(
        "kept_prior_visible_stream_after_completed_terminal_failure",
        terminal_status="completed",
        last_physical_stream_event_id="$stream",
        final_visible_body="partial",
    )


def _kept_prior_visible_stream_after_cancel() -> FinalDeliveryOutcome:
    return _outcome(
        "kept_prior_visible_stream_after_cancel",
        terminal_status="cancelled",
        last_physical_stream_event_id="$stream",
        final_visible_body="partial",
    )


def _kept_prior_visible_stream_after_error() -> FinalDeliveryOutcome:
    return _outcome(
        "kept_prior_visible_stream_after_error",
        terminal_status="error",
        last_physical_stream_event_id="$stream",
        final_visible_body="partial",
    )


def _cancelled_with_visible_response() -> FinalDeliveryOutcome:
    return _outcome(
        "cancelled_with_visible_response",
        terminal_status="cancelled",
        final_visible_event_id="$existing",
        final_visible_body="existing reply",
    )


def _cancelled_with_visible_note() -> FinalDeliveryOutcome:
    return _outcome(
        "cancelled_with_visible_note",
        terminal_status="cancelled",
        final_visible_event_id="$cancel-note",
        final_visible_body="Cancelled.",
        delivery_kind="edited",
    )


def _cancelled_without_visible_response() -> FinalDeliveryOutcome:
    return _outcome("cancelled_without_visible_response", terminal_status="cancelled")


def _suppressed_without_visible_response() -> FinalDeliveryOutcome:
    return _outcome("suppressed_without_visible_response", terminal_status="completed")


def _kept_prior_visible_response_after_suppression() -> FinalDeliveryOutcome:
    return _outcome(
        "kept_prior_visible_response_after_suppression",
        terminal_status="completed",
        final_visible_event_id="$existing",
        final_visible_body="existing reply",
    )


def _suppressed_redacted() -> FinalDeliveryOutcome:
    return _outcome(
        "suppressed_redacted",
        terminal_status="completed",
        last_physical_stream_event_id="$suppressed",
    )


def _suppression_cleanup_failed() -> FinalDeliveryOutcome:
    return _outcome(
        "suppression_cleanup_failed",
        terminal_status="error",
        last_physical_stream_event_id="$suppressed",
    )


def _kept_prior_visible_response_after_error() -> FinalDeliveryOutcome:
    return _outcome(
        "kept_prior_visible_response_after_error",
        terminal_status="error",
        final_visible_event_id="$existing",
        final_visible_body="existing reply",
    )


def _error_without_visible_response() -> FinalDeliveryOutcome:
    return _outcome("error_without_visible_response", terminal_status="error")


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
                should_mark_handled=False,
                retryable=True,
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
                should_register_interactive_follow_up=False,
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
                should_mark_handled=False,
                retryable=True,
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
                should_mark_handled=False,
                retryable=True,
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
            _kept_prior_visible_response_after_suppression,
            _PolicyExpectation(
                visible_response_event_id="$existing",
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
            id="kept_prior_visible_response_after_suppression",
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
            _kept_prior_visible_response_after_error,
            _PolicyExpectation(
                visible_response_event_id="$existing",
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
            id="kept_prior_visible_response_after_error",
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
