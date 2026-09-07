"""Where one event sits in a thread, asked once per turn.

Thread resolution needs relation facts about events the conversation refers to:
what a reply points at, what an edit replaces, which thread an arbitrary event
belongs to. The old answer was a durable cache of raw event JSON, filled on
demand and kept forever, which is most of the machinery this cutover removes.

Almost all of those questions are about events this bot already admitted, and
the journal recorded their thread when it accepted them. The rest -- an event
referenced but never seen, typically older than anything local -- come from the
homeserver, once, and are remembered only for the turn that asked.

Nothing here is durable. A per-turn memo exists so one turn resolving the same
event from three places pays for one round trip; it is deliberately not a cache
that outlives the turn, because that is the thing being deleted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.event_journal import RelationView


class _SupportsClient(Protocol):
    """The one thing this needs from the bot runtime."""

    @property
    def client(self) -> nio.AsyncClient | None:
        """Return the live Matrix client, once there is one."""
        ...


logger = get_logger(__name__)

# Keyed by (room, event). Set for the duration of one turn and discarded with
# it, so a stale answer cannot outlive the turn that read it.
_TURN_EVENT_INFO: ContextVar[dict[tuple[str, str], EventInfo | None] | None] = ContextVar(
    "mindroom_turn_event_info",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RelationLookup:
    """Relation facts about single events, journal first, homeserver second."""

    store: RelationView
    # The runtime rather than a client, because the bot builds this before it
    # has logged in and the client arrives later.
    runtime: _SupportsClient

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[None]:
        """Memoize point lookups for the lifetime of one inbound turn.

        Re-entrant on purpose: an inner scope joins the outer one rather than
        starting a second memo, so a nested resolution still sees what the turn
        already fetched.
        """
        if _TURN_EVENT_INFO.get() is not None:
            yield
            return
        token = _TURN_EVENT_INFO.set({})
        try:
            yield
        finally:
            _TURN_EVENT_INFO.reset(token)

    async def admitted_thread_id(self, room_id: str, event_id: str) -> str | None:
        """Return the thread one event belongs to, asking only what is already here.

        The journal-only half of ``thread_id``, for a caller that has already
        asked the homeserver about this exact event and been told nothing. A
        second round trip resolves nothing it did not resolve the first time,
        so local state is the whole answer.

        Not knowing is reported as not in a thread. That is the conservative
        direction for the room scan that asks: an event it cannot place stays
        where it is rather than being reattached to a thread on a guess.
        """
        try:
            admitted, thread_id = await self.store.admitted_thread_id(room_id=room_id, event_id=event_id)
        except Exception:
            logger.warning(
                "Failed to read the admitted thread for an event; treating it as unproven",
                room_id=room_id,
                event_id=event_id,
                exc_info=True,
            )
            return None
        return thread_id if admitted else None

    async def strict_thread_id(self, room_id: str, event_id: str) -> str | None:
        """Return the thread one event belongs to, raising if that cannot be established.

        The fail-closed sibling of `thread_id`. A caller classifying an
        outbound mutation -- deciding whether a send or a redaction lands in a
        thread or at room level -- must not be told "no thread" by a lookup
        that simply failed. Degrading there silently reclassifies threaded work
        as room-level, which is how the cache-backed index behaved before it
        was replaced: it propagated its failures, and the tool turned them into
        an explicit error rather than a wrong send.

        Journal-only, and deliberately so. This stands in for the cache index
        the mutation path used to consult, which answered from local rows or
        not at all. The access object that calls this already fetches the
        event's own relation metadata separately, so falling back to the
        homeserver here would fetch the same event twice per classification.
        """
        admitted, thread_id = await self.store.admitted_thread_id(room_id=room_id, event_id=event_id)
        return thread_id if admitted else None

    async def thread_id(self, room_id: str, event_id: str) -> str | None:
        """Return the thread one event belongs to, or nothing.

        The journal answers for anything it admitted, including the events that
        are in no thread at all -- which is why it reports that separately from
        not knowing. Only a genuinely unseen event costs a round trip.

        A journal that cannot be read is an accelerator that failed, not an
        answer: the homeserver is authoritative here either way, so the lookup
        degrades to it rather than taking the turn down with it.
        """
        try:
            admitted, thread_id = await self.store.admitted_thread_id(room_id=room_id, event_id=event_id)
        except Exception:
            logger.warning(
                "Failed to read the admitted thread for an event; asking the homeserver",
                room_id=room_id,
                event_id=event_id,
                exc_info=True,
            )
            admitted, thread_id = False, None
        if admitted:
            return thread_id
        try:
            info = await self.event_info(room_id, event_id)
        except Exception:
            # Asking which thread an event is in is advisory: a caller that
            # cannot find out treats the event as unthreaded and degrades,
            # which is what the index this replaced did. `event_info` still
            # raises for callers resolving a specific event, where guessing
            # would attach the turn to the wrong conversation.
            logger.warning(
                "Failed to resolve the thread of a related event; treating it as unthreaded",
                room_id=room_id,
                event_id=event_id,
                exc_info=True,
            )
            return None
        return None if info is None else info.thread_id

    async def event_info(self, room_id: str, event_id: str) -> EventInfo | None:
        """Return one event's relation metadata, or nothing if the server lost it.

        Raises on a lookup that failed for any reason other than the event not
        existing. A caller resolving a reply target cannot tell an event that
        was deleted from one the homeserver merely refused to serve, and
        silently treating the second as the first would attach the turn to the
        wrong conversation.
        """
        memo = _TURN_EVENT_INFO.get()
        key = (room_id, event_id.strip())
        if memo is not None and key in memo:
            return memo[key]
        info = await self._fetch_event_info(room_id, event_id)
        if memo is not None:
            memo[key] = info
        return info

    async def _fetch_event_info(self, room_id: str, event_id: str) -> EventInfo | None:
        client = self.runtime.client
        if client is None:
            return None
        response = await client.room_get_event(room_id, event_id)
        if isinstance(response, nio.RoomGetEventResponse):
            return EventInfo.from_event(response.event.source)
        if isinstance(response, nio.RoomGetEventError) and response.status_code == "M_NOT_FOUND":
            return None
        detail = response.message if isinstance(response, nio.RoomGetEventError) else "unknown error"
        msg = f"Failed to resolve related Matrix event {event_id}: {detail}"
        raise RuntimeError(msg)
