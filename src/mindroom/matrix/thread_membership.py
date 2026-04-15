"""Shared transitive thread membership resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    import nio

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

    for _ in range(_MAX_THREAD_MEMBERSHIP_HOPS):
        if current_event_id in visited_event_ids:
            break
        visited_event_ids.add(current_event_id)

        thread_id = await access.lookup_thread_id(room_id, current_event_id)
        if thread_id is not None:
            return ThreadResolution.threaded(thread_id)

        related_event_info = await access.fetch_event_info(room_id, current_event_id)
        if related_event_info is None:
            return ThreadResolution.room_level()

        thread_id = related_event_info.thread_id or related_event_info.thread_id_from_edit
        if thread_id is not None:
            return ThreadResolution.threaded(thread_id)

        next_target = _next_related_event_target(
            related_event_info,
            current_event_id=current_event_id,
        )
        if next_target is not None:
            current_event_id = next_target
            continue

        if related_event_info.can_be_thread_root:
            return _resolution_from_root_proof(
                current_event_id,
                await access.prove_thread_root(room_id, current_event_id),
            )
        return ThreadResolution.room_level()

    return ThreadResolution.room_level()


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
    has_children = any(event_source.get("event_id") != thread_root_id for event_source in event_sources)
    return ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()


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
