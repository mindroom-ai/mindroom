"""Tests for the policy-grade view of a turn's thread history."""

from __future__ import annotations

from mindroom.conversation_resolver import MessageContext, _planning_history_for
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
)
from mindroom.matrix.thread_history_result import thread_history_result


def _message(event_id: str) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage(
        sender="@user:localhost",
        body="hello",
        timestamp=1,
        event_id=event_id,
        content={"body": "hello"},
        thread_id="$thread:localhost",
        latest_event_id=event_id,
    )


def test_planning_history_for_hides_degraded_history() -> None:
    """Degraded thread snapshots are not proof for policy decisions."""
    history = thread_history_result(
        [_message("$event:localhost")],
        is_full_history=False,
        diagnostics={
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
        },
    )

    assert _planning_history_for(history) == ()


def test_planning_history_for_hides_healthy_partial_history() -> None:
    """Healthy dispatch snapshots can prove targets but are not policy-grade history."""
    message = _message("$event:localhost")
    history = thread_history_result([message], is_full_history=False)

    assert _planning_history_for(history) == ()


def test_planning_history_for_keeps_complete_history() -> None:
    """Complete healthy histories pass through unchanged for policy decisions."""
    message = _message("$event:localhost")
    history = thread_history_result([message], is_full_history=True)

    assert _planning_history_for(history) == (message,)


def test_message_context_marks_partial_planning_history_unavailable() -> None:
    """Partial thread context must fail closed instead of behaving like an empty thread."""
    message = _message("$event:localhost")
    context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$thread:localhost",
        thread_history=thread_history_result([message], is_full_history=False),
        mentioned_agents=[],
        has_non_agent_mentions=False,
        requires_model_history_refresh=True,
    )

    assert context.planning_thread_history == ()
    assert context.planning_thread_history_unavailable is True
