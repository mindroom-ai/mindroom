"""Canonical Matrix thread resolution.

Ownership map:
- canonical thread identity: this module (pure domain rules, no client or cache transport)
- turn-path membership access: `mindroom.conversation_resolver`
- membership access for tools and outbound mutations: `mindroom.matrix.projected_thread_membership`
- outbound mutation impact: `mindroom.matrix.thread_mutation_impact`
- tool-facing normalization: `mindroom.custom_tools.attachment_helpers`

Every root proof reads the candidate's thread from the conversation projection.

Invariants enforced here (every resolver in the repo must go through this module):

1. An event is THREADED if and only if one of the following holds:
   it carries a native ``m.thread`` relation (``EventInfo.thread_id``);
   a relation walk from it reaches an event carrying one;
   or the walk terminates at a relation-free event that is proven to have at least one real threaded child,
   in which case that terminal event is itself the thread root.
   Per MSC3440 only relation-free events (``can_be_thread_root``) may become roots.
   An ``m.thread`` relation written inside an edit's ``m.new_content`` is never one of those things.
   Matrix applies ``m.new_content`` by keeping the original event's relation and ignoring every
   ``m.relates_to`` found there, so an edit is placed by the event it replaces: the walk hops to
   that original and resolves it, and an original nobody can read is INDETERMINATE like any other
   unavailable relation target.

2. The relation walk follows ``EventInfo.next_related_event_id``: edit original, then reaction target,
   then ``m.reference`` target, then reply target.
   An explicit thread relation always wins immediately; the walk never continues past one.
   This is how implied membership works: plain replies, references, and reactions to threaded events
   inherit the target's thread transitively.

3. The walk always terminates: a visited set breaks relation cycles and ``_MAX_THREAD_MEMBERSHIP_HOPS``
   caps pathological chains; a walk ended either way resolves ROOM_LEVEL.

4. A root proof answers with the candidate's own membership, and proof failure never silently
   demotes to room level: a proven root is THREADED in its own thread, a candidate that is not a
   thread root is ROOM_LEVEL, and an unavailable proof is INDETERMINATE.
   Callers can therefore fail closed: tools and outbound mutations refuse, and ingress retries a
   non-blocking read strictly, then refuses the event until its scope can be decided.
   Lookup failures and missing related events during the walk are likewise INDETERMINATE, never ROOM_LEVEL.
   A related event the homeserver will not serve, or one that is not a message, carries
   ``RelatedEventUnavailableError``, which no retry changes.

5. Relation ancestry accepts only ``m.room.message`` and ``m.room.encrypted`` events.
   Child proof counts only readable messages the candidate's thread holds other than the root itself;
   an edit of the root is folded into the root, so it never proves the root has a thread.

6. A root proof built from thread history whose read source is the explicit degraded fallback
   (``THREAD_HISTORY_SOURCE_DEGRADED``, i.e. an empty fail-open read) is unavailable, never
   ROOM_LEVEL: an empty degraded read must not demote an existing thread to room level.
   A thread read that raises leaves the proof unavailable too.

7. A read with no readable child proves a candidate is not a thread root only as far as the caller
   accepts it.
   History that may be incomplete -- hydration stopped at a ceiling or dropped replies it could not
   decrypt -- cannot rule out a child, so a caller that must not act on a wrong "no thread" asks
   for full history (``absence_needs_full_history``) and gets an unavailable proof instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto

from mindroom.matrix.event_info import EventInfo, event_type_supports_thread_relations
from mindroom.matrix.thread_diagnostics import is_thread_history_source_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult

type _ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type _EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type _ThreadRootProofLookup = Callable[[str, str], Awaitable[ThreadResolution]]
_MAX_THREAD_MEMBERSHIP_HOPS = 512

type _ThreadMessagesLookup = Callable[[str, str], Awaitable[ThreadHistoryResult]]


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
    def _threaded(cls, thread_id: str) -> ThreadResolution:
        """Return one positive thread-membership result."""
        return cls(ThreadResolutionState.THREADED, thread_id=thread_id)

    @classmethod
    def _room_level(cls) -> ThreadResolution:
        """Return one definite room-level result."""
        return cls(ThreadResolutionState.ROOM_LEVEL)

    @classmethod
    def _indeterminate(cls, error: Exception) -> ThreadResolution:
        """Return one unresolved result caused by proof failure."""
        return cls(ThreadResolutionState.INDETERMINATE, error=error)


class ThreadMembershipLookupError(RuntimeError):
    """Raised when related-event lookup cannot determine thread membership from available data."""


class RelatedEventUnavailableError(ThreadMembershipLookupError):
    """Raised when a related event can never place an event in a conversation.

    The homeserver will not serve it -- it does not exist, is not in this
    room, or is hidden from this account, for example history from before it
    joined -- or it is not a message and so belongs to no conversation. Asking
    again gets the same answer, so a caller that cannot proceed without the
    event has to end its work rather than retry it.
    """


@dataclass(frozen=True)
class ThreadMembershipAccess:
    """Repository-wide accessors used to resolve one event's thread membership."""

    lookup_thread_id: _ThreadIdLookup
    fetch_event_info: _EventInfoLookup
    prove_thread_root: _ThreadRootProofLookup


def _conversation_relation_thread_membership_access(
    access: ThreadMembershipAccess,
) -> ThreadMembershipAccess:
    """Reject non-message ancestors and stale indexes from conversation relation walks."""

    async def fetch_event_info(room_id: str, event_id: str) -> EventInfo | None:
        event_info = await access.fetch_event_info(room_id, event_id)
        if event_info is not None and not event_type_supports_thread_relations(event_info.event_type):
            msg = f"Related event {event_id} cannot carry conversation thread membership"
            raise RelatedEventUnavailableError(msg)
        return event_info

    async def lookup_thread_id(room_id: str, event_id: str) -> str | None:
        thread_id = await access.lookup_thread_id(room_id, event_id)
        if thread_id is None:
            return None
        event_info = await fetch_event_info(room_id, event_id)
        if event_info is None:
            msg = f"Indexed event {event_id} is unavailable for conversation thread validation"
            raise RelatedEventUnavailableError(msg)
        return thread_id

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=access.prove_thread_root,
    )


async def resolve_event_thread_membership(
    room_id: str,
    event_info: EventInfo,
    *,
    access: ThreadMembershipAccess,
    event_id: str | None = None,
) -> ThreadResolution:
    """Return canonical thread membership for one event.

    ``event_id`` names the event itself; when given, a relation-free event is proven as the root of its own thread.
    """
    if event_info.thread_id is not None:
        return ThreadResolution._threaded(event_info.thread_id)
    related_event_id = event_info.next_related_event_id("")
    if related_event_id is not None:
        return await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=access,
        )
    if event_id is not None and event_info.can_be_thread_root:
        return await access.prove_thread_root(room_id, event_id)
    return ThreadResolution._room_level()


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
            return ThreadResolution._threaded(thread_id)

        try:
            related_event_info = await access.fetch_event_info(room_id, current_event_id)
        except Exception as exc:
            # A related event nobody could read may be the thread's root, so its scope stays unknown.
            return ThreadResolution._indeterminate(exc)
        if related_event_info is None:
            # A missing related event may still be a thread root; unknown, never room level.
            return ThreadResolution._indeterminate(
                RelatedEventUnavailableError(f"Related event {current_event_id} is unavailable"),
            )

        if related_event_info.thread_id is not None:
            return ThreadResolution._threaded(related_event_info.thread_id)

        next_target = related_event_info.next_related_event_id(current_event_id)
        if next_target is not None:
            current_event_id = next_target
            continue

        if related_event_info.can_be_thread_root:
            return await access.prove_thread_root(room_id, current_event_id)
        break

    return ThreadResolution._room_level()


def thread_has_child(thread_messages: ThreadHistoryResult, thread_root_id: str) -> bool:
    """Return whether one thread's history holds a message other than its root, which proves the root."""
    return any(message.event_id != thread_root_id for message in thread_messages)


async def _thread_messages_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_messages: _ThreadMessagesLookup,
    absence_needs_full_history: bool,
) -> ThreadResolution:
    """Return one candidate root's membership from authoritative thread messages."""
    try:
        thread_messages = await fetch_thread_messages(room_id, thread_root_id)
    except Exception as exc:
        return ThreadResolution._indeterminate(exc)
    if is_thread_history_source_degraded(thread_messages):
        msg = "Thread root proof unavailable from degraded thread history"
        return ThreadResolution._indeterminate(RuntimeError(msg))
    if thread_has_child(thread_messages, thread_root_id):
        return ThreadResolution._threaded(thread_root_id)
    if absence_needs_full_history and not thread_messages.is_full_history:
        msg = "Thread root proof unavailable: no readable child, and the thread history may be incomplete"
        return ThreadResolution._indeterminate(RuntimeError(msg))
    return ThreadResolution._room_level()


def thread_messages_thread_membership_access(
    *,
    lookup_thread_id: _ThreadIdLookup,
    fetch_event_info: _EventInfoLookup,
    fetch_thread_messages: _ThreadMessagesLookup,
    absence_needs_full_history: bool,
) -> ThreadMembershipAccess:
    """Build shared membership access backed by authoritative thread messages.

    ``absence_needs_full_history`` has no default: whether a read with no
    readable child may disprove a root is the caller's policy (invariant 7).
    """

    async def prove_thread_root(room_id: str, thread_root_id: str) -> ThreadResolution:
        return await _thread_messages_root_proof(
            room_id,
            thread_root_id,
            fetch_thread_messages=fetch_thread_messages,
            absence_needs_full_history=absence_needs_full_history,
        )

    return _conversation_relation_thread_membership_access(
        ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
        ),
    )
