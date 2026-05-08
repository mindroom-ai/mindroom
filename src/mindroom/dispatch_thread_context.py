"""Dispatch-only Matrix thread context finalization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.matrix.cache.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.conversation_resolver import MessageContext
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.message_target import MessageTarget


@dataclass(frozen=True)
class DispatchThreadContext:
    """Thread evidence that may exist only while dispatch is being finalized."""

    stable_target: MessageTarget
    candidate_thread_root_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    requires_model_history_refresh: bool
    replay_guard_history: Sequence[ResolvedVisibleMessage]
    replay_guard_degraded: bool


def planning_history_for(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> tuple[ResolvedVisibleMessage, ...]:
    """Return history only when it is complete enough for policy decisions."""
    if not isinstance(thread_history, ThreadHistoryResult):
        return ()
    if not thread_history.is_full_history or is_thread_history_degraded(thread_history):
        return ()
    return tuple(thread_history)


def planning_history_unavailable_for(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    requires_model_history_refresh: bool,
) -> bool:
    """Return whether hidden planning history is unavailable rather than known empty."""
    if is_thread_history_degraded(thread_history):
        return True
    if isinstance(thread_history, ThreadHistoryResult):
        return not thread_history.is_full_history
    # Plain sequences have no completeness signal, so visible content must not masquerade as policy-grade history.
    return requires_model_history_refresh or bool(thread_history)


def context_with_dispatch_thread_context(
    context: MessageContext,
    thread_context: DispatchThreadContext,
) -> MessageContext:
    """Return ``context`` with finalized stable thread state from dispatch evidence."""
    source_thread_id = thread_context.stable_target.source_thread_id
    stable_thread_id = None if thread_context.stable_target.represents_new_thread_root else source_thread_id
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
