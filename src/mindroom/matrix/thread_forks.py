"""Resolve a forked thread as its parent's messages up to the fork point, then its own."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import FORK_CONTENT_KEY
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC, is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from structlog.stdlib import BoundLogger

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_MAX_FORK_DEPTH = 8


@dataclass(frozen=True, slots=True)
class _ThreadForkReference:
    """The parent thread and last parent message a forked thread includes."""

    thread_id: str
    event_id: str


def _thread_fork_reference(content: Mapping[str, Any]) -> _ThreadForkReference | None:
    """Parse the fork reference from a thread root's content, or None when it is not a fork."""
    value = content.get(FORK_CONTENT_KEY)
    if not isinstance(value, dict):
        return None
    thread_id = value.get("thread_id")
    event_id = value.get("event_id")
    if not (isinstance(thread_id, str) and isinstance(event_id, str)):
        return None
    if not (thread_id.startswith("$") and event_id.startswith("$")):
        return None
    return _ThreadForkReference(thread_id=thread_id, event_id=event_id)


def _own_messages(history: ThreadHistoryResult, thread_id: str) -> list[ResolvedVisibleMessage]:
    return [m for m in history if thread_id in (m.event_id, m.thread_id)]


def _root_reference(messages: list[ResolvedVisibleMessage], thread_id: str) -> _ThreadForkReference | None:
    for message in messages:
        if message.event_id == thread_id:
            return _thread_fork_reference(message.content)
    return None


async def with_fork_prefix(
    history: ThreadHistoryResult,
    *,
    thread_id: str,
    read_thread: Callable[[str], Awaitable[ThreadHistoryResult]],
    logger: BoundLogger,
) -> ThreadHistoryResult:
    """Return the history with the parent chain's messages up to each fork point in front."""
    reference = _root_reference(list(history), thread_id)
    if reference is None:
        return history

    segments: list[list[ResolvedVisibleMessage]] = []
    visited = {thread_id}
    is_full_history = history.is_full_history
    degraded = False
    while reference is not None:
        if len(segments) >= _MAX_FORK_DEPTH or reference.thread_id in visited:
            logger.warning(
                "Thread fork chain stopped",
                thread_id=thread_id,
                parent_thread_id=reference.thread_id,
                depth=len(segments),
            )
            break
        visited.add(reference.thread_id)
        parent = await read_thread(reference.thread_id)
        own = _own_messages(parent, reference.thread_id)
        cut = next((i for i, m in enumerate(own) if m.event_id == reference.event_id), None)
        if cut is None:
            break
        segments.append(own[: cut + 1])
        is_full_history = is_full_history and parent.is_full_history
        degraded = degraded or is_thread_history_degraded(parent)
        reference = _root_reference(own, reference.thread_id)

    if not segments:
        return history
    prefix = [m for segment in reversed(segments) for m in segment]
    diagnostics = dict(history.diagnostics)
    if degraded:
        diagnostics[THREAD_HISTORY_DEGRADED_DIAGNOSTIC] = True
    return thread_history_result([*prefix, *history], is_full_history=is_full_history, diagnostics=diagnostics)
