"""Building a conversation from the server, once per membership.

Hydration is the only thing in MindRoom that reads history from Matrix, and it
runs at most once per conversation per membership. There is no periodic repair
scan and no room-wide fallback: if hydration fails, the read fails, which is
visible. A background repairer that quietly half-works is not.

The same code path serves two callers — first read of a conversation, and the
point refetch owed after the visible revision of a message was redacted — so
there is exactly one implementation of "ask the server what this looks like
now".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio

from mindroom.event_journal import (
    ProjectedEvent,
    RefreshRequest,
    replacement_target,
    thread_root,
    visible_content,
)
from mindroom.event_journal.projection import is_newer_revision
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.event_journal import PrincipalStore

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
REQUIRED_RECURSION_DEPTH = 0

_MESSAGES_PAGE_LIMIT = 100
_MAX_MESSAGES_PAGES = 20


class HydrationError(RuntimeError):
    """A conversation could not be built from the server."""


def _is_redacted(source: Mapping[str, object]) -> bool:
    unsigned = source.get("unsigned")
    return isinstance(unsigned, dict) and "redacted_because" in unsigned


def projected_from_event(room_id: str, event: nio.Event) -> ProjectedEvent | None:
    """Return the projection view of one fetched event, or nothing.

    A redacted event comes back from the server with its content stripped.
    Storing that would put an empty message in the conversation, so it is
    dropped instead: the server has already told us there is nothing to show.
    """
    if _is_redacted(event.source):
        return None
    content = event.source.get("content")
    if not isinstance(content, dict) or not content:
        return None
    if event.source.get("type") != "m.room.message":
        return None
    return ProjectedEvent(
        event_id=event.event_id,
        room_id=room_id,
        thread_id=thread_root(content),
        sender=event.sender,
        origin_server_ts=event.server_timestamp,
        content=content,
        replaces_event_id=replacement_target(content),
        redacts_event_id=None,
    )


@dataclass(frozen=True, slots=True)
class Revision:
    """The revision of a logical message that is currently on the server."""

    event_id: str
    origin_server_ts: int
    content: Mapping[str, object]


def reduce_current_revision(
    original: ProjectedEvent,
    relations: Sequence[ProjectedEvent],
) -> Revision:
    """Return the revision the server would show for one logical message.

    Uses the same ordering rule as the live projection, so a refetched message
    and a message built from live events cannot disagree about which edit won.
    """
    winner = Revision(
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
        winner = Revision(
            event_id=relation.event_id,
            origin_server_ts=relation.origin_server_ts,
            content=visible_content(relation.content),
        )
    return winner


@dataclass
class ConversationHydrator:
    """One-time conversation hydration and point refetch against Matrix."""

    store: PrincipalStore
    client: nio.AsyncClient
    required_recursion_depth: int = REQUIRED_RECURSION_DEPTH
    _in_flight: dict[tuple[str, str | None], asyncio.Task[None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    async def ensure_hydrated(self, *, room_id: str, thread_id: str | None) -> None:
        """Hydrate a conversation once, sharing one task among concurrent readers."""
        if await self.store.conversation_is_hydrated(room_id=room_id, thread_id=thread_id):
            return
        key = (room_id, thread_id)
        task = self._in_flight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._hydrate(room_id=room_id, thread_id=thread_id),
                name=f"hydrate_conversation_{room_id}",
            )
            self._in_flight[key] = task
        try:
            await asyncio.shield(task)
        finally:
            if self._in_flight.get(key) is task and task.done():
                del self._in_flight[key]

    async def _hydrate(self, *, room_id: str, thread_id: str | None) -> None:
        epoch = await self.store.membership_epoch(room_id)
        events = (
            await self._fetch_thread(room_id, thread_id)
            if thread_id is not None
            else await self._fetch_room(room_id)
        )
        installed = await self.store.install_hydrated_conversation(
            room_id=room_id,
            thread_id=thread_id,
            events=events,
            expected_membership_epoch=epoch,
        )
        if not installed:
            # Membership moved while the fetch was in flight, so this view is of
            # a room the bot is no longer in the same relationship with.
            logger.info("conversation_hydration_superseded", room_id=room_id, thread_id=thread_id)

    async def _fetch_thread(self, room_id: str, thread_id: str) -> tuple[ProjectedEvent, ...]:
        root = await self.client.room_get_event(room_id, thread_id)
        if not isinstance(root, nio.RoomGetEventResponse):
            msg = f"Could not fetch thread root {thread_id!r}: {root}"
            raise HydrationError(msg)
        events: list[ProjectedEvent] = []
        root_projected = projected_from_event(room_id, root.event)
        if root_projected is not None:
            events.append(root_projected)
        events.extend(await self._fetch_relations(room_id, thread_id))
        return tuple(events)

    async def _fetch_relations(self, room_id: str, event_id: str) -> tuple[ProjectedEvent, ...]:
        """Walk the whole relation tree, without filtering by relation type.

        Filtering by ``m.thread`` would miss the edits and replies hanging off
        thread members, which is exactly the content a conversation is made of.
        """
        events: list[ProjectedEvent] = []
        try:
            async for event in self.client.room_get_event_relations(
                room_id=room_id,
                event_id=event_id,
                recurse=True,
                minimum_recursion_depth=self.required_recursion_depth,
            ):
                projected = projected_from_event(room_id, event)
                if projected is not None:
                    events.append(projected)
        except nio.InsufficientRecursionDepthError as error:
            msg = (
                f"Homeserver returned related events without reporting a recursion depth "
                f"({error.reported!r}), so it did not honor the recursive request and the "
                f"conversation would be missing indirectly related events"
            )
            raise HydrationError(msg) from error
        return tuple(events)

    async def _fetch_room(self, room_id: str) -> tuple[ProjectedEvent, ...]:
        """Walk a bounded amount of room history, once.

        A server that has run out of history answers with an empty chunk and no
        ``end`` token. That is successful exhaustion, not a failure, and
        treating it as one is what used to leave rooms permanently unready.
        """
        events: list[ProjectedEvent] = []
        start: str | None = None
        for _ in range(_MAX_MESSAGES_PAGES):
            response = await self.client.room_messages(
                room_id,
                start=start,
                direction=nio.MessageDirection.back,
                limit=_MESSAGES_PAGE_LIMIT,
            )
            if not isinstance(response, nio.RoomMessagesResponse):
                msg = f"Could not fetch history for {room_id!r}: {response}"
                raise HydrationError(msg)
            for event in response.chunk:
                projected = projected_from_event(room_id, event)
                if projected is not None:
                    events.append(projected)
            if not response.chunk or not response.end:
                break
            start = response.end
        return tuple(events)

    async def refresh(self, request: RefreshRequest) -> bool:
        """Refetch one logical message whose visible revision was redacted.

        Returns whether the projection was updated. A ``False`` result leaves
        the message hidden and its refresh token durable, so the next strict
        read tries again rather than serving anything stale.
        """
        original = await self.client.room_get_event(request.room_id, request.logical_event_id)
        if not isinstance(original, nio.RoomGetEventResponse):
            logger.info(
                "conversation_refresh_unavailable",
                room_id=request.room_id,
                logical_event_id=request.logical_event_id,
            )
            return False
        projected = projected_from_event(request.room_id, original.event)
        if projected is None:
            return await self.store.drop_refetched_message(request)
        relations = await self._fetch_relations(request.room_id, request.logical_event_id)
        revision = reduce_current_revision(projected, relations)
        return await self.store.install_refetched_revision(
            request,
            revision_event_id=revision.event_id,
            revision_ts=revision.origin_server_ts,
            content=revision.content,
        )

    async def resolve_refreshes(self, *, room_id: str, thread_id: str | None) -> None:
        """Drive every owed refetch for one conversation.

        The next strict read is what runs this. There is no background refresh
        worker, so an unreachable homeserver degrades reads instead of building
        up retry state nobody is watching.
        """
        for request in await self.store.pending_refreshes(room_id=room_id, thread_id=thread_id):
            await self.refresh(request)
