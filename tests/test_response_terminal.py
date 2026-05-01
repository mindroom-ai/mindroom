"""Terminal response outcome helpers."""

from __future__ import annotations

import pytest

from mindroom.response_terminal import PendingVisibleResponse, build_terminal_stream_transport_outcome


@pytest.mark.parametrize(
    ("pending", "expected_event_id", "expected_placeholder_only"),
    [
        pytest.param(
            PendingVisibleResponse(
                tracked_event_id="$visible",
                run_message_id="$thinking",
                existing_event_id=None,
                existing_event_is_placeholder=False,
            ),
            "$visible",
            False,
            id="preserves-non-placeholder-visible-stream",
        ),
        pytest.param(
            PendingVisibleResponse(
                tracked_event_id="$existing",
                run_message_id=None,
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
            ),
            "$existing",
            False,
            id="keeps-existing-visible-message-without-placeholder-body",
        ),
        pytest.param(
            PendingVisibleResponse(
                tracked_event_id=None,
                run_message_id="$thinking",
                existing_event_id=None,
                existing_event_is_placeholder=False,
            ),
            "$thinking",
            True,
            id="uses-new-thinking-placeholder",
        ),
        pytest.param(
            PendingVisibleResponse(
                tracked_event_id=None,
                run_message_id=None,
                existing_event_id="$existing",
                existing_event_is_placeholder=True,
            ),
            "$existing",
            True,
            id="uses-adopted-existing-placeholder",
        ),
        pytest.param(
            PendingVisibleResponse(
                tracked_event_id=None,
                run_message_id=None,
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
            ),
            None,
            False,
            id="ignores-non-placeholder-existing-message",
        ),
    ],
)
def test_terminal_stream_outcome_resolves_visible_placeholder_state(
    pending: PendingVisibleResponse,
    expected_event_id: str | None,
    expected_placeholder_only: bool,
) -> None:
    """Terminal failures should classify pending visible events in one place."""
    outcome = build_terminal_stream_transport_outcome(
        pending,
        terminal_status="error",
        failure_reason="delivery failed",
        placeholder_body="Thinking...",
    )

    assert outcome.last_physical_stream_event_id == expected_event_id
    assert outcome.terminal_status == "error"
    assert outcome.failure_reason == "delivery failed"
    assert outcome.rendered_body == ("Thinking..." if expected_placeholder_only else None)
    assert outcome.visible_body_state == ("placeholder_only" if expected_placeholder_only else "none")
