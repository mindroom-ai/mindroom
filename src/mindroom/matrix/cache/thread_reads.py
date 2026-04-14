"""Thread read policy for Matrix conversation cache."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

from mindroom.matrix.cache.thread_cache_helpers import latest_visible_thread_event_id
from mindroom.matrix.cache.thread_history_result import (
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    ThreadHistoryResult,
    thread_history_result,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.client import ResolvedVisibleMessage


class ThreadReadPolicy:
    """Own thread-history reads for one cache facade."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        fetch_thread_history_from_client: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
        fetch_thread_snapshot_from_client: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
        fetch_dispatch_thread_history_from_client: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
        fetch_dispatch_thread_snapshot_from_client: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self.fetch_thread_history_from_client = fetch_thread_history_from_client
        self.fetch_thread_snapshot_from_client = fetch_thread_snapshot_from_client
        self.fetch_dispatch_thread_history_from_client = fetch_dispatch_thread_history_from_client
        self.fetch_dispatch_thread_snapshot_from_client = fetch_dispatch_thread_snapshot_from_client

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    async def _wait_for_pending_room_cache_updates(self, room_id: str) -> None:
        await self.runtime.event_cache_write_coordinator.wait_for_room_idle(room_id)

    def _full_history_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
    ) -> ThreadHistoryResult:
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=True,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(list(history), is_full_history=True)

    async def _load_full_thread_history(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
    ) -> ThreadHistoryResult:
        return self._full_history_result(
            await fetcher(room_id, thread_id),
        )

    async def _load_thread_history_under_room_barrier(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
        name: str,
    ) -> ThreadHistoryResult:
        return typing.cast(
            "ThreadHistoryResult",
            await self.runtime.event_cache_write_coordinator.run_room_update(
                room_id,
                lambda: self._load_full_thread_history(
                    room_id,
                    thread_id,
                    fetcher=fetcher,
                ),
                name=name,
            ),
        )

    async def get_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Resolve advisory lightweight thread context for one thread under the room-scoped barrier."""
        await self._wait_for_pending_room_cache_updates(room_id)
        return typing.cast(
            "ThreadHistoryResult",
            await self.runtime.event_cache_write_coordinator.run_room_update(
                room_id,
                lambda: self.fetch_thread_snapshot_from_client(room_id, thread_id),
                name="matrix_cache_refresh_thread_snapshot",
            ),
        )

    async def get_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Resolve advisory full thread history for one conversation root."""
        await self._wait_for_pending_room_cache_updates(room_id)
        return await self._load_thread_history_under_room_barrier(
            room_id,
            thread_id,
            fetcher=self.fetch_thread_history_from_client,
            name="matrix_cache_refresh_thread_history",
        )

    async def get_dispatch_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Resolve strict lightweight thread context for dispatch under the room-scoped barrier."""
        await self._wait_for_pending_room_cache_updates(room_id)
        return typing.cast(
            "ThreadHistoryResult",
            await self.runtime.event_cache_write_coordinator.run_room_update(
                room_id,
                lambda: self.fetch_dispatch_thread_snapshot_from_client(room_id, thread_id),
                name="matrix_cache_refresh_dispatch_thread_snapshot",
            ),
        )

    async def get_dispatch_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Resolve strict full thread history for dispatch."""
        await self._wait_for_pending_room_cache_updates(room_id)
        return await self._load_thread_history_under_room_barrier(
            room_id,
            thread_id,
            fetcher=self.fetch_dispatch_thread_history_from_client,
            name="matrix_cache_refresh_dispatch_thread_history",
        )

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it."""
        if thread_id is None or existing_event_id is not None or reply_to_event_id is not None:
            return None
        try:
            thread_history = await self.get_thread_history(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to refresh latest thread event ID; falling back to thread root",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return thread_id
        if thread_history.diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC) == THREAD_HISTORY_SOURCE_STALE_CACHE:
            self.logger.warning(
                "Ignoring stale cached thread tail for latest-event lookup; falling back to thread root",
                room_id=room_id,
                thread_id=thread_id,
            )
            return thread_id
        return latest_visible_thread_event_id(thread_history) or thread_id
