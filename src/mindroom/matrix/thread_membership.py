"""Canonical Matrix thread resolution.

Ownership map:
- canonical thread identity: this module (pure domain rules, no client or cache transport)
- turn-path membership access: `mindroom.conversation_resolver`
- membership access for tools and outbound mutations: `mindroom.matrix.projected_thread_membership`
- mutation/bookkeeping impact: `mindroom.matrix.thread_mutation_impact`
- tool-facing normalization: `mindroom.custom_tools.attachment_helpers`

Every root proof reads the candidate's thread from the conversation projection.

Invariants enforced here (every resolver in the repo must go through this module):

1. An event is THREADED if and only if one of the following holds:
   it carries a native ``m.thread`` relation (``EventInfo.thread_id``);
   a relation walk from it reaches an event carrying one;
   or the walk terminates at a relation-free event that is proven to have at least one real threaded child,
   in which case that terminal event is itself the thread root.
   Per MSC3440 only relation-free events (``can_be_thread_root``) may become roots.
   An ``m.thread`` relation written inside an edit's ``m.new_content`` (``EventInfo
   .thread_id_from_edit``) is never one of those things. Matrix applies ``m.new_content`` by
   keeping the original event's relation and ignoring every ``m.relates_to`` found there, so an
   edit is placed by the event it replaces: the walk hops to that original and resolves it, and an
   original nobody can read is INDETERMINATE like any other unavailable relation target.

2. The relation walk follows ``EventInfo.next_related_event_id``: edit original, then reaction target,
   then ``m.reference`` target, then reply target.
   An explicit thread relation always wins immediately; the walk never continues past one.
   This is how implied membership works: plain replies, references, and reactions to threaded events
   inherit the target's thread transitively.

3. The walk always terminates: a visited set breaks relation cycles and ``_MAX_THREAD_MEMBERSHIP_HOPS``
   caps pathological chains, resolving to the best answer found so far (initially ROOM_LEVEL).

4. Root proof is three-valued (PROVEN, NOT_A_THREAD_ROOT, PROOF_UNAVAILABLE) and proof failure never
   silently demotes to room level.
   PROOF_UNAVAILABLE maps to ``ThreadResolution._indeterminate`` with the candidate root preserved so
   callers can fail closed: tools and outbound mutations refuse, and ingress retries a non-blocking
   read strictly, then refuses the event until its scope can be decided.
   Lookup failures and missing related events during the walk are likewise INDETERMINATE, never ROOM_LEVEL.
   A related event the homeserver will not serve, or one that is not a message, carries
   ``RelatedEventUnavailableError``, which no retry changes.

5. Child proof and relation ancestry accept only ``m.room.message`` and ``m.room.encrypted`` events.
   Child proof also excludes the candidate root itself and edits of the root: an ``m.replace`` of a
   relation-free event does not make that event a thread root.

6. A root proof built from thread history whose read source is the explicit degraded fallback
   (``THREAD_HISTORY_SOURCE_DEGRADED``, i.e. an empty fail-open read) is PROOF_UNAVAILABLE, never
   NOT_A_THREAD_ROOT: an empty degraded read must not demote an existing thread to room level.
   A thread read that raises is PROOF_UNAVAILABLE too.

7. A read with no readable child proves NOT_A_THREAD_ROOT only as far as the caller accepts it.
   History that may be incomplete -- hydration stopped at a ceiling or dropped replies it could not
   decrypt -- cannot rule out a child, so a caller that must not act on a wrong "no thread" asks
   for full history (``absence_needs_full_history``) and gets PROOF_UNAVAILABLE instead.
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
type _ThreadRootProofLookup = Callable[[str, str], Awaitable["_ThreadRootProof"]]
_MAX_THREAD_MEMBERSHIP_HOPS = 512

type _ThreadMessagesLookup = Callable[[str, str], Awaitable[ThreadHistoryResult]]


class _ThreadRootProofState(Enum):
    """Outcome of proving whether one candidate event is a real thread root."""

    PROVEN = auto()
    NOT_A_THREAD_ROOT = auto()
    PROOF_UNAVAILABLE = auto()


@dataclass(frozen=True)
class _ThreadRootProof:
    """Result of one thread-root proof attempt."""

    state: _ThreadRootProofState
    error: Exception | None = None

    @classmethod
    def proven(cls) -> _ThreadRootProof:
        """Return a successful root proof."""
        return cls(_ThreadRootProofState.PROVEN)

    @classmethod
    def not_a_thread_root(cls) -> _ThreadRootProof:
        """Return a definite non-thread-root result."""
        return cls(_ThreadRootProofState.NOT_A_THREAD_ROOT)

    @classmethod
    def proof_unavailable(cls, error: Exception) -> _ThreadRootProof:
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
    candidate_thread_root_id: str | None = None
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
    def _indeterminate(
        cls,
        error: Exception,
        candidate_thread_root_id: str | None = None,
    ) -> ThreadResolution:
        """Return one unresolved result caused by proof failure."""
        return cls(
            ThreadResolutionState.INDETERMINATE,
            candidate_thread_root_id=candidate_thread_root_id,
            error=error,
        )


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


def _resolution_from_root_proof(
    thread_root_id: str,
    proof: _ThreadRootProof,
) -> ThreadResolution:
    """Convert one root proof result into canonical thread membership."""
    if proof.state is _ThreadRootProofState.PROVEN:
        return ThreadResolution._threaded(thread_root_id)
    if proof.state is _ThreadRootProofState.NOT_A_THREAD_ROOT:
        return ThreadResolution._room_level()
    assert proof.error is not None
    return ThreadResolution._indeterminate(proof.error, candidate_thread_root_id=thread_root_id)


async def resolve_event_thread_membership(
    room_id: str,
    event_info: EventInfo,
    *,
    access: ThreadMembershipAccess,
    event_id: str | None = None,
    allow_current_root: bool = False,
) -> ThreadResolution:
    """Return canonical thread membership for one event."""
    if event_info.thread_id is not None:
        return ThreadResolution._threaded(event_info.thread_id)
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
    resolution = ThreadResolution._room_level()

    for _ in range(_MAX_THREAD_MEMBERSHIP_HOPS):
        if current_event_id in visited_event_ids:
            break
        visited_event_ids.add(current_event_id)

        thread_id = await access.lookup_thread_id(room_id, current_event_id)
        if thread_id is not None:
            resolution = ThreadResolution._threaded(thread_id)
            break

        try:
            related_event_info = await access.fetch_event_info(room_id, current_event_id)
        except Exception as exc:
            # Keep lookup-failed related events separately scoped for dispatch coalescing and replay checks.
            resolution = ThreadResolution._indeterminate(exc, candidate_thread_root_id=current_event_id)
            break
        if related_event_info is None:
            # Missing related events are still possible thread roots; demote later without losing the candidate.
            resolution = ThreadResolution._indeterminate(
                RelatedEventUnavailableError(f"Related event {current_event_id} is unavailable"),
                candidate_thread_root_id=current_event_id,
            )
            break

        if related_event_info.thread_id is not None:
            resolution = ThreadResolution._threaded(related_event_info.thread_id)
            break

        next_target = related_event_info.next_related_event_id(current_event_id)
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


async def _thread_messages_root_proof(
    room_id: str,
    thread_root_id: str,
    *,
    fetch_thread_messages: _ThreadMessagesLookup,
    absence_needs_full_history: bool,
) -> _ThreadRootProof:
    """Return one root-proof result from authoritative thread messages."""
    try:
        thread_messages = await fetch_thread_messages(room_id, thread_root_id)
    except Exception as exc:
        return _ThreadRootProof.proof_unavailable(exc)
    if is_thread_history_source_degraded(thread_messages):
        msg = "Thread root proof unavailable from degraded thread history"
        return _ThreadRootProof.proof_unavailable(RuntimeError(msg))
    if any(message.event_id != thread_root_id for message in thread_messages):
        return _ThreadRootProof.proven()
    if absence_needs_full_history and not thread_messages.is_full_history:
        msg = "Thread root proof unavailable: no readable child, and the thread history may be incomplete"
        return _ThreadRootProof.proof_unavailable(RuntimeError(msg))
    return _ThreadRootProof.not_a_thread_root()


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

    async def prove_thread_root(room_id: str, thread_root_id: str) -> _ThreadRootProof:
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
