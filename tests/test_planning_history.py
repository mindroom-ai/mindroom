"""Tests for the policy-grade view of a turn's thread history."""

from __future__ import annotations

from mindroom.conversation_resolver import MessageContext
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result


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


def _context(
    history: ThreadHistoryResult,
    *,
    is_thread: bool = True,
    requires_model_history_refresh: bool = False,
) -> MessageContext:
    """Return a context whose thread read returned this history."""
    return MessageContext(
        am_i_mentioned=False,
        is_thread=is_thread,
        thread_id="$thread:localhost" if is_thread else None,
        thread_history=history,
        mentioned_agents=[],
        has_non_agent_mentions=False,
        requires_model_history_refresh=requires_model_history_refresh,
    )


def test_planning_history_hides_degraded_history() -> None:
    """A degraded read is not proof for policy decisions, even when it reached the start of the thread."""
    history = thread_history_result(
        [_message("$event:localhost")],
        is_full_history=True,
        diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True},
    )
    context = _context(history)

    assert context.planning_thread_history == ()
    assert context.planning_thread_history_unavailable is True


def test_planning_history_keeps_complete_history() -> None:
    """Complete healthy histories pass through unchanged for policy decisions."""
    message = _message("$event:localhost")
    context = _context(thread_history_result([message], is_full_history=True))

    assert context.planning_thread_history == (message,)
    assert context.planning_thread_history_unavailable is False


def test_message_context_marks_partial_planning_history_unavailable() -> None:
    """Partial thread context must fail closed instead of behaving like an empty thread."""
    message = _message("$event:localhost")
    context = _context(
        thread_history_result([message], is_full_history=False),
        requires_model_history_refresh=True,
    )

    assert context.planning_thread_history == ()
    assert context.planning_thread_history_unavailable is True


def test_room_level_context_has_no_thread_history_to_be_unavailable() -> None:
    """Only a thread can fail closed for missing history."""
    context = _context(thread_history_result([_message("$event:localhost")], is_full_history=False), is_thread=False)

    assert context.planning_thread_history_unavailable is False
