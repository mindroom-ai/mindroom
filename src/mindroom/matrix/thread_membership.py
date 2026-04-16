"""Canonical Matrix thread resolution.

Ownership map:
- canonical thread identity: this module
- scanned-event ordering and latest-thread-tail helpers: this module
- mutation/bookkeeping impact: `mindroom.matrix.thread_bookkeeping`
- tool-facing normalization: `mindroom.custom_tools.attachment_helpers`
"""

from __future__ import annotations

import heapq
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import nio

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

type ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type ThreadRootProofLookup = Callable[[str, str], Awaitable["ThreadRootProof"]]
type ThreadEventSourcesLookup = Callable[[str, str], Awaitable[tuple[Sequence[Mapping[str, object]], bool]]]
_MAX_THREAD_MEMBERSHIP_HOPS = 512


class SupportsEventId(Protocol):
    """Minimal protocol for snapshot entries used during thread-root checks."""

    event_id: str


type ThreadMessagesLookup = Callable[[str, str], Awaitable[Sequence[SupportsEventId]]]
type ThreadSnapshotLookup = Callable[[str, str], Awaitable[Sequence[SupportsEventId]]]
TThreadItem = TypeVar("TThreadItem")


class SupportsThreadMessageOrdering(Protocol):
    """Minimal protocol for timestamped thread messages ordered by event id."""

    event_id: str
    timestamp: int


class SupportsVisibleThreadMessage(Protocol):
    """Minimal protocol for visible-message cleanup thread analysis."""

    event_id: str
    visible_event_id: str
    thread_id: str | None
    timestamp: int
    latest_event_id: str
    content: Mapping[str, Any]


class ThreadRootProofState(Enum):
    """Outcome of proving whether one candidate event is a real thread root."""

    PROVEN = auto()
    NOT_A_THREAD_ROOT = auto()
    PROOF_UNAVAILABLE = auto()


@dataclass(frozen=True)
class ThreadRootProof:
    """Result of one thread-root proof attempt."""

    state: ThreadRootProofState
    error: Exception | None = None

    @classmethod
    def proven(cls) -> ThreadRootProof:
        """Return a successful root proof."""
        return cls(ThreadRootProofState.PROVEN)

    @classmethod
    def not_a_thread_root(cls) -> ThreadRootProof:
        """Return a definite non-thread-root result."""
        return cls(ThreadRootProofState.NOT_A_THREAD_ROOT)

    @classmethod
    def proof_unavailable(cls, error: Exception) -> ThreadRootProof:
        """Return one failed proof attempt without weakening caller policy."""
        return cls(ThreadRootProofState.PROOF_UNAVAILABLE, error=error)


class ThreadResolutionState(Enum):
    """Canonical thread-membership outcomes."""

    THREADED = auto()
    ROOM_LEVEL = auto()
    INDETERMINATE = auto()


@dataclass(frozen=True)
class ThreadResolution:
    """Canonical thread-membership result for one event."""

    state: ThreadResolutionState
    thread_id: str | None = None
    error: Exception | None = None

    @classmethod
    def threaded(cls, thread_id: str) -> ThreadResolution:
        """Return one positive thread-membership result."""
        return cls(ThreadResolutionState.THREADED, thread_id=thread_id)

    @classmethod
    def room_level(cls) -> ThreadResolution:
        """Return one definite room-level result."""
        return cls(ThreadResolutionState.ROOM_LEVEL)

    @classmethod
    def indeterminate(cls, error: Exception) -> ThreadResolution:
        """Return one unresolved result caused by proof failure."""
        return cls(ThreadResolutionState.INDETERMINATE, error=error)

    @property
    def is_threaded(self) -> bool:
        """Return whether the event was proven to belong to a thread."""
        return self.state is ThreadResolutionState.THREADED


class ThreadMembershipProofError(RuntimeError):
    """Raised when strict thread-membership resolution cannot prove one candidate root."""


class ThreadMembershipLookupError(RuntimeError):
    """Raised when related-event lookup cannot determine thread membership from available data."""


def _next_related_event_target(
    event_info: EventInfo,
    *,
    current_event_id: str,
) -> str | None:
    """Return the next related event to inspect."""
    return event_info.next_related_event_id(current_event_id)


@dataclass(frozen=True)
class ThreadMembershipAccess:
    """Repository-wide accessors used to resolve one event's thread membership."""

    lookup_thread_id: ThreadIdLookup
    fetch_event_info: EventInfoLookup
    prove_thread_root: ThreadRootProofLookup


def _thread_history_input_order(
    event_id: str,
    input_order_by_event_id: Mapping[str, int] | None,
) -> int:
    """Return the stable secondary ordering index for one event."""
    if input_order_by_event_id is None:
        return 0
    return input_order_by_event_id.get(event_id, 0)


def _build_same_timestamp_relation_graph(
    event_ids: set[str],
    *,
    related_event_id_by_event_id: Mapping[str, str],
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Return parent and child relationships for one same-timestamp group."""
    in_degree = dict.fromkeys(event_ids, 0)
    children_by_parent: dict[str, list[str]] = {}
    for event_id in in_degree:
        parent_event_id = related_event_id_by_event_id.get(event_id)
        if parent_event_id not in event_ids or parent_event_id == event_id:
            continue
        in_degree[event_id] += 1
        children_by_parent.setdefault(parent_event_id, []).append(event_id)
    return in_degree, children_by_parent


def _topologically_sort_same_timestamp_event_ids(
    event_ids: set[str],
    *,
    related_event_id_by_event_id: Mapping[str, str],
    input_order_by_event_id: Mapping[str, int] | None,
) -> list[str] | None:
    """Return one ancestry-aware order for equal-timestamp events when relations exist."""
    if len(event_ids) < 2:
        return None

    in_degree, children_by_parent = _build_same_timestamp_relation_graph(
        event_ids,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )
    if not children_by_parent:
        return None

    ready: list[tuple[int, str]] = []
    for event_id, degree in in_degree.items():
        if degree == 0:
            heapq.heappush(
                ready,
                (
                    _thread_history_input_order(event_id, input_order_by_event_id),
                    event_id,
                ),
            )

    ordered_event_ids: list[str] = []
    while ready:
        _input_order, event_id = heapq.heappop(ready)
        ordered_event_ids.append(event_id)
        for child_event_id in children_by_parent.get(event_id, ()):
            in_degree[child_event_id] -= 1
            if in_degree[child_event_id] == 0:
                heapq.heappush(
                    ready,
                    (
                        _thread_history_input_order(child_event_id, input_order_by_event_id),
                        child_event_id,
                    ),
                )

    if len(ordered_event_ids) != len(event_ids):
        remaining_event_ids = event_ids - set(ordered_event_ids)
        ordered_event_ids.extend(
            sorted(
                remaining_event_ids,
                key=lambda event_id: (
                    _thread_history_input_order(event_id, input_order_by_event_id),
                    event_id,
                ),
            ),
        )

    return ordered_event_ids


def _sort_same_timestamp_group(
    items: list[TThreadItem],
    *,
    event_id_getter: Callable[[TThreadItem], str],
    related_event_id_by_event_id: Mapping[str, str],
    input_order_by_event_id: Mapping[str, int] | None,
) -> list[TThreadItem]:
    """Keep same-timestamp parents ahead of descendants when relation ancestry is known."""
    if len(items) < 2:
        return items

    items_by_event_id = {event_id_getter(item): item for item in items}
    ordered_event_ids = _topologically_sort_same_timestamp_event_ids(
        set(items_by_event_id),
        related_event_id_by_event_id=related_event_id_by_event_id,
        input_order_by_event_id=input_order_by_event_id,
    )
    if ordered_event_ids is None:
        return items

    return [items_by_event_id[event_id] for event_id in ordered_event_ids]


def sort_thread_items_root_first(
    items: list[TThreadItem],
    *,
    thread_id: str,
    event_id_getter: Callable[[TThreadItem], str],
    timestamp_getter: Callable[[TThreadItem], int],
    input_order_by_event_id: Mapping[str, int] | None = None,
    related_event_id_by_event_id: Mapping[str, str] | None = None,
) -> None:
    """Keep the thread root first, then order the remaining items chronologically."""
    items.sort(
        key=lambda item: (
            timestamp_getter(item),
            _thread_history_input_order(event_id_getter(item), input_order_by_event_id),
            event_id_getter(item),
        ),
    )
    if related_event_id_by_event_id is not None:
        grouped_items: list[TThreadItem] = []
        index = 0
        while index < len(items):
            group_end = index + 1
            while group_end < len(items) and timestamp_getter(items[group_end]) == timestamp_getter(items[index]):
                group_end += 1
            grouped_items.extend(
                _sort_same_timestamp_group(
                    items[index:group_end],
                    event_id_getter=event_id_getter,
                    related_event_id_by_event_id=related_event_id_by_event_id,
                    input_order_by_event_id=input_order_by_event_id,
                ),
            )
            index = group_end
        items[:] = grouped_items

    root_index = next(
        (index for index, item in enumerate(items) if event_id_getter(item) == thread_id),
        None,
    )
    if root_index not in (None, 0):
        items.insert(0, items.pop(root_index))


def sort_thread_messages_root_first[TThreadMessageOrdering: SupportsThreadMessageOrdering](
    messages: list[TThreadMessageOrdering],
    *,
    thread_id: str,
    input_order_by_event_id: Mapping[str, int] | None = None,
    related_event_id_by_event_id: Mapping[str, str] | None = None,
) -> None:
    """Keep the thread root first, then order the remaining messages chronologically."""
    sort_thread_items_root_first(
        messages,
        thread_id=thread_id,
        event_id_getter=lambda message: message.event_id,
        timestamp_getter=lambda message: message.timestamp,
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )


def _event_id_from_source(event_source: Mapping[str, Any]) -> str | None:
    """Return one Matrix event ID from a raw event source when present."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def sort_thread_event_sources_root_first(
    event_sources: Sequence[dict[str, Any]],
    *,
    thread_id: str,
) -> list[dict[str, Any]]:
    """Return one stable raw-source order with same-timestamp ancestors before descendants."""
    input_order_by_event_id: dict[str, int] = {}
    related_event_id_by_event_id: dict[str, str] = {}
    for index, event_source in enumerate(event_sources):
        event_id = _event_id_from_source(event_source)
        if event_id is None:
            continue
        input_order_by_event_id[event_id] = index
        related_event_id = EventInfo.from_event(event_source).next_related_event_id(event_id)
        if related_event_id is not None:
            related_event_id_by_event_id[event_id] = related_event_id

    sorted_sources = list(event_sources)
    sort_thread_items_root_first(
        sorted_sources,
        thread_id=thread_id,
        event_id_getter=lambda source: _event_id_from_source(source) or "",
        timestamp_getter=lambda source: int(source.get("origin_server_ts", 0)),
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )
    return sorted_sources


def ordered_event_ids_from_scanned_event_sources(
    event_sources: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Return scanned event IDs in stable chronological discovery order."""
    event_source_list = list(event_sources)
    input_order_by_event_id = {
        event_id: index
        for index, event_source in enumerate(event_source_list)
        if isinstance((event_id := event_source.get("event_id")), str)
    }
    return [
        event_id
        for event_source in sorted(
            event_source_list,
            key=lambda source: (
                int(source.get("origin_server_ts", 0)),
                input_order_by_event_id.get(str(source.get("event_id", "")), 0),
                str(source.get("event_id", "")),
            ),
        )
        if isinstance((event_id := event_source.get("event_id")), str)
    ]


def _visible_thread_message_is_better_candidate(
    candidate: SupportsVisibleThreadMessage,
    current: SupportsVisibleThreadMessage,
) -> bool:
    """Return whether one visible message copy should replace the current event winner."""
    return (
        candidate.thread_id is not None,
        candidate.timestamp,
        candidate.latest_event_id,
    ) > (
        current.thread_id is not None,
        current.timestamp,
        current.latest_event_id,
    )


def _visible_thread_key(
    message: SupportsVisibleThreadMessage,
    thread_root_ids: set[str],
) -> str | None:
    """Return the canonical cleanup thread bucket for one visible message."""
    if message.thread_id is not None:
        return message.thread_id
    if message.event_id in thread_root_ids:
        return message.event_id
    return None


def _related_event_id_by_visible_event_id(
    messages: Sequence[SupportsVisibleThreadMessage],
) -> dict[str, str]:
    """Return same-thread relation edges keyed by the visible event state."""
    related_event_id_by_event_id: dict[str, str] = {}
    for message in messages:
        related_event_id = EventInfo.from_event({"content": dict(message.content)}).next_related_event_id(
            message.visible_event_id,
        )
        if isinstance(related_event_id, str):
            related_event_id_by_event_id[message.visible_event_id] = related_event_id
    return related_event_id_by_event_id


def latest_visible_thread_event_id_by_thread(
    messages: Sequence[SupportsVisibleThreadMessage],
) -> dict[str, str]:
    """Return the latest visible event ID for each thread in one cleanup scan."""
    thread_root_ids = {message.thread_id for message in messages if message.thread_id is not None}
    best_message_by_visible_event_id: dict[str, SupportsVisibleThreadMessage] = {}
    messages_by_thread: dict[str, list[SupportsVisibleThreadMessage]] = {}

    for message in messages:
        existing_message = best_message_by_visible_event_id.get(message.visible_event_id)
        if existing_message is not None and not _visible_thread_message_is_better_candidate(message, existing_message):
            continue
        best_message_by_visible_event_id[message.visible_event_id] = message

    for message in best_message_by_visible_event_id.values():
        thread_key = _visible_thread_key(message, thread_root_ids)
        if thread_key is None:
            continue
        messages_by_thread.setdefault(thread_key, []).append(message)

    latest_visible_event_ids: dict[str, str] = {}
    for thread_key, thread_messages in messages_by_thread.items():
        ordered_messages = list(thread_messages)
        sort_thread_items_root_first(
            ordered_messages,
            thread_id=thread_key,
            event_id_getter=lambda message: message.visible_event_id,
            timestamp_getter=lambda message: message.timestamp,
            input_order_by_event_id={message.visible_event_id: index for index, message in enumerate(thread_messages)},
            related_event_id_by_event_id=_related_event_id_by_visible_event_id(thread_messages),
        )
        root_index = next(
            (index for index, message in enumerate(ordered_messages) if message.event_id == thread_key),
            None,
        )
        if root_index not in (None, 0):
            ordered_messages.insert(0, ordered_messages.pop(root_index))
        latest_visible_event_ids[thread_key] = ordered_messages[-1].visible_event_id

    return latest_visible_event_ids


def _resolution_from_root_proof(
    thread_root_id: str,
    proof: ThreadRootProof,
) -> ThreadResolution:
    """Convert one root proof result into canonical thread membership."""
    if proof.state is ThreadRootProofState.PROVEN:
        return ThreadResolution.threaded(thread_root_id)
    if proof.state is ThreadRootProofState.NOT_A_THREAD_ROOT:
        return ThreadResolution.room_level()
    assert proof.error is not None
    return ThreadResolution.indeterminate(proof.error)


def _strict_thread_id_from_resolution(
    resolution: ThreadResolution,
) -> str | None:
    """Return the strict thread id or raise when proof is unavailable."""
    if resolution.state is not ThreadResolutionState.INDETERMINATE:
        return resolution.thread_id
    msg = "Thread membership proof unavailable"
    if resolution.error is not None and str(resolution.error):
        msg = str(resolution.error)
    raise ThreadMembershipProofError(msg) from resolution.error


async def resolve_event_thread_membership(
    room_id: str,
    event_info: EventInfo,
    *,
    access: ThreadMembershipAccess,
    event_id: str | None = None,
    allow_current_root: bool = False,
) -> ThreadResolution:
    """Return canonical thread membership for one event."""
    explicit_thread_id = event_info.thread_id or event_info.thread_id_from_edit
    if explicit_thread_id is not None:
        return ThreadResolution.threaded(explicit_thread_id)
    related_event_id = event_info.next_related_event_id("")
    if related_event_id is not None:
        return await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=access,
        )
    if allow_current_root and event_id is not None and event_info.can_be_thread_root:
        return _resolution_from_root_proof(
            event_id,
            await access.prove_thread_root(room_id, event_id),
        )
    return ThreadResolution.room_level()


async def resolve_related_event_thread_membership(
    room_id: str,
    related_event_id: str,
    *,
    access: ThreadMembershipAccess,
) -> ThreadResolution:
    """Return canonical thread membership for one related target event."""
    current_event_id = related_event_id
    visited_event_ids: set[str] = set()
    resolution = ThreadResolution.room_level()

    for _ in range(_MAX_THREAD_MEMBERSHIP_HOPS):
        if current_event_id in visited_event_ids:
            break
        visited_event_ids.add(current_event_id)

        thread_id = await access.lookup_thread_id(room_id, current_event_id)
        if thread_id is not None:
            resolution = ThreadResolution.threaded(thread_id)
            break

        try:
            related_event_info = await access.fetch_event_info(room_id, current_event_id)
        except Exception as exc:
            resolution = ThreadResolution.indeterminate(exc)
            break
        if related_event_info is None:
            break

        thread_id = related_event_info.thread_id or related_event_info.thread_id_from_edit
        if thread_id is not None:
            resolution = ThreadResolution.threaded(thread_id)
            break

        next_target = _next_related_event_target(
            related_event_info,
            current_event_id=current_event_id,
        )
        if next_target is not None:
            current_event_id = next_target
            continue

        if related_event_info.can_be_thread_root:
            resolution = _resolution_from_root_proof(
                current_event_id,
                await access.prove_thread_root(room_id, current_event_id),
            )
        break

    return resolution


async def resolve_event_thread_id(
    room_id: str,
    event_info: EventInfo,
    *,
    access: ThreadMembershipAccess,
    event_id: str | None = None,
    allow_current_root: bool = False,
) -> str | None:
    """Return the strict canonical thread membership for one event."""
    resolution = await resolve_event_thread_membership(
        room_id,
        event_info,
        access=access,
        event_id=event_id,
        allow_current_root=allow_current_root,
    )
    return _strict_thread_id_from_resolution(resolution)


async def resolve_related_event_thread_id(
    room_id: str,
    related_event_id: str,
    *,
    access: ThreadMembershipAccess,
) -> str | None:
    """Return the strict canonical thread membership for one related target event."""
    resolution = await resolve_related_event_thread_membership(
        room_id,
        related_event_id,
        access=access,
    )
    return _strict_thread_id_from_resolution(resolution)


async def resolve_event_thread_id_best_effort(
    room_id: str,
    event_info: EventInfo,
    *,
    access: ThreadMembershipAccess,
    event_id: str | None = None,
    allow_current_root: bool = False,
) -> str | None:
    """Return best-effort canonical thread membership for one event."""
    resolution = await resolve_event_thread_membership(
        room_id,
        event_info,
        access=access,
        event_id=event_id,
        allow_current_root=allow_current_root,
    )
    return resolution.thread_id


async def resolve_related_event_thread_id_best_effort(
    room_id: str,
    related_event_id: str,
    *,
    access: ThreadMembershipAccess,
) -> str | None:
    """Return best-effort canonical thread membership for one related target event."""
    resolution = await resolve_related_event_thread_membership(
        room_id,
        related_event_id,
        access=access,
    )
    return resolution.thread_id


def map_backed_thread_membership_access(
    *,
    event_infos: Mapping[str, EventInfo],
    resolved_thread_ids: dict[str, str],
) -> ThreadMembershipAccess:
    """Return one thread-membership access adapter backed by in-memory event maps."""

    async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
        return resolved_thread_ids.get(event_id)

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return event_infos.get(event_id)

    async def prove_thread_root(_room_id: str, thread_root_id: str) -> ThreadRootProof:
        has_children = any(
            event_id != thread_root_id
            and any(
                candidate_thread_id == thread_root_id
                for candidate_thread_id in (
                    event_info.thread_id,
                    event_info.thread_id_from_edit,
                )
            )
            for event_id, event_info in event_infos.items()
        )
        return ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
    )


def _is_thread_root_not_found_error(error: Exception) -> bool:
    """Return whether one proof failure means the candidate root simply does not exist."""
    from mindroom.matrix.client import ThreadRoomScanRootNotFoundError  # noqa: PLC0415

    return isinstance(error, ThreadRoomScanRootNotFoundError)


async def thread_messages_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_messages: ThreadMessagesLookup,
) -> ThreadRootProof:
    """Return one root-proof result from authoritative thread messages."""
    try:
        thread_messages = await fetch_thread_messages(room_id, thread_root_id)
    except Exception as exc:
        if _is_thread_root_not_found_error(exc):
            return ThreadRootProof.not_a_thread_root()
        return ThreadRootProof.proof_unavailable(exc)
    has_children = any(message.event_id != thread_root_id for message in thread_messages)
    return ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()


async def snapshot_thread_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_snapshot: ThreadSnapshotLookup,
) -> ThreadRootProof:
    """Return one snapshot-backed root-proof result."""
    return await thread_messages_root_proof(
        room_id,
        thread_root_id,
        fetch_thread_messages=fetch_thread_snapshot,
    )


async def room_scan_thread_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_event_sources: ThreadEventSourcesLookup,
) -> ThreadRootProof:
    """Return one room-scan-backed root-proof result."""
    try:
        event_sources, root_found = await fetch_thread_event_sources(room_id, thread_root_id)
    except Exception as exc:
        if _is_thread_root_not_found_error(exc):
            return ThreadRootProof.not_a_thread_root()
        return ThreadRootProof.proof_unavailable(exc)
    if not root_found:
        return ThreadRootProof.not_a_thread_root()
    has_children = any(
        _room_scan_event_source_counts_as_thread_child_proof(
            thread_root_id,
            event_source=event_source,
        )
        for event_source in event_sources
    )
    return ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()


def _room_scan_event_source_counts_as_thread_child_proof(
    thread_root_id: str,
    *,
    event_source: Mapping[str, object],
) -> bool:
    """Return whether one room-scan source proves the root has real threaded descendants."""
    event_id = event_source.get("event_id")
    if event_id == thread_root_id:
        return False
    event_info = EventInfo.from_event(dict(event_source))
    return not (event_info.is_edit and event_info.original_event_id == thread_root_id)


def thread_messages_thread_membership_access(
    *,
    lookup_thread_id: ThreadIdLookup,
    fetch_event_info: EventInfoLookup,
    fetch_thread_messages: ThreadMessagesLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative thread messages."""

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadRootProof:
        return await thread_messages_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_messages=fetch_thread_messages,
        )

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
    )


def snapshot_thread_membership_access(
    *,
    lookup_thread_id: ThreadIdLookup,
    fetch_event_info: EventInfoLookup,
    fetch_thread_snapshot: ThreadSnapshotLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative thread snapshots."""
    return thread_messages_thread_membership_access(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        fetch_thread_messages=fetch_thread_snapshot,
    )


def room_scan_thread_membership_access(
    *,
    lookup_thread_id: ThreadIdLookup,
    fetch_event_info: EventInfoLookup,
    fetch_thread_event_sources: ThreadEventSourcesLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative room scans."""

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadRootProof:
        return await room_scan_thread_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_event_sources=fetch_thread_event_sources,
        )

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
    )


def room_scan_thread_membership_access_for_client(
    client: nio.AsyncClient,
    *,
    lookup_thread_id: ThreadIdLookup,
    fetch_event_info: EventInfoLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access using room scans from one Matrix client."""

    async def fetch_thread_event_sources(
        room_id: str,
        thread_root_id: str,
    ) -> tuple[list[dict[str, object]], bool]:
        from mindroom.matrix.client import _fetch_thread_event_sources_via_room_messages  # noqa: PLC0415

        return await _fetch_thread_event_sources_via_room_messages(
            client,
            room_id,
            thread_root_id,
        )

    return room_scan_thread_membership_access(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        fetch_thread_event_sources=fetch_thread_event_sources,
    )


async def lookup_thread_id_from_conversation_cache(
    conversation_cache: ConversationCacheProtocol | None,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return one cached thread root when a conversation cache is available."""
    if conversation_cache is None:
        return None
    return await conversation_cache.get_thread_id_for_event(room_id, event_id)


def _event_info_from_lookup_response(
    response: object,
    *,
    event_id: str,
    strict: bool,
) -> EventInfo | None:
    """Normalize one room-get-event style response into EventInfo when available."""
    if isinstance(response, nio.RoomGetEventResponse):
        return EventInfo.from_event(response.event.source)
    if not strict:
        return None
    if isinstance(response, nio.RoomGetEventError) and response.status_code == "M_NOT_FOUND":
        return None
    detail = response.message if isinstance(response, nio.RoomGetEventError) else "unknown error"
    msg = f"Failed to resolve Matrix event {event_id}: {detail}"
    raise RuntimeError(msg)


async def fetch_event_info_from_conversation_cache(
    conversation_cache: ConversationCacheProtocol,
    room_id: str,
    event_id: str,
    *,
    strict: bool,
) -> EventInfo | None:
    """Fetch one event through the conversation cache and parse its relation metadata."""
    response = await conversation_cache.get_event(room_id, event_id)
    return _event_info_from_lookup_response(
        response,
        event_id=event_id,
        strict=strict,
    )


async def fetch_event_info_for_client(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    strict: bool,
) -> EventInfo | None:
    """Fetch one event directly from Matrix and parse its relation metadata."""
    response = await client.room_get_event(room_id, event_id)
    return _event_info_from_lookup_response(
        response,
        event_id=event_id,
        strict=strict,
    )


def conversation_cache_thread_membership_access_for_client(
    client: nio.AsyncClient,
    *,
    conversation_cache: ConversationCacheProtocol | None,
    fetch_event_info: EventInfoLookup | None = None,
) -> ThreadMembershipAccess:
    """Build room-scan membership access backed by conversation-cache lookups when available."""

    async def lookup_thread_id(lookup_room_id: str, lookup_event_id: str) -> str | None:
        return await lookup_thread_id_from_conversation_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
        )

    async def resolved_fetch_event_info(lookup_room_id: str, lookup_event_id: str) -> EventInfo | None:
        if fetch_event_info is not None:
            return await fetch_event_info(lookup_room_id, lookup_event_id)
        if conversation_cache is None:
            return None
        return await fetch_event_info_from_conversation_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
            strict=True,
        )

    return room_scan_thread_membership_access_for_client(
        client,
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=resolved_fetch_event_info,
    )


async def resolve_thread_root_event_id_for_client(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Resolve one event ID into a canonical thread root when thread membership can prove one."""
    normalized_event_id = event_id.strip() if isinstance(event_id, str) else ""
    if not normalized_event_id:
        return None

    event_info = await fetch_event_info_for_client(
        client,
        room_id,
        normalized_event_id,
        strict=False,
    )
    if event_info is None:
        return await lookup_thread_id_from_conversation_cache(
            conversation_cache,
            room_id,
            normalized_event_id,
        )

    return await resolve_event_thread_id(
        room_id,
        event_info,
        event_id=normalized_event_id,
        allow_current_root=True,
        access=conversation_cache_thread_membership_access_for_client(
            client,
            conversation_cache=conversation_cache,
            fetch_event_info=lambda lookup_room_id, lookup_event_id: fetch_event_info_for_client(
                client,
                lookup_room_id,
                lookup_event_id,
                strict=False,
            ),
        ),
    )


async def resolve_thread_ids_for_event_infos(
    room_id: str,
    *,
    event_infos: Mapping[str, EventInfo],
    ordered_event_ids: Sequence[str],
    resolved_thread_ids: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve canonical thread membership for one local event-info graph."""
    resolved = {} if resolved_thread_ids is None else resolved_thread_ids
    access = map_backed_thread_membership_access(
        event_infos=event_infos,
        resolved_thread_ids=resolved,
    )

    progress_made = True
    while progress_made:
        progress_made = False
        for event_id in ordered_event_ids:
            if event_id in resolved:
                continue
            event_info = event_infos.get(event_id)
            if event_info is None:
                continue
            resolution = await resolve_event_thread_membership(
                room_id,
                event_info,
                access=access,
            )
            if not resolution.is_threaded:
                continue
            assert resolution.thread_id is not None
            resolved[event_id] = resolution.thread_id
            progress_made = True

    return resolved
