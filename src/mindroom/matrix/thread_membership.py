"""Canonical Matrix thread resolution.

Ownership map:
- canonical thread identity: this module
- scanned-event ordering and latest-thread-tail helpers: `mindroom.matrix.thread_projection`
- mutation/bookkeeping impact: `mindroom.matrix.thread_bookkeeping`
- tool-facing normalization: `mindroom.custom_tools.attachment_helpers`
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

type _ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type _EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type _ThreadRootProofLookup = Callable[[str, str], Awaitable["ThreadRootProof"]]
type _ThreadEventSourcesLookup = Callable[[str, str], Awaitable[tuple[Sequence[Mapping[str, object]], bool]]]
_MAX_THREAD_MEMBERSHIP_HOPS = 512


class _SupportsEventId(Protocol):
    """Minimal protocol for snapshot entries used during thread-root checks."""

    event_id: str


type _ThreadMessagesLookup = Callable[[str, str], Awaitable[Sequence[_SupportsEventId]]]
type _ThreadSnapshotLookup = Callable[[str, str], Awaitable[Sequence[_SupportsEventId]]]


class _ThreadRootProofState(Enum):
    """Outcome of proving whether one candidate event is a real thread root."""

    PROVEN = auto()
    NOT_A_THREAD_ROOT = auto()
    PROOF_UNAVAILABLE = auto()


@dataclass(frozen=True)
class ThreadRootProof:
    """Result of one thread-root proof attempt."""

    state: _ThreadRootProofState
    error: Exception | None = None

    @classmethod
    def proven(cls) -> ThreadRootProof:
        """Return a successful root proof."""
        return cls(_ThreadRootProofState.PROVEN)

    @classmethod
    def not_a_thread_root(cls) -> ThreadRootProof:
        """Return a definite non-thread-root result."""
        return cls(_ThreadRootProofState.NOT_A_THREAD_ROOT)

    @classmethod
    def proof_unavailable(cls, error: Exception) -> ThreadRootProof:
        """Return one failed proof attempt without weakening caller policy."""
        return cls(_ThreadRootProofState.PROOF_UNAVAILABLE, error=error)


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


class _ThreadMembershipProofError(RuntimeError):
    """Raised when strict thread-membership resolution cannot prove one candidate root."""


class ThreadMembershipLookupError(RuntimeError):
    """Raised when related-event lookup cannot determine thread membership from available data."""


class ThreadRoomScanRootNotFoundError(RuntimeError):
    """Raised when a room scan finishes without ever seeing the requested root event."""


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

    lookup_thread_id: _ThreadIdLookup
    fetch_event_info: _EventInfoLookup
    prove_thread_root: _ThreadRootProofLookup


def _resolution_from_root_proof(
    thread_root_id: str,
    proof: ThreadRootProof,
) -> ThreadResolution:
    """Convert one root proof result into canonical thread membership."""
    if proof.state is _ThreadRootProofState.PROVEN:
        return ThreadResolution.threaded(thread_root_id)
    if proof.state is _ThreadRootProofState.NOT_A_THREAD_ROOT:
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
    raise _ThreadMembershipProofError(msg) from resolution.error


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


async def _resolve_related_event_thread_id(
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
    return isinstance(error, ThreadRoomScanRootNotFoundError)


async def _thread_messages_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_messages: _ThreadMessagesLookup,
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


async def _snapshot_thread_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_snapshot: _ThreadSnapshotLookup,
) -> ThreadRootProof:
    """Return one snapshot-backed root-proof result."""
    return await _thread_messages_root_proof(
        room_id,
        thread_root_id,
        fetch_thread_messages=fetch_thread_snapshot,
    )


async def _room_scan_thread_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_event_sources: _ThreadEventSourcesLookup,
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
    lookup_thread_id: _ThreadIdLookup,
    fetch_event_info: _EventInfoLookup,
    fetch_thread_messages: _ThreadMessagesLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative thread messages."""

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadRootProof:
        return await _thread_messages_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_messages=fetch_thread_messages,
        )

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
    )


def _snapshot_thread_membership_access(
    *,
    lookup_thread_id: _ThreadIdLookup,
    fetch_event_info: _EventInfoLookup,
    fetch_thread_snapshot: _ThreadSnapshotLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative thread snapshots."""
    return thread_messages_thread_membership_access(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        fetch_thread_messages=fetch_thread_snapshot,
    )


def room_scan_thread_membership_access(
    *,
    lookup_thread_id: _ThreadIdLookup,
    fetch_event_info: _EventInfoLookup,
    fetch_thread_event_sources: _ThreadEventSourcesLookup,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative room scans."""

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadRootProof:
        return await _room_scan_thread_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_event_sources=fetch_thread_event_sources,
        )

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
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


async def _fetch_event_info_from_conversation_cache(
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
