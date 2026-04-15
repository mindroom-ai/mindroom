"""Thread mutation and advisory bookkeeping policy for Matrix conversation cache."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any

from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_write_live import ThreadLiveWritePolicy
from mindroom.matrix.cache.thread_write_outbound import ThreadOutboundWritePolicy
from mindroom.matrix.cache.thread_write_resolution import ThreadMutationResolver
from mindroom.matrix.cache.thread_write_sync import (
    ThreadSyncWritePolicy,
    _collect_sync_timeline_cache_updates,
)

if TYPE_CHECKING:
    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.event_info import EventInfo


__all__ = ["ThreadWritePolicy", "_collect_sync_timeline_cache_updates"]


class ThreadWritePolicy:
    """Compose outbound, live, and sync thread-cache mutations behind one facade."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        require_client: typing.Callable[[], nio.AsyncClient],
        fetch_event_info_for_thread_resolution: typing.Callable[[str, str], typing.Awaitable[EventInfo | None]],
    ) -> None:
        self._resolver = ThreadMutationResolver(
            logger_getter=logger_getter,
            runtime=runtime,
            fetch_event_info_for_thread_resolution=fetch_event_info_for_thread_resolution,
        )
        self._cache_ops = ThreadMutationCacheOps(
            logger_getter=logger_getter,
            runtime=runtime,
        )
        self._outbound = ThreadOutboundWritePolicy(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
            require_client=require_client,
        )
        self._live = ThreadLiveWritePolicy(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
        )
        self._sync = ThreadSyncWritePolicy(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
        )

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._cache_ops.logger

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule advisory bookkeeping for one locally sent threaded message or edit."""
        self._outbound.notify_outbound_message(room_id, event_id, content)

    def notify_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Schedule advisory bookkeeping for one locally redacted threaded message."""
        self._outbound.notify_outbound_redaction(room_id, redacted_event_id)

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

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        self._sync.cache_sync_timeline(response)
