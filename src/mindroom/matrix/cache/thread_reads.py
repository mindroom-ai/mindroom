"""Thread read policy for Matrix conversation cache."""

from __future__ import annotations

import time
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
    from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


class ThreadHistoryFetcher(typing.Protocol):
    """Client-backed thread-history fetcher with refresh diagnostics metadata."""

    def __call__(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str,
        coordinator_queue_wait_ms: float,
    ) -> typing.Awaitable[ThreadHistoryResult]:
        """Fetch one thread from cache/source and attach refresh diagnostics."""


class ThreadReadPolicy:
    """Own thread-history reads for one cache facade."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        fetch_thread_history_from_client: ThreadHistoryFetcher,
        fetch_thread_snapshot_from_client: ThreadHistoryFetcher,
        fetch_dispatch_thread_history_from_client: ThreadHistoryFetcher,
        fetch_dispatch_thread_snapshot_from_client: ThreadHistoryFetcher,
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

    def _coordinator(self) -> EventCacheWriteCoordinator | None:
        return self.runtime.event_cache_write_coordinator

    async def _wait_for_pending_thread_cache_updates(self, room_id: str, thread_id: str) -> None:
        coordinator = self._coordinator()
        if coordinator is None:
            return
        await coordinator.wait_for_thread_idle(
            room_id,
            thread_id,
            ignore_cancelled_room_fences=True,
        )

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

    async def _run_thread_read(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: ThreadHistoryFetcher,
        name: str,
        full_history: bool,
        caller_label: str,
        queue_wait_started: float,
    ) -> ThreadHistoryResult:
        async def load() -> ThreadHistoryResult:
            coordinator_queue_wait_ms = round((time.perf_counter() - queue_wait_started) * 1000, 1)
            thread_history = await fetcher(
                room_id,
                thread_id,
                caller_label=caller_label,
                coordinator_queue_wait_ms=coordinator_queue_wait_ms,
            )
            if full_history:
                return self._full_history_result(thread_history)
            return thread_history

        coordinator = self._coordinator()
        if coordinator is None:
            return await load()
        return typing.cast(
            "ThreadHistoryResult",
            await coordinator.run_thread_update(
                room_id,
                thread_id,
                load,
                name=name,
                ignore_cancelled_room_fences=True,
            ),
        )

    async def read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str,
    ) -> ThreadHistoryResult:
        """Resolve one thread read through the same-thread barrier and fetch selection path."""
        queue_wait_started = time.perf_counter()
        await self._wait_for_pending_thread_cache_updates(room_id, thread_id)
        if full_history and dispatch_safe:
            return await self._run_thread_read(
                room_id,
                thread_id,
                fetcher=self.fetch_dispatch_thread_history_from_client,
                name="matrix_cache_refresh_dispatch_thread_history",
                full_history=True,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
            )
        if full_history:
            return await self._run_thread_read(
                room_id,
                thread_id,
                fetcher=self.fetch_thread_history_from_client,
                name="matrix_cache_refresh_thread_history",
                full_history=True,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
            )
        if dispatch_safe:
            return await self._run_thread_read(
                room_id,
                thread_id,
                fetcher=self.fetch_dispatch_thread_snapshot_from_client,
                name="matrix_cache_refresh_dispatch_thread_snapshot",
                full_history=False,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
            )
        return await self._run_thread_read(
            room_id,
            thread_id,
            fetcher=self.fetch_thread_snapshot_from_client,
            name="matrix_cache_refresh_thread_snapshot",
            full_history=False,
            caller_label=caller_label,
            queue_wait_started=queue_wait_started,
        )

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
        *,
        caller_label: str = "latest_thread_event_lookup",
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it."""
        if thread_id is None or existing_event_id is not None or reply_to_event_id is not None:
            return None
        try:
            thread_history = await self.read_thread(
                room_id,
                thread_id,
                full_history=True,
                dispatch_safe=False,
                caller_label=caller_label,
            )
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
