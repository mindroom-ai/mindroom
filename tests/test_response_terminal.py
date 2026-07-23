"""Terminal response outcome helpers."""

from __future__ import annotations

import pytest

from mindroom.response_terminal import (
    PendingVisibleResponse,
    TerminalFailureStatus,
    build_terminal_stream_transport_outcome,
)


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


@pytest.mark.parametrize("terminal_status", ["cancelled", "error"])
def test_fresh_thinking_pre_delivery_failure_shape_is_placeholder_only(
    terminal_status: TerminalFailureStatus,
) -> None:
    """The fresh-thinking pre-delivery pending shape preserves placeholder cleanup.

    This is the shape the response runner builds when the attempt runner
    raised after sending a thinking message on a fresh turn: the tracked
    event doubles as the run message so the dangling placeholder is cleaned.
    """
    outcome = build_terminal_stream_transport_outcome(
        PendingVisibleResponse(
            tracked_event_id="$thinking",
            run_message_id="$thinking",
            existing_event_id=None,
            existing_event_is_placeholder=False,
        ),
        terminal_status=terminal_status,
        failure_reason="delivery failed",
        placeholder_body="Thinking...",
    )

    assert outcome.last_physical_stream_event_id == "$thinking"
    assert outcome.terminal_status == terminal_status
    assert outcome.failure_reason == "delivery failed"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"
