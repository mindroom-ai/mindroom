"""Direct tests for the retained terminal delivery fields."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome


@dataclass(frozen=True)
class _Expectation:
    event_id: str | None
    is_visible_response: bool
    final_visible_event_id: str | None
    handled_response_event_id: str | None
    mark_handled: bool


def _handled_response_event_id(outcome: FinalDeliveryOutcome) -> str | None:
    return outcome.event_id if outcome.mark_handled and outcome.is_visible_response and not outcome.suppressed else None


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id="$waiting",
                is_visible_response=True,
                final_visible_body="Waiting for approval",
                delivery_kind="sent",
            ),
            _Expectation("$waiting", True, "$waiting", None, False),
            id="suspended-visible-delivery",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$final",
                is_visible_response=True,
                final_visible_body="hello",
                delivery_kind="sent",
            ),
            _Expectation("$final", True, "$final", "$final", True),
            id="completed-visible-delivery",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id="$stream",
                is_visible_response=True,
                final_visible_body="partial",
                failure_reason="cancelled_by_user",
            ),
            _Expectation("$stream", True, "$stream", "$stream", True),
            id="cancelled-visible-stream",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$stream",
                is_visible_response=True,
                final_visible_body="partial",
                failure_reason="terminal_update_failed",
            ),
            _Expectation("$stream", True, "$stream", "$stream", True),
            id="error-visible-stream",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$placeholder",
                is_visible_response=True,
                failure_reason="suppressed_by_hook",
                suppressed=True,
            ),
            _Expectation("$placeholder", True, "$placeholder", None, False),
            id="suppression-cleanup-failed",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id=None,
                failure_reason="delivery_failed",
            ),
            _Expectation(None, False, None, None, False),
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
    assert outcome.final_visible_event_id == expected.final_visible_event_id
    assert _handled_response_event_id(outcome) == expected.handled_response_event_id
    assert outcome.mark_handled is expected.mark_handled


def test_final_delivery_outcome_cancelled_for_empty_prompt_is_not_retryable() -> None:
    """Empty prompt cancellation should not mark the source turn handled."""
    outcome = FinalDeliveryOutcome.cancelled_for_empty_prompt()

    assert outcome.terminal_status == "cancelled"
    assert outcome.failure_reason == "empty_prompt"
    assert outcome.mark_handled is False


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
            ),
            True,
            id="successful-visible-body",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$existing",
                is_visible_response=True,
                failure_reason="delivery_failed",
            ),
            False,
            id="preserved-existing-body",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$placeholder",
                is_visible_response=True,
                final_visible_body="Response delivery failed.",
                delivery_kind="edited",
                failure_reason="delivery_failed",
            ),
            False,
            id="delivered-failure-note",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="completed",
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
    """Only a confirmed delivery with nonblank body text is substantive."""
    assert outcome.delivered_substantive_content is expected


def test_stream_transport_outcome_accepts_placeholder_only_visible_state() -> None:
    """Placeholder-only visibility must remain distinct from visible body."""
    outcome = StreamTransportOutcome(
        last_physical_stream_event_id="$thinking",
        terminal_status="completed",
        rendered_body="Thinking...",
        visible_body_state="placeholder_only",
        failure_reason="terminal_update_failed",
    )

    assert outcome.last_physical_stream_event_id == "$thinking"
    assert outcome.visible_body_state == "placeholder_only"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$placeholder",
                is_visible_response=True,
                cancel_source="sync_restart",
                failure_reason="failed to redact cancelled placeholder",
            ),
            "sync_restart",
            id="explicit-provenance-wins-on-error-outcome",
        ),
        pytest.param(
            FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=None,
                cancel_source="user_stop",
                failure_reason="cancelled_by_user",
            ),
            "user_stop",
            id="explicit-provenance-wins-when-cancelled",
        ),
        pytest.param(
            FinalDeliveryOutcome(terminal_status="cancelled", event_id=None, failure_reason="sync_restart_cancelled"),
            "sync_restart",
            id="derived-from-persisted-reason",
        ),
        pytest.param(
            FinalDeliveryOutcome(terminal_status="cancelled", event_id=None, failure_reason="interrupted"),
            "interrupted",
            id="unknown-provenance-falls-back-to-interrupted",
        ),
        pytest.param(
            FinalDeliveryOutcome(terminal_status="completed", event_id="$final", is_visible_response=True),
            None,
            id="no-cancellation-provenance-on-completed",
        ),
        pytest.param(
            FinalDeliveryOutcome(terminal_status="error", event_id=None, failure_reason="delivery_failed"),
            None,
            id="no-cancellation-provenance-on-plain-error",
        ),
    ],
)
def test_final_delivery_outcome_resolves_cancel_source_precedence(
    outcome: FinalDeliveryOutcome,
    expected: str | None,
) -> None:
    """Explicit provenance wins, then the persisted failure reason, then interrupted."""
    assert outcome.resolved_cancel_source == expected


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
        terminal_status="completed",
        rendered_body="body",
        visible_body_state=visible_body_state,  # type: ignore[arg-type]
    )
    final_outcome = FinalDeliveryOutcome(
        terminal_status="completed",
        event_id="$stream",
        is_visible_response=visible_body_state == "visible_body",
        final_visible_body="body" if visible_body_state == "visible_body" else None,
    )
    assert stream_outcome.visible_event_id == expected_event_id
    assert final_outcome.final_visible_event_id == expected_event_id


def test_stream_transport_resolved_cancel_source_matches_final_delivery() -> None:
    """Transport and final delivery resolve identical cancellation provenance."""
    transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="cancelled",
        rendered_body=None,
        visible_body_state="none",
        failure_reason="sync_restart_cancelled",
    )
    final = FinalDeliveryOutcome(
        terminal_status="cancelled",
        event_id="$stream",
        is_visible_response=True,
        cancel_source="sync_restart",
        failure_reason="sync_restart_cancelled",
    )
    assert transport.resolved_cancel_source == final.resolved_cancel_source == "sync_restart"
    cancelled_only = StreamTransportOutcome(
        last_physical_stream_event_id=None,
        terminal_status="error",
        rendered_body=None,
        visible_body_state="none",
        failure_reason="stream_finalize_failed",
    )
    assert cancelled_only.resolved_cancel_source is None
