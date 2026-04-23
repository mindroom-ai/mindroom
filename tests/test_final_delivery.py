"""Direct tests for the retained terminal delivery fields."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome


@dataclass(frozen=True)
class _Expectation:
    event_id: str | None
    is_visible_response: bool
    visible_response_event_id: str | None
    response_identity_event_id: str | None
    mark_handled: bool
    retryable: bool


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$final",
                is_visible_response=True,
                final_visible_body="hello",
                delivery_kind="sent",
                mark_handled=True,
            ),
            _Expectation("$final", True, "$final", "$final", True, False),
            id="completed-visible-delivery",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$stream",
                is_visible_response=True,
                final_visible_body="partial",
                mark_handled=True,
            ),
            _Expectation("$stream", True, "$stream", "$stream", True, False),
            id="completed-preserved-visible-stream",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id="$stream",
                is_visible_response=True,
                final_visible_body="partial",
                failure_reason="cancelled_by_user",
                retryable=True,
            ),
            _Expectation("$stream", True, "$stream", None, False, True),
            id="cancelled-visible-stream",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$stream",
                is_visible_response=True,
                final_visible_body="partial",
                failure_reason="terminal_update_failed",
                mark_handled=True,
            ),
            _Expectation("$stream", True, "$stream", "$stream", True, False),
            id="error-visible-stream",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id=None,
                failure_reason="suppressed_by_hook",
                mark_handled=True,
                suppressed=True,
            ),
            _Expectation(None, False, None, None, True, False),
            id="suppressed-without-visible-response",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$placeholder",
                is_visible_response=True,
                failure_reason="suppressed_by_hook",
                mark_handled=True,
            ),
            _Expectation("$placeholder", True, "$placeholder", "$placeholder", True, False),
            id="suppression-cleanup-failed",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id=None,
                failure_reason="delivery_failed",
                retryable=True,
            ),
            _Expectation(None, False, None, None, False, True),
            id="error-without-visible-response",
        ),
    ],
)
def test_final_delivery_outcomes_use_canonical_event_fields(
    outcome: FinalDeliveryOutcome,
    expected: _Expectation,
) -> None:
    """Call sites should rely on the canonical event id plus visible-response flag."""
    assert outcome.event_id == expected.event_id
    assert outcome.is_visible_response is expected.is_visible_response
    assert outcome.visible_response_event_id == expected.visible_response_event_id
    assert outcome.response_identity_event_id == expected.response_identity_event_id
    assert outcome.mark_handled is expected.mark_handled
    assert outcome.retryable is expected.retryable


def test_final_delivery_outcome_requires_event_id_for_visible_response() -> None:
    """Visible-response outcomes must carry the canonical event id."""
    with pytest.raises(ValueError, match="is_visible_response requires event_id"):
        FinalDeliveryOutcome(
            terminal_status="completed",
            event_id=None,
            is_visible_response=True,
        )


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
