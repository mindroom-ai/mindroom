"""Resolve a forked thread as its parent's messages up to the fork point, then its own."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import FORK_CONTENT_KEY
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC, is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

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


def _root_reference(messages: Sequence[ResolvedVisibleMessage], thread_id: str) -> _ThreadForkReference | None:
    for message in messages:
        if message.event_id == thread_id:
            return _thread_fork_reference(message.content)
    return None


def is_thread_fork_root(content: Mapping[str, Any]) -> bool:
    """Return whether a message's content holds a valid fork reference, which makes it the root of a fork."""
    return _thread_fork_reference(content) is not None


def fork_parent_thread_id(history: Sequence[ResolvedVisibleMessage], thread_id: str) -> str | None:
    """Return the thread a thread was forked from, read from its root in ``history``, or None for a plain thread."""
    reference = _root_reference(history, thread_id)
    return None if reference is None else reference.thread_id


def fork_points(history: Sequence[ResolvedVisibleMessage]) -> dict[str, str]:
    """Map the last parent message of each fork a resolved history holds to the parent thread it was forked from."""
    return {
        reference.event_id: reference.thread_id
        for message in history
        if message.thread_id in (None, message.event_id)
        and (reference := _thread_fork_reference(message.content)) is not None
    }


def fork_parent_thread_ids(history: Sequence[ResolvedVisibleMessage], thread_id: str) -> frozenset[str]:
    """Return the parent threads whose messages a fork's resolved history holds, none for a plain thread."""
    thread_ids = {thread_id}
    reference = _root_reference(history, thread_id)
    while (
        reference is not None
        and reference.thread_id not in thread_ids
        and any(reference.thread_id in (m.event_id, m.thread_id) for m in history)
    ):
        thread_ids.add(reference.thread_id)
        reference = _root_reference(history, reference.thread_id)
    return frozenset(thread_ids - {thread_id})


async def with_fork_prefix(
    history: ThreadHistoryResult,
    *,
    room_id: str,
    thread_id: str,
    read_thread: Callable[[str], Awaitable[ThreadHistoryResult]],
    logger: BoundLogger,
) -> ThreadHistoryResult:
    """Return the history with the parent chain's messages up to each fork point in front."""
    reference = _root_reference(history, thread_id)
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
                room_id=room_id,
                thread_id=thread_id,
                parent_thread_id=reference.thread_id,
                depth=len(segments),
            )
            break
        visited.add(reference.thread_id)
        parent = await read_thread(reference.thread_id)
        # A parent read that is incomplete may only lack the fork point for now, so the fork is incomplete too.
        is_full_history = is_full_history and parent.is_full_history
        degraded = degraded or is_thread_history_degraded(parent)
        own = _own_messages(parent, reference.thread_id)
        cut = next((i for i, m in enumerate(own) if m.event_id == reference.event_id), None)
        if cut is None:
            if own:
                logger.info(
                    "Thread fork point missing from its parent",
                    room_id=room_id,
                    thread_id=thread_id,
                    parent_thread_id=reference.thread_id,
                    fork_point_event_id=reference.event_id,
                )
            break
        segments.append(own[: cut + 1])
        reference = _root_reference(own, reference.thread_id)

    prefix = [m for segment in reversed(segments) for m in segment]
    diagnostics = dict(history.diagnostics)
    if degraded:
        diagnostics[THREAD_HISTORY_DEGRADED_DIAGNOSTIC] = True
    return thread_history_result([*prefix, *history], is_full_history=is_full_history, diagnostics=diagnostics)
