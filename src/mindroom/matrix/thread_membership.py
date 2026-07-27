"""Canonical Matrix thread resolution.

Ownership map:
- canonical thread identity: this module (pure domain rules, no client or cache transport)
- client- and cache-backed membership accessors: `mindroom.matrix.thread_room_scan`
- scanned-event ordering and latest-thread-tail helpers: `mindroom.matrix.thread_projection`
- mutation/bookkeeping impact: `mindroom.matrix.thread_bookkeeping`
- tool-facing normalization: `mindroom.custom_tools.attachment_helpers`

Invariants enforced here (every resolver in the repo must go through this module):

1. An event is THREADED if and only if one of the following holds:
   it carries a native ``m.thread`` relation (``EventInfo.thread_id``);
   it is an edit whose ``m.new_content`` carries an ``m.thread`` relation (``thread_id_from_edit``);
   a relation walk from it reaches an event satisfying either of the above;
   or the walk terminates at a relation-free event that is proven to have at least one real threaded child,
   in which case that terminal event is itself the thread root.
   Per MSC3440 only relation-free events (``can_be_thread_root``) may become roots.

2. The relation walk follows ``EventInfo.next_related_event_id``: edit original, then reaction target,
   then ``m.reference`` target, then reply target.
   An explicit thread relation always wins immediately; the walk never continues past one.
   This is how implied membership works: plain replies, references, and reactions to threaded events
   inherit the target's thread transitively.

3. The walk always terminates: a visited set breaks relation cycles and ``_MAX_THREAD_MEMBERSHIP_HOPS``
   caps pathological chains, resolving to the best answer found so far (initially ROOM_LEVEL).

4. Root proof is three-valued (PROVEN, NOT_A_THREAD_ROOT, PROOF_UNAVAILABLE) and proof failure never
   silently demotes to room level.
   PROOF_UNAVAILABLE maps to ``ThreadResolution.indeterminate`` with the candidate root preserved so
   callers can fail closed (mutation callers invalidate room-wide; dispatch callers coalesce on the
   candidate root and retry).
   Lookup failures and missing related events during the walk are likewise INDETERMINATE, never ROOM_LEVEL.

5. Child proof excludes the candidate root itself and excludes edits of the root: an ``m.replace`` of a
   relation-free event does not make that event a thread root.

6. A root proof built from thread history whose read source is the explicit degraded fallback
   (``THREAD_HISTORY_SOURCE_DEGRADED``, i.e. an empty fail-open read) is PROOF_UNAVAILABLE, never
   NOT_A_THREAD_ROOT: an empty degraded read must not demote an existing thread to room level.
   Stale-cache history (``stale_cache`` source) remains acceptable proof material.

7. A room scan that completes without ever seeing the candidate root
   (``ThreadRoomScanRootNotFoundError``) is definitive NOT_A_THREAD_ROOT, not a proof failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_diagnostics import is_thread_history_source_degraded

type _ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type _EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type _ThreadRootProofLookup = Callable[[str, str], Awaitable["ThreadRootProof"]]
type _ThreadEventSourcesLookup = Callable[[str, str], Awaitable[tuple[Sequence[Mapping[str, object]], bool]]]
_MAX_THREAD_MEMBERSHIP_HOPS = 512


class _SupportsEventId(Protocol):
    """Minimal protocol for entries used during thread-root checks."""

    event_id: str


type _ThreadMessagesLookup = Callable[[str, str], Awaitable[Sequence[_SupportsEventId]]]


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
    thread_history: Sequence[_SupportsEventId] | None = None

    @classmethod
    def proven(cls, thread_history: Sequence[_SupportsEventId] | None = None) -> ThreadRootProof:
        """Return a successful root proof."""
        return cls(_ThreadRootProofState.PROVEN, thread_history=thread_history)

    @classmethod
    def not_a_thread_root(cls, thread_history: Sequence[_SupportsEventId] | None = None) -> ThreadRootProof:
        """Return a definite non-thread-root result."""
        return cls(_ThreadRootProofState.NOT_A_THREAD_ROOT, thread_history=thread_history)

    @classmethod
    def proof_unavailable(
        cls,
        error: Exception,
        thread_history: Sequence[_SupportsEventId] | None = None,
    ) -> ThreadRootProof:
        """Return one failed proof attempt without weakening caller policy."""
        return cls(_ThreadRootProofState.PROOF_UNAVAILABLE, error=error, thread_history=thread_history)


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
    candidate_thread_root_id: str | None = None
    error: Exception | None = None
    thread_history: Sequence[_SupportsEventId] | None = None

    @classmethod
    def threaded(
        cls,
        thread_id: str,
        thread_history: Sequence[_SupportsEventId] | None = None,
    ) -> ThreadResolution:
        """Return one positive thread-membership result."""
        return cls(ThreadResolutionState.THREADED, thread_id=thread_id, thread_history=thread_history)

    @classmethod
    def room_level(cls, thread_history: Sequence[_SupportsEventId] | None = None) -> ThreadResolution:
        """Return one definite room-level result."""
        return cls(ThreadResolutionState.ROOM_LEVEL, thread_history=thread_history)

    @classmethod
    def indeterminate(
        cls,
        error: Exception,
        candidate_thread_root_id: str | None = None,
        thread_history: Sequence[_SupportsEventId] | None = None,
    ) -> ThreadResolution:
        """Return one unresolved result caused by proof failure."""
        return cls(
            ThreadResolutionState.INDETERMINATE,
            candidate_thread_root_id=candidate_thread_root_id,
            error=error,
            thread_history=thread_history,
        )

    @property
    def is_threaded(self) -> bool:
        """Return whether the event was proven to belong to a thread."""
        return self.state is ThreadResolutionState.THREADED


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
        return ThreadResolution.threaded(thread_root_id, thread_history=proof.thread_history)
    if proof.state is _ThreadRootProofState.NOT_A_THREAD_ROOT:
        return ThreadResolution.room_level(thread_history=proof.thread_history)
    assert proof.error is not None
    return ThreadResolution.indeterminate(
        proof.error,
        candidate_thread_root_id=thread_root_id,
        thread_history=proof.thread_history,
    )


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
            # Keep lookup-failed related events separately scoped for dispatch coalescing and replay checks.
            resolution = ThreadResolution.indeterminate(exc, candidate_thread_root_id=current_event_id)
            break
        if related_event_info is None:
            # Missing related events are still possible thread roots; demote later without losing the candidate.
            resolution = ThreadResolution.indeterminate(
                ThreadMembershipLookupError(f"Related event {current_event_id} is unavailable"),
                candidate_thread_root_id=current_event_id,
            )
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
            page_event_info_counts_as_thread_child_proof(
                thread_root_id,
                event_id=event_id,
                event_info=event_info,
            )
            for event_id, event_info in event_infos.items()
        )
        return ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
    )


def page_event_info_counts_as_thread_child_proof(
    thread_root_id: str,
    *,
    event_id: str,
    event_info: EventInfo,
) -> bool:
    """Return whether one page-local event proves a root has thread children."""
    if event_id == thread_root_id:
        return False
    return any(
        candidate_thread_id == thread_root_id
        for candidate_thread_id in (
            event_info.thread_id,
            event_info.thread_id_from_edit,
        )
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
    if is_thread_history_source_degraded(thread_messages):
        msg = "Thread root proof unavailable from degraded thread history"
        return ThreadRootProof.proof_unavailable(RuntimeError(msg), thread_history=thread_messages)
    has_children = any(message.event_id != thread_root_id for message in thread_messages)
    return (
        ThreadRootProof.proven(thread_history=thread_messages)
        if has_children
        else ThreadRootProof.not_a_thread_root(thread_history=thread_messages)
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
