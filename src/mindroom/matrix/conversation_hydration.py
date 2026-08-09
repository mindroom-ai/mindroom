"""Building a conversation from the server, once per membership.

Hydration is the only thing in MindRoom that reads history from Matrix, and it
runs at most once per conversation per membership. There is no periodic repair
scan and no room-wide fallback: if hydration fails, the read fails, which is
visible. A background repairer that quietly half-works is not.

The same code path serves three callers — first read of a conversation, the
point refetch owed after the visible revision of a message was redacted, and
the room walk owed after sync skipped a gap it could not rebuild — so there is
exactly one implementation of "ask the server what this looks like now".

The third caller is why hydration runs at all for a conversation that already
has a marker. A skipped gap makes the projection wrong rather than merely
short, and the store answers "not hydrated" for every conversation in a room
with a repairable obligation precisely so the next read comes back here. Repair
happens on that read and nowhere else, for the same reason nothing else here is a
background pass: an unreachable homeserver should degrade a read that someone
is waiting for, not accumulate retry state nobody is watching.

"Once per membership" is a statement about what a marker discharges, and a
marker only discharges the question its walk answered. A bounded walk answers
"is this conversation recent", which is all a prompt asks. A caller whose
correctness is completeness rather than recency asks a strictly harder
question, reads the same principal's projection, and always arrives second --
so `require_complete` lets it walk past a marker earned under smaller bounds
rather than inherit an answer to a question it did not ask.

The marker therefore records which policy the widest walk here ran under, not
only whether one reached the start. Without that, "walk past a marker earned
under smaller bounds" becomes "walk again on every read" the moment the bounds
already spent are this caller's own, which for a permanently oversized thread
is the entire maximum walk, every time, forever. A policy and not one of its
ceilings, because a caller is defined by all three of its ceilings at once and
two policies can differ on any one of them.

That record is a cost decision and never a proof. A walk that stopped at a
ceiling has not shown that a wider one would stop there too, and pagination
that collapses superseded edits can carry the same policy further tomorrow. It
says only that paying again for a walk no wider than one already tried here is
not worth it; completeness is a separate fact, and export reads that one.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio

from mindroom.event_journal import (
    HistoryRecoveryOutcome,
    HistoryRecoveryState,
    HydrationPolicy,
    ProjectedEvent,
    RefreshRequest,
    replacement_target,
    thread_root,
    visible_content,
)
from mindroom.event_journal.projection import is_newer_revision
from mindroom.logging_config import get_logger
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.sidecar_content import holds_unresolved_sidecar
from mindroom.matrix.transport_progress import is_transport_progress_revision
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from mindroom.event_journal import HydrationView, RefreshRequest, RoomHistoryRecovery

logger = get_logger(__name__)

# Requiring zero means "the server must tell us it recursed", and that is the
# strongest portable requirement there is.
#
# The two servers MindRoom runs against report different things under the same
# name. Synapse returns the constant 3 — the depth it is willing to traverse.
# Tuwunel returns the depth of the deepest event it actually returned, so a
# root with one reply and one edit of that reply reports 1, verified against a
# live server. Any numeric floor above zero would therefore reject ordinary
# complete pages on Tuwunel while proving nothing on Synapse.
#
# What is worth catching is a server that ignores `recurse` entirely and
# silently returns only direct children: it omits the field, and a caller that
# accepted that would quietly lose every edit hanging off a threaded reply.
_REQUIRED_RECURSION_DEPTH = 0

_MESSAGES_PAGE_LIMIT = 100
# How much of a room hydration is for: enough recent logical messages to fill
# the largest prompt the runtime will build, and no more. The projection is a
# prompt view, not a Matrix replica, so "hydrated" means "the window a prompt
# can read is present" rather than "this room is fully mirrored". A caller that
# needs older history than this paginates Matrix directly.
HYDRATED_PROMPT_WINDOW_MESSAGES = 2_000
# Raw Matrix events and logical messages are not the same quantity, and in this
# product they are not even the same order of magnitude: a streamed answer is
# one original followed by a long tail of `m.replace` edits, all of which
# reduce to a single line in a prompt. Counting pages would therefore have
# hydration stop at a window that is mostly edits — a handful of messages in an
# edit-heavy room. The window is counted in logical messages, and this ceiling
# exists only so that one pathological room cannot walk its entire history.
#
# Reaching it means the window is short, and the conversation is still marked
# hydrated. That is the deliberate trade and it is worth being explicit about:
# the marker records that the one-time walk ran to completion, not that a
# particular number of messages exists. Withholding it would re-run a
# twenty-thousand-event walk on every single read of that room, which is a far
# worse outcome than a prompt with less history than its maximum.
_MAX_FETCHED_EVENTS = 20_000
# A separate bound, because it measures a different thing. Deriving it from the
# event ceiling made a room that returns one event per page stop after two
# pages while reporting that it had read twenty thousand events.
#
# This one is the room walk's alone. A thread is a single
# `room_get_event_relations` call whose pagination happens inside nio, which
# yields events rather than pages, so there is no request for this side of the
# code to count. See `_fetch_relations`.
_MAX_MESSAGES_REQUESTS = 400

# Membership can move while a walk is in flight, refusing its install. Retrying
# under the fresh epoch is almost always enough; a room whose membership keeps
# moving is one the bot cannot get a stable view of, and a strict caller is
# better served by an error than by a page it cannot vouch for.
_HYDRATION_EPOCH_ATTEMPTS = 3


class _HydrationError(RuntimeError):
    """A conversation could not be built from the server."""


def _is_redacted(source: Mapping[str, object]) -> bool:
    unsigned = source.get("unsigned")
    return isinstance(unsigned, dict) and "redacted_because" in unsigned


def _redaction_target(event: nio.Event) -> str | None:
    """Return the event one fetched event deletes, if it deletes one.

    A walk meets a deletion in two shapes and both mean the same thing. The
    ``m.room.redaction`` event itself appears in ``/messages`` like any other
    timeline event; nio's schema requires it to name its target, and room
    version 11 moved ``redacts`` into content while servers still serve the
    top-level key over the client-server API, which is what nio parses.

    The other shape is the deleted event itself, which comes back with its
    content stripped and ``redacted_because`` in ``unsigned``. That one carries
    the whole fact on its own, and for a thread walk it is the only shape there
    is: a redaction carries no ``m.relates_to``, so it is not in any relation
    tree. Reading it as a redaction of itself is what makes both walks agree.

    The second shape is confined to events this projection could be storing. A
    redaction preserves its target's ``type``, so the type check is enough to
    say so, and without it a redacted reaction or state event would tombstone
    itself -- a durable row naming an event no visible message ever had, which
    deletes nothing and has to be read past forever after.
    """
    if isinstance(event, nio.RedactionEvent):
        return event.redacts
    if event.source.get("type") != "m.room.message":
        return None
    return event.event_id if _is_redacted(event.source) else None


def _readable_event(client: nio.AsyncClient, event: nio.BaseEvent) -> nio.Event | None:
    """Return one fetched event in the clear, or nothing if it stayed unreadable.

    ``room_get_event_relations`` is why this exists. nio decrypts what
    ``receive_response`` recognizes -- a ``/messages`` chunk, a context
    response, a single fetched event -- but that chain has no branch for a
    relations response, so nio yields those events exactly as they came off the
    wire. In an encrypted room every relation therefore arrives as a
    ``MegolmEvent`` no matter how many keys this device holds, and a
    ``MegolmEvent`` projects to nothing: its ``source`` type is
    ``m.room.encrypted``, not ``m.room.message``.

    The reads nio already decrypts are run through this too. It returns them
    untouched, and the boundary stays one rule instead of a list of which
    responses nio happens to handle -- a list that is only correct until nio's
    chain changes.

    Decrypting can still fail after being attempted, because a key for a
    session this device never received is not going to appear. So the answer is
    "readable or not" rather than "decrypted or not": a caller needs to know an
    event was dropped unread, and none of them can do anything about why.
    """
    if not isinstance(event, nio.Event):
        return None
    if not isinstance(event, nio.MegolmEvent):
        return event
    if client.olm is None:
        return None
    try:
        decrypted = client.decrypt_event(event)
    except nio.EncryptionError:
        return None
    # A payload that decrypted into something malformed comes back as a
    # `BadEvent`, which nio deliberately does not make an `Event`. Unreadable is
    # the honest answer for it as well.
    return decrypted if isinstance(decrypted, nio.Event) else None


def _projected_from_event(room_id: str, event: nio.Event, *, self_sender: str) -> ProjectedEvent | None:
    """Return the projection view of one fetched event, or nothing.

    A deletion projects as a deletion, under exactly the semantics live
    admission gives one, because the projection is what has to end up the same
    either way. Dropping it instead -- which is what "the server already
    stripped the body" argued for -- left every redaction that happened inside
    a skipped gap unapplied: the original or edit it deleted had already been
    projected from before the gap, and nothing in the walk would otherwise
    remove it before the recovery obligation was settled.

    This bot's own in-flight streaming edits are dropped for the other reason
    the projection exists: they are transport. The same rule runs here and at
    live admission, because a cold read fetches the whole relation tree and
    would otherwise reinstall every progress edit the live path just declined.
    """
    content = event.source.get("content")
    content = content if isinstance(content, dict) else {}
    redacts = _redaction_target(event)
    if redacts is not None:
        return ProjectedEvent(
            event_id=event.event_id,
            room_id=room_id,
            thread_id=thread_root(content),
            sender=event.sender,
            origin_server_ts=event.server_timestamp,
            content=content,
            replaces_event_id=None,
            redacts_event_id=redacts,
        )
    if not content or event.source.get("type") != "m.room.message":
        return None
    projected = ProjectedEvent(
        event_id=event.event_id,
        room_id=room_id,
        thread_id=thread_root(content),
        sender=event.sender,
        origin_server_ts=event.server_timestamp,
        content=content,
        replaces_event_id=replacement_target(content),
        redacts_event_id=None,
    )
    if is_transport_progress_revision(projected, self_sender=self_sender):
        return None
    return projected


def _is_logical_message(projected: ProjectedEvent) -> bool:
    """Return whether one projection adds a message to a walk's window.

    An edit revises a message and a redaction removes one, so neither is a
    message the window is spent on. Counting a redaction would shorten every
    ordinary walk of a room by however many deletions it has seen. Proof-mode
    recovery ignores the prompt window entirely.
    """
    return projected.replaces_event_id is None and projected.redacts_event_id is None


def _advanced_room_cursor(*, room_id: str, start: str | None, end: str) -> str:
    """Return an advancing room pagination cursor, or fail the stalled walk."""
    if end == start:
        # Neither server MindRoom runs signals exhaustion this way. Tuwunel
        # derives `end` from the last event it returned and so omits it entirely
        # for an empty page; Synapse omits it too, explicitly, when pagination
        # found nothing. A repeated token is therefore a stall, not the start
        # of history.
        #
        # Failing is right, and the earlier reasoning that it was too harsh
        # missed why: returning here would install a hydration marker, and
        # hydration runs once per membership, so a single transient stall would
        # become permanent truncation. Raising leaves the conversation
        # unhydrated, which is the only outcome a later read can still repair.
        msg = f"Homeserver repeated pagination token {start!r} for {room_id!r} instead of advancing"
        raise _HydrationError(msg)
    return end


@dataclass(frozen=True, slots=True)
class _Walk:
    """What one hydration walk collected, and whether it reached the end.

    ``complete`` says the walk ran out of conversation rather than out of
    allowance. It is not the same statement as the hydration marker, which only
    records that the one-time walk ran: a bounded walk that stopped at the
    prompt window is hydrated and is not complete, and a reader whose
    correctness is completeness rather than recency has to be able to tell those
    apart instead of reading a warm marker as a whole conversation.

    ``exhausted_server`` says the homeserver returned no continuation token.
    It is independent of readability: a walk can reach the start of retained
    history while still being unable to understand an encrypted event it saw.

    ``unreadable`` says the walk fetched at least one event it could not read,
    which is a different failure from either bound and has to be kept apart
    from both. A walk that stopped at a ceiling knows exactly what it skipped
    and could fetch it by paying more; a walk that could not decrypt an event
    has already paid and still does not know what it holds.

    It forces ``complete`` down, because a conversation missing an event nobody
    could read is not whole. It is kept as its own field anyway, because
    "stopped early" and "read everything and understood some of it" call for
    opposite responses from a point refetch: the first still found the newest
    revision, since relations arrive newest first, and the second may have
    dropped exactly the edit it was sent to fetch.
    """

    events: tuple[ProjectedEvent, ...]
    complete: bool
    exhausted_server: bool = False
    unreadable: bool = False


@dataclass(frozen=True, slots=True)
class _Revision:
    """The revision of a logical message that is currently on the server."""

    event_id: str
    origin_server_ts: int
    content: Mapping[str, object]


def _reduce_current_revision(
    original: ProjectedEvent,
    relations: Sequence[ProjectedEvent],
) -> _Revision:
    """Return the revision the server would show for one logical message.

    Uses the same ordering rule as the live projection, so a refetched message
    and a message built from live events cannot disagree about which edit won.
    """
    winner = _Revision(
        event_id=original.event_id,
        origin_server_ts=original.origin_server_ts,
        content=visible_content(original.content),
    )
    for relation in relations:
        if relation.replaces_event_id != original.event_id:
            continue
        if relation.sender != original.sender:
            continue
        if not is_newer_revision(
            (relation.origin_server_ts, relation.event_id),
            (winner.origin_server_ts, winner.event_id),
        ):
            continue
        winner = _Revision(
            event_id=relation.event_id,
            origin_server_ts=relation.origin_server_ts,
            content=visible_content(relation.content),
        )
    return winner


@dataclass
class ConversationHydrator:
    """One-time conversation hydration and point refetch against Matrix."""

    store: HydrationView
    # The runtime view rather than a client, because a client does not exist
    # when the bot assembles its collaborators: it arrives at login. This is
    # the same indirection the delivery gateway uses for the same reason.
    runtime: SupportsClientConfig
    # This bot's raw Matrix user ID, the same value live admission compares
    # senders against, so a refetched conversation reduces to what the live
    # projection would have held.
    self_sender: str
    required_recursion_depth: int = _REQUIRED_RECURSION_DEPTH
    # Which named set of bounds the three ceilings below belong to. It is what
    # a walk writes down about itself, and the three numbers are what the walk
    # actually spends -- recorded as a name because a caller is defined by all
    # three at once, and any single one of them read back later cannot tell two
    # policies apart that differ only on one of the others. A caller whose
    # bounds are not one of these named sets has to name a new policy and rank
    # it, which is the point: the ordering is declared rather than inferred
    # from whichever ceiling happened to get stored.
    policy: HydrationPolicy = HydrationPolicy.PROMPT
    prompt_window_messages: int = HYDRATED_PROMPT_WINDOW_MESSAGES
    max_fetched_events: int = _MAX_FETCHED_EVENTS
    max_requests: int = _MAX_MESSAGES_REQUESTS
    # Whether a marker left by a walk that stopped at a ceiling is good enough.
    #
    # It is for a prompt, and saying so is the whole reason the short-circuit
    # can stay cheap. It is not for a caller whose correctness is completeness,
    # and that caller shares this projection: the principal an export reads is
    # the one the running bot writes, deliberately, because that sharing is what
    # makes a warm export cost no Matrix calls. The consequence was that the
    # prompt path always got there first, so a hydrator configured with larger
    # bounds never once used them and every thread the bot had answered in was
    # unexportable until a rejoin moved the membership epoch.
    require_complete: bool = False
    _in_flight: dict[tuple[str, str | None], asyncio.Task[None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    # Recovery walks are shared per room rather than per conversation, because
    # one room walk repairs the whole room. Kept apart from the conversation
    # tasks rather than squeezed into their key: a recovery and the room
    # conversation are both "this room, no thread", and one of them waiting on
    # the other under a shared key is a deadlock.
    _recoveries: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)

    def _client(self) -> nio.AsyncClient:
        """Return the Matrix client, which only exists once the bot has logged in.

        Held as the runtime view rather than a client because the bot assembles
        its collaborators before it logs in, the same indirection the delivery
        gateway uses for the same reason.
        """
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation hydration"
            raise RuntimeError(msg)
        return client

    async def ensure_hydrated(self, *, room_id: str, thread_id: str | None) -> None:
        """Hydrate a conversation once, sharing one task among concurrent readers.

        Returning means a hydration marker exists for the *current* membership,
        and that is checked rather than assumed. A walk installs nothing when
        membership moved while it was in flight, and the shared task is keyed by
        conversation alone, so a reader that arrives in the new epoch joins the
        old epoch's walk and is handed its result. Both then believed a
        conversation was hydrated when the install had been refused.

        Silently, too: a missing hydration row is not a truncation, so the
        prompt path reads it as a whole conversation and a model answers from a
        page with history missing from behind it. Membership churn is exactly
        when that history matters.

        So each attempt re-checks the durable marker, and a walk refused by the
        epoch it was launched under is retried under the current one. Bounded,
        because membership that keeps moving is a room the bot cannot get a
        stable view of, and failing closed there is the safe direction -- a
        strict caller gets an error instead of a page it cannot vouch for.

        ``require_complete`` adds the one other reason to walk a conversation
        that already has a marker: the marker vouches for a bounded walk and
        this caller cannot use a suffix. That is owed exactly once, and once is
        counted durably rather than in a local variable, because a strict
        caller arrives in a fresh process and a fresh hydrator every time it
        runs. A second pass would re-read the same ceiling and reinstall the
        same marker, and both a thread past the larger bounds and an unknown-gap
        recovery that spent its ceiling are durable cost decisions. Whether the
        deeper walk reached the start is then a fact for the caller to judge,
        not a reason to walk again.
        """
        for _ in range(_HYDRATION_EPOCH_ATTEMPTS):
            recovery = await self.store.room_history_recovery(room_id)
            if recovery is not None and recovery.state is HistoryRecoveryState.REPAIRABLE:
                # Shared per room, so two readers in two threads of a repairable
                # room walk it once between them rather than once each.
                await self._shared(
                    self._recoveries,
                    room_id,
                    lambda: self._repair(recovery),  # noqa: B023 - awaited before the next iteration rebinds it
                    name=f"repair_room_history_{room_id}",
                )
                if thread_id is None:
                    # The recovery walked the room conversation and installed
                    # it, under this hydrator's own bounds, so it is also the
                    # deeper walk a strict caller was owed -- and it recorded
                    # those bounds, so the loop's own check sees that. Walking
                    # it again here would fetch the same pages a second time to
                    # reach the same rows.
                    continue
            if await self._hydration_stands(room_id=room_id, thread_id=thread_id):
                return
            await self._shared(
                self._in_flight,
                (room_id, thread_id),
                lambda: self._hydrate(room_id=room_id, thread_id=thread_id),
                name=f"hydrate_conversation_{room_id}",
            )
        if await self.store.conversation_is_hydrated(room_id=room_id, thread_id=thread_id):
            return
        msg = f"Conversation hydration kept losing its membership epoch for {room_id} thread {thread_id}"
        raise _HydrationError(msg)

    async def _hydration_stands(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether the stored marker already answers what this caller asked.

        A prompt asks for recency, and any marker for the current membership
        answers it. A caller that needs the whole conversation asks a strictly
        harder question, and it is satisfied two ways: a walk ran out of
        conversation, or a walk under a policy ranked at least as wide as this
        one already ran and did not. Either way it is one store read.

        The two halves are not the same kind of statement and only the first is
        a guarantee. Reaching the start of the conversation is permanent. A
        spent policy is a cost decision: it does not say a wider walk would get
        no further -- the servers MindRoom runs against collapse superseded
        `m.replace` events out of pagination, so the same policy can carry a
        later walk past a ceiling it hit today -- it says only that paying for
        a walk no wider than one already tried here is not worth it. Which is
        why the caller's own completeness check reads `complete` and this is
        never allowed to stand in for it.

        The second half is still what keeps "owed exactly once" true across
        calls. Without it a thread past even this caller's bounds is walked to
        the same ceiling on every single read -- at export's policy, millions
        of fetched events each time -- because nothing durable distinguishes a
        conversation whose deepest walk has already been spent from one nobody
        walked deeply at all.

        Neither half is `conversation_is_complete`, which also answers no when
        an unknown-gap recovery spent its ceiling. That truncation is real and
        export must still refuse on it, but repeating the same bounded walk on
        every read is not useful.
        """
        if not self.require_complete:
            return await self.store.conversation_is_hydrated(room_id=room_id, thread_id=thread_id)
        coverage = await self.store.conversation_hydration_coverage(room_id=room_id, thread_id=thread_id)
        if coverage is None:
            return False
        return coverage.reached_its_end or coverage.attempted_policy_rank >= self.policy

    async def _shared[Key](
        self,
        running: dict[Key, asyncio.Task[None]],
        key: Key,
        start: Callable[[], Coroutine[None, None, None]],
        *,
        name: str,
    ) -> None:
        """Run one keyed piece of server work, joining whoever is already on it."""
        task = running.get(key)
        if task is None or task.done():
            task = asyncio.create_task(start(), name=name)
            running[key] = task
        try:
            await asyncio.shield(task)
        finally:
            if running.get(key) is task and task.done():
                del running[key]

    async def _hydrate(self, *, room_id: str, thread_id: str | None) -> None:
        if await self._hydration_stands(room_id=room_id, thread_id=thread_id):
            # A concurrent reader finished this walk while this one was still on
            # its way here. `ensure_hydrated` checks the marker before it awaits
            # anything, but it awaits twice afterwards, and `_shared` can only
            # join a task it can still see -- a finished one has already been
            # dropped. So the durable marker is rechecked here, where it is the
            # last word, rather than trusting arrival order to a walk that costs
            # server requests. This is the contract, not an optimization: a
            # conversation is hydrated at most once per membership epoch.
            #
            # Rechecked against the same question the caller asked, because a
            # marker that only vouches for a bounded walk does not discharge a
            # strict caller's walk any more than it discharged its short-circuit.
            return
        epoch = await self.store.membership_epoch(room_id)
        walk = (
            await self._fetch_thread(room_id, thread_id) if thread_id is not None else await self._fetch_room(room_id)
        )
        installed = await self.store.install_hydrated_conversation(
            room_id=room_id,
            thread_id=thread_id,
            events=walk.events,
            complete=walk.complete,
            attempted_policy_rank=self.policy,
            expected_membership_epoch=epoch,
        )
        if not installed:
            # Membership moved while the fetch was in flight, so this view is of
            # a room the bot is no longer in the same relationship with.
            logger.info("conversation_hydration_superseded", room_id=room_id, thread_id=thread_id)

    async def _repair(self, recovery: RoomHistoryRecovery) -> None:
        """Walk a repairable room to server exhaustion or a configured ceiling.

        A room walk and not a thread walk, whichever conversation asked. The
        skipped gap is a range of the room's timeline, and ``/messages`` returns
        every event in it -- threaded replies included, each projected under the
        thread it belongs to -- so one walk repairs every conversation the hole
        touched. A thread's relation tree could never prove the same thing: it
        says what that thread contains, not what the room received.

        The prompt window is the wrong bound for this job: a busy room can fill
        it entirely with post-gap tail while the missing interval remains on
        the next page. The proof walk therefore continues to readable server
        exhaustion, while the raw-event and request ceilings still bound cost.

        A failure here propagates. The read that triggered it fails visibly, the
        obligation stays repairable, and the next read tries again -- which is the
        same contract every other hydration failure follows, and the reason
        there is no retry state to leak.
        """
        if await self.store.room_history_recovery(recovery.room_id) != recovery:
            # Another reader already settled this. `_shared` only joins readers
            # that overlap in time: the caller read this obligation before that
            # settlement committed, and by the time it got here the finished
            # task had already been dropped from the map, so there was nothing
            # left to join. Walking anyway pages the entire room a second time
            # for a question that has been answered; the store's exact CAS would
            # refuse to install it, so every request would be spent on nothing.
            #
            # Rechecked here, inside the shared task, rather than at the call
            # site: the durable obligation is the serialization that already exists,
            # and a store read cannot re-enter `_shared` and wait on the task it
            # is running inside.
            #
            # A *different* obligation is superseding for the same reason a stale one
            # is -- a later gap is a different hole and this walk was not
            # launched for it -- and the caller's loop re-reads the obligation.
            logger.info("conversation_history_recovery_already_settled", room_id=recovery.room_id)
            return
        epoch = await self.store.membership_epoch(recovery.room_id)
        walk = await self._fetch_room(recovery.room_id, require_server_exhaustion=True)
        if walk.exhausted_server and walk.unreadable:
            msg = f"Could not prove complete readable history for {recovery.room_id!r}: unreadable events remain"
            raise _HydrationError(msg)
        outcome = await self.store.settle_room_history_recovery(
            recovery,
            events=walk.events,
            exhausted_server=walk.exhausted_server,
            attempted_policy_rank=self.policy,
            expected_membership_epoch=epoch,
        )
        log = logger.info
        if outcome is HistoryRecoveryOutcome.TRUNCATED:
            # Not loss, but not nothing: the room is short until a walk
            # gets further back, and nothing schedules that on its own.
            log = logger.warning
        log(
            "conversation_history_recovery_settled",
            room_id=recovery.room_id,
            outcome=outcome.value,
            recovery_state=recovery.state.value,
            exhausted_server=walk.exhausted_server,
            unreadable=walk.unreadable,
            walk_complete=walk.complete,
        )

    async def _fetch_thread(self, room_id: str, thread_id: str) -> _Walk:
        """Build one thread from its root and a bounded walk of its relations.

        The room walk's ceilings apply here, but they do not carry over
        mechanically, because a thread has no "paginate backwards until the
        window fills" shape of its own.

        The window still counts logical messages, and the relation walk is what
        it bounds. The root is kept over and above it: a thread that starts at
        its first reply is missing the message the whole thread is about, so it
        is not a message the window is allowed to spend itself on.

        The event ceiling still counts raw relation events, and it is the one
        that matters most here. MindRoom streams by editing, so the relation
        tree of a thread of streamed answers is an order of magnitude larger
        than its logical message count, and all of it used to be accumulated in
        one list and written in one projection transaction.

        The request ceiling has no counterpart. This is a single
        ``room_get_event_relations`` call; nio paginates inside it and yields
        events, not pages, so there is nothing here to count.
        """
        root = await self._client().room_get_event(room_id, thread_id)
        if not isinstance(root, nio.RoomGetEventResponse):
            msg = f"Could not fetch thread root {thread_id!r}: {root}"
            raise _HydrationError(msg)
        events: list[ProjectedEvent] = []
        readable_root = _readable_event(self._client(), root.event)
        root_projected = (
            None
            if readable_root is None
            else _projected_from_event(room_id, readable_root, self_sender=self.self_sender)
        )
        if root_projected is not None:
            events.append(root_projected)
        relations = await self._fetch_relations(
            room_id,
            thread_id,
            window_messages=self.prompt_window_messages,
        )
        # A thread whose root could not be read is missing the message the whole
        # thread is about, which is the one event this walk refuses to spend its
        # window on precisely because a thread without it is not the thread.
        # The relation walk has already accounted for its own unread events in
        # both fields, so the root is all there is left to add here.
        return _Walk(
            events=(*events, *relations.events),
            complete=relations.complete and readable_root is not None,
            unreadable=relations.unreadable or readable_root is None,
        )

    async def _fetch_relations(
        self,
        room_id: str,
        event_id: str,
        *,
        window_messages: int | None,
    ) -> _Walk:
        """Walk the relation tree newest first, without filtering by relation type.

        Filtering by ``m.thread`` would miss the edits and replies hanging off
        thread members, which is exactly the content a conversation is made of.

        Newest first is what makes stopping early safe, and it is asked for
        explicitly rather than inherited from nio's default because the whole
        truncation rests on it. MSC3981 has the server return relations in the
        same topological order ``/messages`` would give for the same direction,
        and an edit is sent after the message it revises, so every edit of a
        message arrives before that message does. The room walk's backwards
        pagination already rests on exactly this and for exactly this reason.

        So the unit the window is applied to is the logical message, and the
        only place the walk may stop for it is the moment one has just been
        admitted: at that point its whole edit tail is already collected, and a
        message can never be installed at a stale revision. The event ceiling
        can stop mid-message, and under this order that direction is the harmless
        one -- it drops an original and keeps edits nothing will claim, rather
        than keeping a message and dropping the edits that supersede it.

        ``window_messages`` is ``None`` for a point refetch, which is one logical
        message and has no window: a threaded reply among its relations must not
        end the walk before the edit it came for arrives.
        """
        events: list[ProjectedEvent] = []
        admitted = 0
        fetched = 0
        complete = True
        unreadable = False
        client = self._client()
        relations = client.room_get_event_relations(
            room_id=room_id,
            event_id=event_id,
            direction=nio.MessageDirection.back,
            recurse=True,
            minimum_recursion_depth=self.required_recursion_depth,
        )
        try:
            # Closed explicitly, because every exit below but exhaustion leaves
            # the server mid-tree and nio's generator holding a page it will
            # never be asked for.
            async with contextlib.aclosing(relations):
                async for event in relations:
                    fetched += 1
                    readable = _readable_event(client, event)
                    if readable is None:
                        # nio hands relations over exactly as they arrived, so
                        # in an encrypted room this is every one of them until
                        # the decryption above succeeds -- and it still is for
                        # any session whose key never reached this device.
                        #
                        # Saying so is what separates a short answer from a
                        # wrong one. Every other reason an event projects to
                        # nothing is a decision this walk made: a reaction, a
                        # state event, this bot's own streaming frames. Those
                        # are events the conversation does not want. An event
                        # that could not be read is one the conversation may
                        # well want, dropped without knowing, and a walk that
                        # kept `complete` through it would install a thread
                        # holding only its root, mark it whole for the entire
                        # membership epoch, and hand that to an export as the
                        # conversation.
                        unreadable = True
                        complete = False
                    else:
                        projected = _projected_from_event(room_id, readable, self_sender=self.self_sender)
                        if projected is not None:
                            events.append(projected)
                            if _is_logical_message(projected):
                                admitted += 1
                                if window_messages is not None and admitted >= window_messages:
                                    complete = False
                                    break
                    if fetched >= self.max_fetched_events:
                        complete = False
                        # Said out loud for the same reason the room walk says
                        # it: this is not the window being met, it is a
                        # conversation whose remaining relations cost more than
                        # they are worth to a prompt.
                        logger.warning(
                            "conversation_hydration_ceiling_reached",
                            room_id=room_id,
                            event_id=event_id,
                            fetched_events=fetched,
                            logical_messages=admitted,
                            prompt_window_messages=window_messages,
                        )
                        break
        except nio.InsufficientRecursionDepthError as error:
            msg = (
                f"Homeserver returned related events without reporting a recursion depth "
                f"({error.reported!r}), so it did not honor the recursive request and the "
                f"conversation would be missing indirectly related events"
            )
            raise _HydrationError(msg) from error
        return _Walk(events=tuple(events), complete=complete, unreadable=unreadable)

    async def _fetch_room(
        self,
        room_id: str,
        *,
        require_server_exhaustion: bool = False,
    ) -> _Walk:
        """Walk back until this walk's job is done, or the room runs out.

        A server that has run out of history answers with an empty chunk and no
        ``end`` token. That is successful exhaustion, not a failure, and
        treating it as one is what used to leave rooms permanently unready.

        Stopping at the window is the whole point rather than a shortfall: what
        hydration promises is the range a prompt can read, so a room with more
        history than that is hydrated once the window is full. The window is
        measured in logical messages, because that is the unit a prompt is
        built from; an edit does not add a message to it, it revises one.

        Proof mode ignores the logical prompt window because a page of post-gap
        tail can fill that window while the missing interval remains on the
        next page. Only readable server exhaustion proves the interval covered;
        the raw-event and request ceilings still bound its cost.

        There are three ways this returns, and only two of them mean the walk
        finished its job. The third is the event ceiling, which is logged rather
        than raised: the caller gets a shorter conversation, not a failed read.
        """
        events: list[ProjectedEvent] = []
        logical = 0
        fetched = 0
        pages = 0
        unreadable = False
        start: str | None = None
        client = self._client()
        while True:
            response = await client.room_messages(
                room_id,
                start=start,
                direction=nio.MessageDirection.back,
                limit=_MESSAGES_PAGE_LIMIT,
            )
            if not isinstance(response, nio.RoomMessagesResponse):
                msg = f"Could not fetch history for {room_id!r}: {response}"
                raise _HydrationError(msg)
            pages += 1
            remaining = max(self.max_fetched_events - fetched, 0)
            page = response.chunk[:remaining]
            fetched += len(page)
            for event in page:
                # nio decrypts a `/messages` chunk on the way through
                # `receive_response`, so unlike the relation walk this is only
                # reached when decryption was tried and failed. The walk still
                # cannot say it read the room.
                readable = _readable_event(client, event)
                unreadable = unreadable or readable is None
                projected = (
                    None if readable is None else _projected_from_event(room_id, readable, self_sender=self.self_sender)
                )
                if projected is None:
                    continue
                events.append(projected)
                if _is_logical_message(projected):
                    logical += 1
            if len(page) < len(response.chunk):
                # A partial page cannot safely advance to its continuation
                # token: that would skip the page suffix we did not retain.
                # It is therefore a bounded, truncated walk.
                logger.warning(
                    "conversation_hydration_ceiling_reached",
                    room_id=room_id,
                    requests=pages,
                    fetched_events=fetched,
                    logical_messages=logical,
                    prompt_window_messages=self.prompt_window_messages,
                )
                return _Walk(
                    events=tuple(events),
                    complete=False,
                    exhausted_server=False,
                    unreadable=unreadable,
                )
            if not require_server_exhaustion and logical >= self.prompt_window_messages:
                return _Walk(
                    events=tuple(events),
                    complete=False,
                    exhausted_server=False,
                    unreadable=unreadable,
                )
            # An empty page is not exhaustion. The server may filter a page down
            # to nothing and still hand back a continuation token, and the room
            # can hold visible history behind it; only the absent token means
            # there is no more. A token that does not move is refusing to make
            # progress, which is the one shape that could spin forever, because
            # an empty page does not advance the event count either.
            if not response.end:
                return _Walk(
                    events=tuple(events),
                    # Running out of history is only completeness if the walk
                    # could read what it ran through.
                    complete=not unreadable,
                    exhausted_server=True,
                    unreadable=unreadable,
                )
            next_start = _advanced_room_cursor(room_id=room_id, start=start, end=response.end)
            if fetched >= self.max_fetched_events or pages >= self.max_requests:
                # Not the window being met, so it is said out loud. A room that
                # reaches this is one where reading further costs more than the
                # older messages are worth to a prompt.
                logger.warning(
                    "conversation_hydration_ceiling_reached",
                    room_id=room_id,
                    requests=pages,
                    fetched_events=fetched,
                    logical_messages=logical,
                    prompt_window_messages=self.prompt_window_messages,
                )
                return _Walk(
                    events=tuple(events),
                    complete=False,
                    exhausted_server=False,
                    unreadable=unreadable,
                )
            start = next_start

    async def refresh(self, request: RefreshRequest) -> bool:
        """Refetch one logical message whose visible revision was redacted.

        Returns whether the projection was updated. A ``False`` result leaves
        the message hidden and its refresh token durable, so the next strict
        read tries again rather than serving anything stale.
        """
        original = await self._client().room_get_event(request.room_id, request.logical_event_id)
        if not isinstance(original, nio.RoomGetEventResponse):
            logger.info(
                "conversation_refresh_unavailable",
                room_id=request.room_id,
                logical_event_id=request.logical_event_id,
            )
            return False
        readable_original = _readable_event(self._client(), original.event)
        if readable_original is None:
            # Unreadable is not deleted, and the branch below would treat it as
            # deleted: an event that projects to nothing is how "the server no
            # longer has this message" arrives here, so an undecryptable one
            # would drop a message that exists and that every other client in
            # the room can still read. Keeping the token instead leaves the
            # message hidden and retries on the next strict read, which is what
            # every other unreachable-server case here already does.
            logger.info(
                "conversation_refresh_unreadable",
                room_id=request.room_id,
                logical_event_id=request.logical_event_id,
            )
            return False
        projected = _projected_from_event(request.room_id, readable_original, self_sender=self.self_sender)
        if projected is None or projected.redacts_event_id is not None:
            # The whole logical message is gone, not just the revision that was
            # on screen, so there is no revision left to reduce to. Dropping the
            # row is the conditional form of that -- it holds the refresh token
            # and membership epoch this request was issued under, which
            # projecting the redaction would bypass.
            return await self.store.drop_refetched_message(request)
        relations = await self._fetch_relations(request.room_id, request.logical_event_id, window_messages=None)
        if relations.unreadable:
            # An empty relation list is a real answer -- it is how a server that
            # already reclaimed the superseded edits reports the original as
            # current -- so reducing over relations that were dropped unread
            # cannot be told apart from it, and reinstalls the pre-edit body as
            # though the server had said so. The walk's own ceiling is not this
            # case: relations arrive newest first, so a ceiling drops older
            # relations that could never have won.
            logger.info(
                "conversation_refresh_unreadable",
                room_id=request.room_id,
                logical_event_id=request.logical_event_id,
            )
            return False
        revision = _reduce_current_revision(projected, relations.events)
        content = await self._resolved_content(revision.event_id, revision.content)
        if content is None:
            return False
        return await self.store.install_refetched_revision(
            request,
            revision_event_id=revision.event_id,
            revision_ts=revision.origin_server_ts,
            content=content,
        )

    async def _resolved_content(
        self,
        event_id: str,
        content: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Return one revision's whole text, fetching its sidecar when it has one.

        A message too large for a single Matrix event carries a preview in its
        content and its real text in an attached file, and the projection
        refuses to store the preview. This is where that file is read.

        It happens here, and not at admission, because admission commits before
        nio accepts the sync response: making acceptance wait on a media
        download would let a slow or missing attachment decide whether an event
        counts as received. It happens per refetched message, and not per
        hydrated room, because the caller asking for the text is the one whose
        page size bounds how much of it is worth fetching.

        Returning nothing means the attachment could not be read. The message
        then stays unreadable and keeps its refresh token, so the next strict
        read tries again rather than installing the preview and calling the
        debt settled.
        """
        if not holds_unresolved_sidecar(content):
            return content
        resolved = await resolve_event_source_content(
            {"event_id": event_id, "content": dict(content)},
            self._client(),
        )
        resolved_content = resolved.get("content")
        if not isinstance(resolved_content, dict) or holds_unresolved_sidecar(resolved_content):
            logger.info(
                "conversation_refresh_sidecar_unresolved",
                event_id=event_id,
            )
            return None
        return resolved_content

    async def resolve_refreshes(self, requests: Sequence[RefreshRequest]) -> None:
        """Repair exactly the messages one read found missing.

        The caller passes the debts from its own page rather than naming a
        conversation, because those are not the same set. Re-selecting from the
        conversation returns the newest debts first and stops at a fixed
        number, so a page containing one older unresolved message behind
        enough newer unrepairable ones would retry the newer ones on every
        read and never once attempt the message it was actually asked for.

        The next strict read is what runs this. There is no background refresh
        worker, so an unreachable homeserver degrades reads instead of building
        up retry state nobody is watching.
        """
        for request in requests:
            await self.refresh(request)
