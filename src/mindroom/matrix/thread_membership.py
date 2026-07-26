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
   a relation walk from it reaches an event satisfying either of the above;
   or the walk reaches an event without a primary relation type that has at least one real threaded child,
   in which case that terminal event is itself the thread root.
   Local history graphs validate replacement membership through the original event rather than treating
   replacement content as independent proof.
   Per MSC3440 only events without a primary relation type (``can_be_thread_root``) may become roots.

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

5. Child proof and relation ancestry accept only ``m.room.message`` and ``m.room.encrypted`` events.
   Child proof also excludes the candidate root itself and edits of the root: an ``m.replace`` of a
   root-capable event does not make that event a thread root.

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
from typing import Any, Protocol

from mindroom.matrix.event_info import EventInfo, event_type_supports_thread_relations
from mindroom.matrix.media import (
    event_source_supports_valid_explicit_thread_relation,
    event_source_supports_valid_thread_relations,
    valid_room_message_event_source,
    valid_room_message_replacement,
)
from mindroom.matrix.replacements import is_valid_replacement
from mindroom.matrix.thread_diagnostics import is_thread_history_source_degraded

type _ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type _EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type _EventSourceLookup = Callable[[str, str], Awaitable[Mapping[str, Any] | None]]
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


def event_info_proves_thread_membership(event_info: EventInfo, event_id: str, thread_id: str) -> bool:
    """Return whether local event facts alone prove membership in one thread."""
    if event_id == thread_id:
        return event_info.can_be_thread_root
    return bool(thread_id) and event_info.thread_id == thread_id


class ThreadRoomScanRootNotFoundError(RuntimeError):
    """Raised when a room scan finishes without ever seeing the requested root event."""


def _next_related_event_target(
    event_info: EventInfo,
    *,
    current_event_id: str,
) -> str | None:
    """Return the next related event to inspect."""
    return event_info.next_related_event_id(current_event_id)


async def _validated_next_related_event_target(
    room_id: str,
    event_info: EventInfo,
    *,
    current_event_id: str,
    access: ThreadMembershipAccess,
) -> str | None:
    """Return the next relation target after validating replacement ancestry."""
    next_target = _next_related_event_target(event_info, current_event_id=current_event_id)
    if not event_info.is_edit:
        return next_target
    if next_target is None or not await _replacement_ancestry_is_valid(
        room_id,
        current_event_id,
        next_target,
        access=access,
    ):
        return None
    return next_target


async def _replacement_ancestry_is_valid(
    room_id: str,
    replacement_event_id: str,
    original_event_id: str,
    *,
    access: ThreadMembershipAccess,
    candidate_source: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether one replacement may inherit its original's membership."""
    if access.fetch_event_source is None:
        msg = f"Replacement ancestry lookup unavailable for {replacement_event_id}"
        raise ThreadMembershipLookupError(msg)
    candidate = candidate_source or await access.fetch_event_source(room_id, replacement_event_id)
    original = await access.fetch_event_source(room_id, original_event_id)
    if candidate is None or original is None:
        msg = f"Replacement ancestry lookup unavailable for {replacement_event_id}"
        raise ThreadMembershipLookupError(msg)
    if candidate.get("type") == "m.room.encrypted":
        return (
            EventInfo.from_event(dict(candidate)).original_event_id == original_event_id
            and event_source_supports_valid_thread_relations(candidate, room_id)
            and event_source_supports_valid_thread_relations(original, room_id)
            and not EventInfo.from_event(dict(original)).is_edit
            and candidate.get("sender") == original.get("sender")
        )
    return valid_room_message_event_source(original) and is_valid_replacement(
        original,
        candidate,
        room_id=room_id,
        validator=valid_room_message_replacement,
    )


@dataclass(frozen=True)
class ThreadMembershipAccess:
    """Repository-wide accessors used to resolve one event's thread membership."""

    lookup_thread_id: _ThreadIdLookup
    fetch_event_info: _EventInfoLookup
    prove_thread_root: _ThreadRootProofLookup
    fetch_event_source: _EventSourceLookup | None = None


def conversation_relation_thread_membership_access(
    access: ThreadMembershipAccess,
) -> ThreadMembershipAccess:
    """Reject non-message ancestors and stale indexes from conversation relation walks."""
    event_sources: dict[tuple[str, str], Mapping[str, Any] | None] = {}

    async def fetch_event_info(room_id: str, event_id: str) -> EventInfo | None:
        source_key = (room_id, event_id)
        if source_key in event_sources:
            event_source = event_sources[source_key]
            event_info = (
                EventInfo.from_event(dict(event_source))
                if event_source is not None and event_source_supports_valid_thread_relations(event_source, room_id)
                else None
            )
        else:
            event_info = await access.fetch_event_info(room_id, event_id)
        if event_info is not None and not event_type_supports_thread_relations(event_info.event_type):
            msg = f"Related event {event_id} cannot carry conversation thread membership"
            raise ThreadMembershipLookupError(msg)
        return event_info

    async def fetch_event_source(room_id: str, event_id: str) -> Mapping[str, Any] | None:
        source_key = (room_id, event_id)
        if source_key not in event_sources:
            event_sources[source_key] = (
                None if access.fetch_event_source is None else await access.fetch_event_source(room_id, event_id)
            )
        return event_sources[source_key]

    return ThreadMembershipAccess(
        lookup_thread_id=access.lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=access.prove_thread_root,
        fetch_event_source=fetch_event_source,
    )


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
    event_source: Mapping[str, Any] | None = None,
    allow_current_root: bool = False,
) -> ThreadResolution:
    """Return canonical thread membership for one event."""
    if event_source is not None and not event_source_supports_valid_thread_relations(event_source, room_id):
        return ThreadResolution.room_level()
    explicit_thread_id = event_info.thread_id
    if explicit_thread_id is not None:
        explicit_thread_is_valid = (
            explicit_thread_id != event_id
            and event_type_supports_thread_relations(event_info.event_type)
            and (
                event_source_supports_valid_explicit_thread_relation(event_source, room_id)
                if event_source is not None
                else True
            )
        )
        return (
            ThreadResolution.threaded(explicit_thread_id) if explicit_thread_is_valid else ThreadResolution.room_level()
        )
    if allow_current_root and event_id is not None and event_info.can_be_thread_root:
        proof = await access.prove_thread_root(room_id, event_id)
        if proof.state is not _ThreadRootProofState.NOT_A_THREAD_ROOT:
            return _resolution_from_root_proof(event_id, proof)
    related_event_id = event_info.next_related_event_id("")
    if related_event_id is not None:
        if event_info.is_edit and event_id is not None:
            try:
                valid_replacement = await _replacement_ancestry_is_valid(
                    room_id,
                    event_id,
                    related_event_id,
                    access=access,
                    candidate_source=event_source,
                )
            except Exception as exc:
                return ThreadResolution.indeterminate(
                    exc,
                    candidate_thread_root_id=related_event_id,
                )
            if not valid_replacement:
                return ThreadResolution.room_level()
        resolution = await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=access,
        )
    else:
        resolution = ThreadResolution.room_level()
    return resolution


async def resolve_related_event_thread_membership(  # noqa: C901
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

        indexed_thread_id = await access.lookup_thread_id(room_id, current_event_id)

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

        thread_id = related_event_info.thread_id
        if thread_id is not None:
            resolution = ThreadResolution.threaded(thread_id)
            break

        if related_event_info.can_be_thread_root:
            proof = await access.prove_thread_root(room_id, current_event_id)
            if proof.state is not _ThreadRootProofState.NOT_A_THREAD_ROOT:
                resolution = _resolution_from_root_proof(current_event_id, proof)
                break
            resolution = ThreadResolution.room_level(thread_history=proof.thread_history)

        try:
            next_target = await _validated_next_related_event_target(
                room_id,
                related_event_info,
                current_event_id=current_event_id,
                access=access,
            )
        except Exception as exc:
            resolution = ThreadResolution.indeterminate(exc, candidate_thread_root_id=current_event_id)
            break

        if next_target is not None:
            current_event_id = next_target
            continue
        if indexed_thread_id is not None and not related_event_info.is_edit:
            resolution = ThreadResolution.threaded(indexed_thread_id)
            break
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
    event_sources_by_event_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> ThreadMembershipAccess:
    """Return one thread-membership access adapter backed by in-memory event maps."""

    async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
        return resolved_thread_ids.get(event_id)

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        event_info = event_infos.get(event_id)
        if event_info is None or event_sources_by_event_id is None:
            return event_info
        event_source = event_sources_by_event_id.get(event_id)
        return (
            event_info
            if event_source is not None and event_source_supports_valid_thread_relations(event_source, _room_id)
            else None
        )

    async def prove_thread_root(_room_id: str, thread_root_id: str) -> ThreadRootProof:
        has_children = any(
            page_event_info_counts_as_thread_child_proof(
                thread_root_id,
                event_id=event_id,
                event_info=event_info,
            )
            and (
                event_sources_by_event_id is None
                or (
                    (event_source := event_sources_by_event_id.get(event_id)) is not None
                    and event_source_supports_valid_thread_relations(event_source, _room_id)
                )
            )
            for event_id, event_info in event_infos.items()
        )
        return ThreadRootProof.proven() if has_children else ThreadRootProof.not_a_thread_root()

    async def fetch_event_source(
        _room_id: str,
        event_id: str,
    ) -> Mapping[str, Any] | None:
        return None if event_sources_by_event_id is None else event_sources_by_event_id.get(event_id)

    return conversation_relation_thread_membership_access(
        ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
            fetch_event_source=fetch_event_source,
        ),
    )


def page_event_info_counts_as_thread_child_proof(
    thread_root_id: str,
    *,
    event_id: str,
    event_info: EventInfo,
) -> bool:
    """Return whether one page-local event proves a root has thread children."""
    if event_id == thread_root_id or not event_type_supports_thread_relations(event_info.event_type):
        return False
    return event_info.thread_id == thread_root_id


def local_events_prove_thread_root(thread_root_id: str, event_infos: Mapping[str, EventInfo]) -> bool:
    """Return whether local event facts alone prove one candidate is a real thread root.

    Per MSC3440 a root-capable event becomes a root only once it has a real ``m.thread`` child.
    Callers seeding a root into a local resolution must prove it here first, otherwise a plain
    reply to that event would inherit thread membership the thread never actually established.
    """
    root_info = event_infos.get(thread_root_id)
    return (
        root_info is not None
        and root_info.can_be_thread_root
        and any(
            page_event_info_counts_as_thread_child_proof(thread_root_id, event_id=event_id, event_info=event_info)
            for event_id, event_info in event_infos.items()
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
    if not isinstance(event_id, str):
        return False
    event_info = EventInfo.from_event(dict(event_source))
    return (
        event_id != thread_root_id
        and event_type_supports_thread_relations(event_info.event_type)
        and not (event_info.is_edit and event_info.original_event_id == thread_root_id)
    )


def thread_messages_thread_membership_access(
    *,
    lookup_thread_id: _ThreadIdLookup,
    fetch_event_info: _EventInfoLookup,
    fetch_thread_messages: _ThreadMessagesLookup,
    fetch_event_source: _EventSourceLookup | None = None,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative thread messages."""

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadRootProof:
        return await _thread_messages_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_messages=fetch_thread_messages,
        )

    return conversation_relation_thread_membership_access(
        ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
            fetch_event_source=fetch_event_source,
        ),
    )


def room_scan_thread_membership_access(
    *,
    lookup_thread_id: _ThreadIdLookup,
    fetch_event_info: _EventInfoLookup,
    fetch_thread_event_sources: _ThreadEventSourcesLookup,
    fetch_event_source: _EventSourceLookup | None = None,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative room scans."""

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadRootProof:
        return await _room_scan_thread_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_event_sources=fetch_thread_event_sources,
        )

    return conversation_relation_thread_membership_access(
        ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
            fetch_event_source=fetch_event_source,
        ),
    )
