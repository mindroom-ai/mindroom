"""Dispatch-only Matrix thread context finalization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.conversation_resolver import MessageContext
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.event_info import EventInfo


@dataclass(frozen=True)
class DispatchThreadContext:
    """Thread evidence that may exist only while dispatch is being finalized."""

    stable_target: MessageTarget
    candidate_thread_root_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    requires_model_history_refresh: bool
    replay_guard_history: Sequence[ResolvedVisibleMessage]
    replay_guard_degraded: bool
    candidate_event_info: EventInfo | None = None


def planning_history_for(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> tuple[ResolvedVisibleMessage, ...]:
    """Return history only when it is complete enough for policy decisions."""
    if is_thread_history_degraded(thread_history):
        return ()
    return tuple(thread_history)


def room_level_target(target: MessageTarget) -> MessageTarget:
    """Return a true room-level target without preserving source thread identity."""
    return MessageTarget.resolve(
        room_id=target.room_id,
        thread_id=None,
        reply_to_event_id=target.reply_to_event_id,
        room_mode=True,
    )


def context_with_dispatch_thread_context(
    context: MessageContext,
    thread_context: DispatchThreadContext,
) -> MessageContext:
    """Return ``context`` with finalized stable thread state from dispatch evidence."""
    stable_thread_id = thread_context.stable_target.source_thread_id
    return replace(
        context,
        is_thread=stable_thread_id is not None,
        thread_id=stable_thread_id,
        thread_history=thread_context.thread_history if stable_thread_id is not None else [],
        replay_guard_history=thread_context.replay_guard_history,
        requires_model_history_refresh=(
            thread_context.requires_model_history_refresh if stable_thread_id is not None else False
        ),
    )
