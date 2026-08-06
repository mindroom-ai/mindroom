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
_REQUIRED_RECURSION_DEPTH = 0

_MESSAGES_PAGE_LIMIT = 100
# How much of a room hydration is for: enough recent logical messages to fill
# the largest prompt the runtime will build, and no more. The projection is a
# prompt view, not a Matrix replica, so "hydrated" means "the window a prompt
# can read is present" rather than "this room is fully mirrored". A caller that
# needs older history than this paginates Matrix directly.
_HYDRATED_PROMPT_WINDOW_MESSAGES = 2_000
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


class _HydrationError(RuntimeError):
    """A conversation could not be built from the server."""


def _is_redacted(source: Mapping[str, object]) -> bool:
    unsigned = source.get("unsigned")
    return isinstance(unsigned, dict) and "redacted_because" in unsigned


def _projected_from_event(room_id: str, event: nio.Event) -> ProjectedEvent | None:
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

    store: PrincipalStore
    client: nio.AsyncClient
    required_recursion_depth: int = _REQUIRED_RECURSION_DEPTH
    prompt_window_messages: int = _HYDRATED_PROMPT_WINDOW_MESSAGES
    max_fetched_events: int = _MAX_FETCHED_EVENTS
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
            await self._fetch_thread(room_id, thread_id) if thread_id is not None else await self._fetch_room(room_id)
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
            raise _HydrationError(msg)
        events: list[ProjectedEvent] = []
        root_projected = _projected_from_event(room_id, root.event)
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
                projected = _projected_from_event(room_id, event)
                if projected is not None:
                    events.append(projected)
        except nio.InsufficientRecursionDepthError as error:
            msg = (
                f"Homeserver returned related events without reporting a recursion depth "
                f"({error.reported!r}), so it did not honor the recursive request and the "
                f"conversation would be missing indirectly related events"
            )
            raise _HydrationError(msg) from error
        return tuple(events)

    async def _fetch_room(self, room_id: str) -> tuple[ProjectedEvent, ...]:
        """Walk back until the prompt window is filled, or the room runs out.

        A server that has run out of history answers with an empty chunk and no
        ``end`` token. That is successful exhaustion, not a failure, and
        treating it as one is what used to leave rooms permanently unready.

        Stopping at the window is the whole point rather than a shortfall: what
        hydration promises is the range a prompt can read, so a room with more
        history than that is hydrated once the window is full. The window is
        measured in logical messages, because that is the unit a prompt is
        built from; an edit does not add a message to it, it revises one.

        There are three ways this returns, and only two of them mean the window
        was filled. The third is the event ceiling, which is logged rather than
        raised: the caller gets a shorter conversation, not a failed read.
        """
        events: list[ProjectedEvent] = []
        logical = 0
        fetched = 0
        start: str | None = None
        while True:
            response = await self.client.room_messages(
                room_id,
                start=start,
                direction=nio.MessageDirection.back,
                limit=_MESSAGES_PAGE_LIMIT,
            )
            if not isinstance(response, nio.RoomMessagesResponse):
                msg = f"Could not fetch history for {room_id!r}: {response}"
                raise _HydrationError(msg)
            fetched += len(response.chunk)
            for event in response.chunk:
                projected = _projected_from_event(room_id, event)
                if projected is None:
                    continue
                events.append(projected)
                if projected.replaces_event_id is None:
                    logical += 1
            if logical >= self.prompt_window_messages or not response.chunk or not response.end:
                return tuple(events)
            if fetched >= self.max_fetched_events:
                # Not the window being met, so it is said out loud. A room that
                # reaches this is one where reading further costs more than the
                # older messages are worth to a prompt.
                logger.warning(
                    "conversation_hydration_event_ceiling_reached",
                    room_id=room_id,
                    fetched_events=fetched,
                    logical_messages=logical,
                    prompt_window_messages=self.prompt_window_messages,
                )
                return tuple(events)
            start = response.end

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
        projected = _projected_from_event(request.room_id, original.event)
        if projected is None:
            return await self.store.drop_refetched_message(request)
        relations = await self._fetch_relations(request.room_id, request.logical_event_id)
        revision = _reduce_current_revision(projected, relations)
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
