"""Thread read policy for Matrix conversation cache."""

from __future__ import annotations

import time
import typing
from typing import TYPE_CHECKING

from mindroom.matrix.cache.thread_cache import resolved_thread_cache_entry
from mindroom.matrix.cache.thread_cache_helpers import (
    event_id_from_event_source,
    latest_visible_thread_event_id,
    log_resolved_thread_cache,
    resolved_cache_diagnostics,
)
from mindroom.matrix.cache.thread_history_result import (
    ThreadHistoryResult,
    thread_history_result,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.cache.thread_cache import ResolvedThreadCache
    from mindroom.matrix.client import ResolvedVisibleMessage


class ThreadReadPolicy:
    """Own thread-history reads for one cache facade."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        resolved_thread_cache_getter: typing.Callable[[], ResolvedThreadCache],
        fetch_thread_history_from_client: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
        fetch_thread_snapshot_from_client: typing.Callable[[str, str], typing.Awaitable[ThreadHistoryResult]],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self._resolved_thread_cache_getter = resolved_thread_cache_getter
        self.fetch_thread_history_from_client = fetch_thread_history_from_client
        self.fetch_thread_snapshot_from_client = fetch_thread_snapshot_from_client

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    def _resolved_thread_cache(self) -> ResolvedThreadCache:
        return self._resolved_thread_cache_getter()

    async def _wait_for_pending_room_cache_updates(self, room_id: str) -> None:
        await self.runtime.event_cache_write_coordinator.wait_for_room_idle(room_id)

    async def _cached_thread_source_event_ids(
        self,
        room_id: str,
        thread_id: str,
    ) -> frozenset[str]:
        try:
            cached_event_sources = await self.runtime.event_cache.get_thread_events(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to read raw thread events for resolved cache",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return frozenset()
        if cached_event_sources is None:
            return frozenset()
        return frozenset(
            event_id
            for event_source in cached_event_sources
            if (event_id := event_id_from_event_source(event_source)) is not None
        )

    async def _store_resolved_thread_cache_entry(
        self,
        room_id: str,
        thread_id: str,
        *,
        history: Sequence[ResolvedVisibleMessage],
    ) -> None:
        source_event_ids = await self._cached_thread_source_event_ids(room_id, thread_id)
        if not source_event_ids:
            self._resolved_thread_cache().invalidate(room_id, thread_id)
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_skip_store",
                room_id=room_id,
                thread_id=thread_id,
                reason="missing_source_event_ids",
            )
            return
        self._resolved_thread_cache().store(
            room_id,
            thread_id,
            resolved_thread_cache_entry(
                history=history,
                source_event_ids=source_event_ids,
            ),
        )
        log_resolved_thread_cache(
            self.logger,
            "resolved_thread_cache_store",
            room_id=room_id,
            thread_id=thread_id,
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

    def _snapshot_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
    ) -> ThreadHistoryResult:
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=history.is_full_history,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(list(history), is_full_history=False)

    def _empty_history_result(self, *, is_full_history: bool) -> ThreadHistoryResult:
        return thread_history_result([], is_full_history=is_full_history)

    async def _maybe_use_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult | None:
        lookup_started = time.perf_counter()
        entry = self._resolved_thread_cache().lookup(room_id, thread_id)
        cache_read_ms = round((time.perf_counter() - lookup_started) * 1000, 1)
        if entry is None:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_miss",
                room_id=room_id,
                thread_id=thread_id,
            )
            return None
        if not entry.source_event_ids:
            self._resolved_thread_cache().invalidate(room_id, thread_id)
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="missing_source_event_ids",
            )
            return None
        log_resolved_thread_cache(
            self.logger,
            "resolved_thread_cache_hit",
            room_id=room_id,
            thread_id=thread_id,
        )
        return thread_history_result(
            entry.clone_history(),
            is_full_history=True,
            diagnostics=resolved_cache_diagnostics(cache_read_ms=cache_read_ms),
        )

    async def _fetch_full_thread_history_from_source(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        try:
            history = self._full_history_result(
                await self.fetch_thread_history_from_client(room_id, thread_id),
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to fetch Matrix thread history; returning empty history",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return self._empty_history_result(is_full_history=True)
        await self._store_resolved_thread_cache_entry(
            room_id,
            thread_id,
            history=history,
        )
        return history

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadHistoryResult:
        """Resolve lightweight snapshot history for one thread."""
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache().entry_lock(room_id, thread_id):
            cached_history = await self._maybe_use_resolved_thread_cache(room_id, thread_id)
            if cached_history is not None:
                return cached_history
            try:
                snapshot = self._snapshot_result(
                    await self.fetch_thread_snapshot_from_client(room_id, thread_id),
                )
            except Exception as exc:
                self.logger.warning(
                    "Failed to fetch Matrix thread snapshot; returning empty history",
                    room_id=room_id,
                    thread_id=thread_id,
                    error=str(exc),
                )
                return self._empty_history_result(is_full_history=False)
            if snapshot.is_full_history:
                await self._store_resolved_thread_cache_entry(
                    room_id,
                    thread_id,
                    history=snapshot,
                )
            return snapshot

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadHistoryResult:
        """Resolve full thread history for one conversation root."""
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache().entry_lock(room_id, thread_id):
            cached_history = await self._maybe_use_resolved_thread_cache(room_id, thread_id)
            if cached_history is not None:
                return cached_history
            return await self._fetch_full_thread_history_from_source(room_id, thread_id)

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
        thread_history = await self.get_thread_history(room_id, thread_id)
        return latest_visible_thread_event_id(thread_history) or thread_id
