"""Facade for Matrix conversation reads and advisory cache notifications.

``MatrixConversationCache`` is the facade for point lookups and advisory thread bookkeeping; it
composes the three write policies (``cache.thread_writes``) and the mutation resolver
(``thread_bookkeeping``) over one shared write coordinator.

Point lookups no longer read the cache. ``get_event`` answers from the visible-message projection,
which is written inside the admission transaction and already holds the revision currently on
screen, and falls through to the homeserver for anything the projection does not have. Nothing on
this path writes the cache back: the fill existed to be read by this same lookup.

Per-turn memoization covers event lookups only. Thread reads are not memoized: the saving was one
re-read per turn, and paying for it meant every caller reasoning about whether a degraded or stale
read might be replayed later in the same turn.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import nio
from nio.responses import RoomGetEventError

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    ConversationEventCache,
)
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import ThreadLiveWritePolicy, ThreadSyncWritePolicy
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.event_normalization import normalize_nio_event_for_cache
from mindroom.matrix.media import (
    is_encrypted_media_event_source,
    parse_matrix_media_event_source,
)
from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver
from mindroom.matrix.thread_history_result import ThreadHistoryResult

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.event_journal import PointLookupView, VisibleMessage
    from mindroom.matrix.sync_certification import SyncCacheWriteResult


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError
type _TurnEventCacheKey = tuple[str, str, int]

logger = get_logger(__name__)


__all__ = [
    "ConversationCacheProtocol",
    "ConversationEventCache",
    "EventLookupResult",
    "MatrixConversationCache",
    "ThreadReadResult",
]


class ConversationCacheProtocol(Protocol):
    """Conversation-data reads available to resolver and related callers."""

    def turn_scope(self) -> AbstractAsyncContextManager[None]:
        """Provide per-turn memoization for event lookups."""

    async def get_event(self, room_id: str, event_id: str) -> EventLookupResult:
        """Resolve one Matrix event by ID."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Resolve the cached thread root for one event when known."""

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""


def _projected_event_source(message: VisibleMessage) -> dict[str, Any]:
    """Return the Matrix event source one projected message stands for.

    The projection is a reduction, not a copy, so two facts have to be put back
    before nio will recognize it as an event.

    The timestamp is the revision's, not the original's. A point lookup asks
    what this message is now, and for an edited message that is the edit
    currently on screen -- which is what the row already holds, without a
    second lookup for "and what replaced it".

    The thread relation is restored from the row's own ``thread_id`` because an
    edited row no longer carries one. The projection stores ``m.new_content``,
    and a replacement's new content is specified not to repeat the relation of
    the message it replaces. Without this an edited threaded reply would read
    back as a room-level message, and the callers of this are exactly the ones
    resolving which thread an event belongs to.
    """
    content = {key: value for key, value in message.content.items() if isinstance(key, str)}
    if message.thread_id is not None and "m.relates_to" not in content:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": message.thread_id}
    return {
        "event_id": message.logical_event_id,
        "room_id": message.room_id,
        "sender": message.sender,
        "type": "m.room.message",
        "origin_server_ts": message.revision_ts,
        "content": content,
    }


def _room_get_event_response(event_source: dict[str, Any]) -> nio.RoomGetEventResponse | None:
    """Parse one event source into the response shape a point lookup returns."""
    if is_encrypted_media_event_source(event_source):
        parsed_media_event = parse_matrix_media_event_source(event_source)
        if parsed_media_event is None:
            return None
        media_response = nio.RoomGetEventResponse()
        # nio's response parser also assigns BadEvent to this Event-typed field.
        media_response.event = cast("nio.Event", parsed_media_event)
        return media_response
    response = nio.RoomGetEventResponse.from_dict(event_source)
    return response if isinstance(response, nio.RoomGetEventResponse) else None


async def _projected_room_get_event(
    store: PointLookupView,
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> tuple[EventLookupResult, dict[str, Any] | None]:
    """Return one event from the visible projection, or from the homeserver.

    A projection miss is not an answer. The message may predate this bot's
    membership, or be an edit rather than a logical message, or have a body the
    projection is still withholding -- and in every one of those the homeserver
    knows and the projection does not, so the round trip happens exactly as it
    did when this read was cache-first.

    The second element is the source of a homeserver answer, for the caller
    that wants to remember it, and is nothing for a projection hit because a
    projection hit came from the store already.
    """
    normalized_event_id = event_id.strip()
    if normalized_event_id:
        try:
            projected = await store.visible_message(room_id=room_id, logical_event_id=normalized_event_id)
        except Exception as exc:
            logger.warning(
                "Failed to read the projected Matrix event",
                room_id=room_id,
                event_id=normalized_event_id,
                error=str(exc),
            )
        else:
            if projected is not None:
                projected_response = _room_get_event_response(_projected_event_source(projected))
                if projected_response is not None:
                    return projected_response, None
                logger.warning(
                    "Projected Matrix event could not be reconstructed",
                    room_id=room_id,
                    event_id=normalized_event_id,
                )

    response = await client.room_get_event(room_id, normalized_event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return response, None

    normalized_event_source = normalize_nio_event_for_cache(
        response.event,
        event_id=normalized_event_id,
    )
    fetched_response = _room_get_event_response(normalized_event_source)
    return (fetched_response if fetched_response is not None else response), normalized_event_source


@dataclass
class MatrixConversationCache(ConversationCacheProtocol):
    """Own Matrix conversation reads and advisory cache writes for one bot."""

    logger: structlog.stdlib.BoundLogger
    runtime: BotRuntimeView
    # Point lookups answer from the visible projection, which is written inside
    # the admission transaction. Narrowed to that one read so this facade cannot
    # reach the rest of the store on the way past.
    store: PointLookupView
    _turn_event_cache: ContextVar[dict[_TurnEventCacheKey, EventLookupResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_event_lookup_cache", default=None),
    )
    _write_cache_ops: ThreadMutationCacheOps = field(init=False, repr=False)
    _live: ThreadLiveWritePolicy = field(init=False, repr=False)
    _sync: ThreadSyncWritePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind extracted read/write collaborators to this facade."""
        resolver = ThreadMutationResolver(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            fetch_event_info_for_thread_resolution=self._event_info_for_thread_resolution,
        )
        self._write_cache_ops = ThreadMutationCacheOps(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
        )
        self._live = ThreadLiveWritePolicy(
            resolver=resolver,
            cache_ops=self._write_cache_ops,
        )
        self._sync = ThreadSyncWritePolicy(
            resolver=resolver,
            cache_ops=self._write_cache_ops,
        )

    def _require_client(self) -> nio.AsyncClient:
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation cache"
            raise RuntimeError(msg)
        return client

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[None]:
        """Memoize event lookups for the lifetime of one inbound turn."""
        if self._turn_event_cache.get() is not None:
            yield
            return

        event_token = self._turn_event_cache.set({})
        try:
            yield
        finally:
            self._turn_event_cache.reset(event_token)

    async def get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> EventLookupResult:
        """Resolve one event through per-turn memoization and the visible projection."""
        normalized_event_id = event_id.strip()
        cache_key: _TurnEventCacheKey = (
            room_id,
            normalized_event_id,
            self._write_cache_ops.room_departure_epoch(room_id),
        )
        turn_cache = self._turn_event_cache.get()
        if turn_cache is not None and cache_key in turn_cache:
            return turn_cache[cache_key]

        response, _fetched_event_source = await _projected_room_get_event(
            self.store,
            self._require_client(),
            room_id,
            event_id,
        )
        if turn_cache is not None:
            turn_cache[cache_key] = response
        return response

    async def _event_info_for_thread_resolution(
        self,
        room_id: str,
        event_id: str,
    ) -> EventInfo | None:
        """Resolve one related event without memoizing pre-mutation state in the active turn."""
        response, _fetched_event_source = await _projected_room_get_event(
            self.store,
            self._require_client(),
            room_id,
            event_id,
        )
        if not isinstance(response, nio.RoomGetEventResponse):
            return None
        return EventInfo.from_event(response.event.source)

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Resolve the cached thread root for one event when known."""
        try:
            return await self.runtime.event_cache.get_thread_id_for_event(room_id, event_id)
        except Exception as error:
            logger.warning(
                "Conversation cache thread lookup failed; continuing without cached thread id",
                room_id=room_id,
                event_id=event_id,
                error=str(error),
            )
            return None

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        await self._live.append_live_event(room_id, event, event_info=event_info)

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        await self._live.apply_redaction(room_id, event)

    def limited_sync_timeline_room_ids(
        self,
        response: nio.SyncResponse,
    ) -> tuple[tuple[str, ...], tuple[BaseException, ...]]:
        """Return limited joined-room IDs or validation errors for one sync response."""
        return self._sync.limited_sync_timeline_room_ids(response)

    def cache_sync_timeline(
        self,
        response: nio.SyncResponse,
        *,
        raise_on_cache_write_failure: bool = False,
    ) -> list[asyncio.Task[object]]:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        return self._sync.cache_sync_timeline(
            response,
            raise_on_cache_write_failure=raise_on_cache_write_failure,
        )

    async def cache_sync_timeline_for_certification(
        self,
        response: nio.SyncResponse,
    ) -> SyncCacheWriteResult:
        """Durably persist sync timeline events and report cache-certification status."""
        return await self._sync.cache_sync_timeline_for_certification(response)
