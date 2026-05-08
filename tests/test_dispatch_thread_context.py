"""Tests for dispatch-local thread context helpers."""

from __future__ import annotations

from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_thread_context import (
    DispatchThreadContext,
    context_with_dispatch_thread_context,
    planning_history_for,
    room_level_target,
)
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from mindroom.message_target import MessageTarget


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
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
        },
    )

    assert planning_history_for(history) == ()


def test_planning_history_for_hides_healthy_partial_history() -> None:
    """Healthy dispatch snapshots can prove targets but are not policy-grade history."""
    message = _message("$event:localhost")
    history = thread_history_result([message], is_full_history=False)

    assert planning_history_for(history) == ()


def test_planning_history_for_keeps_complete_history() -> None:
    """Complete healthy histories pass through unchanged for policy decisions."""
    message = _message("$event:localhost")
    history = thread_history_result([message], is_full_history=True)

    assert planning_history_for(history) == (message,)


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


def test_room_level_target_strips_source_and_resolved_thread_ids() -> None:
    """Room demotion must remove both authored and resolved thread identity."""
    threaded_target = MessageTarget.resolve(
        "!room:localhost",
        "$source-thread:localhost",
        "$reply:localhost",
    ).with_thread_root("$resolved-thread:localhost")

    target = room_level_target(threaded_target)

    assert target.source_thread_id is None
    assert target.resolved_thread_id is None
    assert target.reply_to_event_id == "$reply:localhost"


def test_message_target_identifies_new_root_target_shape() -> None:
    """Only thread-start delivery targets should be treated as new roots."""
    target = MessageTarget.resolve(
        "!room:localhost",
        None,
        "$event:localhost",
        thread_start_root_event_id="$event:localhost",
    )
    existing_thread = MessageTarget.resolve("!room:localhost", "$event:localhost", "$event:localhost")

    assert target.represents_new_thread_root is True
    assert existing_thread.represents_new_thread_root is False


def test_context_with_dispatch_thread_context_propagates_replay_guard_history() -> None:
    """Replay history may stabilize on MessageContext, but dispatch-only flags stay local."""
    thread_message = _message("$thread-message:localhost")
    replay_message = _message("$replay-message:localhost")
    context = MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=(),
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    dispatch_context = DispatchThreadContext(
        stable_target=MessageTarget.resolve("!room:localhost", "$thread:localhost", "$event:localhost"),
        candidate_thread_root_id=None,
        thread_history=(thread_message,),
        requires_model_history_refresh=True,
        replay_guard_history=(replay_message,),
        replay_guard_degraded=True,
    )

    stabilized = context_with_dispatch_thread_context(context, dispatch_context)

    assert stabilized.is_thread is True
    assert stabilized.thread_id == "$thread:localhost"
    assert stabilized.thread_history == (thread_message,)
    assert stabilized.replay_guard_history == (replay_message,)
    assert stabilized.replay_guard_history_degraded is False
    assert stabilized.requires_model_history_refresh is True


def test_context_with_dispatch_thread_context_hides_new_root_target_from_policy() -> None:
    """A new root delivery target is not existing thread context for policy."""
    context = MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=(),
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    dispatch_context = DispatchThreadContext(
        stable_target=MessageTarget.resolve(
            "!room:localhost",
            None,
            "$event:localhost",
            thread_start_root_event_id="$event:localhost",
        ),
        candidate_thread_root_id=None,
        thread_history=thread_history_result([_message("$event:localhost")], is_full_history=False),
        requires_model_history_refresh=True,
        replay_guard_history=(),
        replay_guard_degraded=False,
    )

    stabilized = context_with_dispatch_thread_context(context, dispatch_context)

    assert stabilized.is_thread is False
    assert stabilized.thread_id is None
    assert stabilized.thread_history == []
    assert stabilized.requires_model_history_refresh is False
