"""Resolve a forked thread as its parent's messages up to the fork point, then its own."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeIs

from mindroom.constants import FORK_CONTENT_KEY
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC, is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from structlog.stdlib import BoundLogger

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_MAX_FORK_DEPTH = 8


class CrossRoomForkDecision(StrEnum):
    """Outcome of checking whether one cross-room fork hop may be read into the target room."""

    ALLOWED = "allowed"
    DENIED = "denied"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class CrossRoomForkAuthorization:
    """Pre-read cross-room authorization plus an optional post-cut segment check."""

    decision: CrossRoomForkDecision
    allow_segment: Callable[[Sequence[ResolvedVisibleMessage]], Awaitable[CrossRoomForkDecision]] | None = None


@dataclass(frozen=True, slots=True)
class _ThreadForkReference:
    """The optional parent room, parent thread, and last parent message a forked thread includes."""

    room_id: str | None
    thread_id: str
    event_id: str

    def resolved_room_id(self, default_room_id: str) -> str:
        """Return the parent room ID, resolving an omitted ``room_id`` in ``default_room_id``."""
        return default_room_id if self.room_id is None else self.room_id

    def is_cross_room(self, room_id: str) -> bool:
        """Return whether this reference targets a different room than ``room_id``."""
        return self.room_id is not None and self.room_id != room_id


@dataclass(frozen=True, slots=True)
class CrossRoomForkHop:
    """One cross-room hop evaluated before reading a parent segment into ``target_room_id``."""

    target_room_id: str
    child_room_id: str
    parent_room_id: str
    fork_root: ResolvedVisibleMessage
    fork_point_event_id: str


def _is_valid_room_id(value: object) -> TypeIs[str]:
    return isinstance(value, str) and len(value) > 1 and value.startswith("!") and not any(c.isspace() for c in value)


def _is_valid_event_id(value: object) -> TypeIs[str]:
    return isinstance(value, str) and len(value) > 1 and value.startswith("$") and not any(c.isspace() for c in value)


def thread_fork_reference(content: Mapping[str, Any]) -> _ThreadForkReference | None:
    """Parse the fork reference from a thread root's content, or None when absent or invalid."""
    value = content.get(FORK_CONTENT_KEY)
    if not isinstance(value, dict):
        return None
    thread_id = value.get("thread_id")
    event_id = value.get("event_id")
    if not (_is_valid_event_id(thread_id) and _is_valid_event_id(event_id)):
        return None
    room_id: str | None = None
    if "room_id" in value:
        raw_room_id = value["room_id"]
        if not _is_valid_room_id(raw_room_id):
            return None
        room_id = raw_room_id
    return _ThreadForkReference(room_id=room_id, thread_id=thread_id, event_id=event_id)


def _message_room_id(message: ResolvedVisibleMessage, default_room_id: str) -> str:
    return default_room_id if message.room_id is None else message.room_id


def _own_messages(
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    room_id: str | None = None,
) -> list[ResolvedVisibleMessage]:
    return [
        message
        for message in history
        if thread_id in (message.event_id, message.thread_id)
        and (room_id is None or message.room_id in (None, room_id))
    ]


def fork_root_message(
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    room_id: str | None = None,
) -> ResolvedVisibleMessage | None:
    """Return the root message of ``thread_id`` in ``history``, scoped to ``room_id`` when given."""
    for message in history:
        if message.event_id != thread_id:
            continue
        if room_id is not None and message.room_id not in (None, room_id):
            continue
        return message
    return None


def is_thread_fork_root(content: Mapping[str, Any]) -> bool:
    """Return whether a message's content holds a valid fork reference, which makes it the root of a fork."""
    return thread_fork_reference(content) is not None


def fork_reference(
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    room_id: str | None = None,
) -> _ThreadForkReference | None:
    """Return the fork reference on ``thread_id``'s root in ``history``, or None."""
    root = fork_root_message(history, thread_id, room_id=room_id)
    return None if root is None else thread_fork_reference(root.content)


def fork_parent_thread_id(
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    room_id: str | None = None,
) -> str | None:
    """Return the thread a thread was forked from, read from its root in ``history``, or None for a plain thread."""
    reference = fork_reference(history, thread_id, room_id=room_id)
    return None if reference is None else reference.thread_id


def fork_parent_scopes(
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    room_id: str,
) -> frozenset[tuple[str, str]]:
    """Return the ``(room_id, thread_id)`` parent scopes whose messages a fork's resolved history holds."""
    scopes: set[tuple[str, str]] = {(room_id, thread_id)}
    current_room_id = room_id
    reference = fork_reference(history, thread_id, room_id=room_id)
    while reference is not None:
        parent_room_id = reference.resolved_room_id(current_room_id)
        scope = (parent_room_id, reference.thread_id)
        if scope in scopes:
            break
        parent_messages = [
            message
            for message in history
            if _message_room_id(message, room_id) == parent_room_id
            and reference.thread_id in (message.event_id, message.thread_id)
        ]
        if not parent_messages:
            break
        scopes.add(scope)
        current_room_id = parent_room_id
        reference = fork_reference(parent_messages, reference.thread_id)
    return frozenset(scopes - {(room_id, thread_id)})


def _log_cross_room_denied(
    logger: BoundLogger,
    *,
    hop: CrossRoomForkHop,
    thread_id: str,
    parent_thread_id: str,
) -> None:
    logger.info(
        "Cross-room thread fork prefix denied",
        room_id=hop.target_room_id,
        thread_id=thread_id,
        child_room_id=hop.child_room_id,
        parent_room_id=hop.parent_room_id,
        parent_thread_id=parent_thread_id,
    )


async def _authorize_hop(
    authorize_cross_room: Callable[
        [CrossRoomForkHop],
        Awaitable[CrossRoomForkDecision | CrossRoomForkAuthorization],
    ]
    | None,
    hop: CrossRoomForkHop,
    *,
    thread_id: str,
    parent_thread_id: str,
    logger: BoundLogger,
) -> CrossRoomForkAuthorization:
    """Evaluate one cross-room hop, failing closed when no authorization gate was supplied."""
    if hop.parent_room_id == hop.target_room_id and hop.child_room_id == hop.parent_room_id:
        return CrossRoomForkAuthorization(CrossRoomForkDecision.ALLOWED)
    raw = CrossRoomForkDecision.DENIED if authorize_cross_room is None else await authorize_cross_room(hop)
    auth = raw if isinstance(raw, CrossRoomForkAuthorization) else CrossRoomForkAuthorization(raw)
    if auth.decision is CrossRoomForkDecision.DENIED:
        _log_cross_room_denied(logger, hop=hop, thread_id=thread_id, parent_thread_id=parent_thread_id)
    return auth


async def _read_authorized_parent_segment(
    *,
    read_thread: Callable[[str, str], Awaitable[ThreadHistoryResult]],
    auth: CrossRoomForkAuthorization,
    hop: CrossRoomForkHop,
    thread_id: str,
    parent_thread_id: str,
    logger: BoundLogger,
) -> tuple[list[ResolvedVisibleMessage] | None, bool, bool]:
    """Read, cut, and post-authorize one parent segment; returns ``(segment, is_full_history, degraded)``."""
    parent = await read_thread(hop.parent_room_id, parent_thread_id)
    own = _own_messages(parent, parent_thread_id, room_id=hop.parent_room_id)
    cut = next((i for i, m in enumerate(own) if m.event_id == hop.fork_point_event_id), None)
    if cut is None:
        if own:
            logger.info(
                "Thread fork point missing from its parent",
                room_id=hop.target_room_id,
                thread_id=thread_id,
                parent_room_id=hop.parent_room_id,
                parent_thread_id=parent_thread_id,
                fork_point_event_id=hop.fork_point_event_id,
            )
        # A parent read that is incomplete may only lack the fork point for now, so the fork is incomplete too.
        return None, parent.is_full_history, is_thread_history_degraded(parent)
    segment = [
        replace(message, room_id=hop.parent_room_id) if message.room_id != hop.parent_room_id else message
        for message in own[: cut + 1]
    ]
    if auth.allow_segment is not None:
        segment_decision = await auth.allow_segment(segment)
        if segment_decision is CrossRoomForkDecision.DEFERRED:
            return None, False, True
        if segment_decision is not CrossRoomForkDecision.ALLOWED:
            _log_cross_room_denied(logger, hop=hop, thread_id=thread_id, parent_thread_id=parent_thread_id)
            return None, True, False
    return segment, parent.is_full_history, is_thread_history_degraded(parent)


async def with_fork_prefix(
    history: ThreadHistoryResult,
    *,
    room_id: str,
    thread_id: str,
    read_thread: Callable[[str, str], Awaitable[ThreadHistoryResult]],
    authorize_cross_room: Callable[
        [CrossRoomForkHop],
        Awaitable[CrossRoomForkDecision | CrossRoomForkAuthorization],
    ]
    | None = None,
    logger: BoundLogger,
) -> ThreadHistoryResult:
    """Return the history with the parent chain's messages up to each fork point in front."""
    root = fork_root_message(history, thread_id, room_id=room_id)
    reference = None if root is None else thread_fork_reference(root.content)
    if reference is None or root is None:
        return history

    segments: list[list[ResolvedVisibleMessage]] = []
    visited: set[tuple[str, str]] = {(room_id, thread_id)}
    is_full_history = history.is_full_history
    degraded = False
    current_room_id = room_id
    current_root = root
    while reference is not None:
        parent_room_id = reference.resolved_room_id(current_room_id)
        if len(segments) >= _MAX_FORK_DEPTH or (parent_room_id, reference.thread_id) in visited:
            logger.warning(
                "Thread fork chain stopped",
                room_id=room_id,
                thread_id=thread_id,
                parent_room_id=parent_room_id,
                parent_thread_id=reference.thread_id,
                depth=len(segments),
            )
            break
        hop = CrossRoomForkHop(
            target_room_id=room_id,
            child_room_id=current_room_id,
            parent_room_id=parent_room_id,
            fork_root=current_root,
            fork_point_event_id=reference.event_id,
        )
        auth = await _authorize_hop(
            authorize_cross_room,
            hop,
            thread_id=thread_id,
            parent_thread_id=reference.thread_id,
            logger=logger,
        )
        if auth.decision is CrossRoomForkDecision.DEFERRED:
            is_full_history, degraded = False, True
            break
        if auth.decision is not CrossRoomForkDecision.ALLOWED:
            break
        visited.add((parent_room_id, reference.thread_id))
        segment, segment_full, segment_degraded = await _read_authorized_parent_segment(
            read_thread=read_thread,
            auth=auth,
            hop=hop,
            thread_id=thread_id,
            parent_thread_id=reference.thread_id,
            logger=logger,
        )
        is_full_history = is_full_history and segment_full
        degraded = degraded or segment_degraded
        if segment is None:
            break
        segments.append(segment)
        current_room_id = parent_room_id
        next_root = fork_root_message(segment, reference.thread_id, room_id=parent_room_id)
        current_root = next_root if next_root is not None else current_root
        reference = None if next_root is None else thread_fork_reference(next_root.content)

    prefix = [m for segment in reversed(segments) for m in segment]
    diagnostics = dict(history.diagnostics)
    if degraded:
        diagnostics[THREAD_HISTORY_DEGRADED_DIAGNOSTIC] = True
    return thread_history_result([*prefix, *history], is_full_history=is_full_history, diagnostics=diagnostics)
