"""Thread read policy for Matrix conversation cache."""

from __future__ import annotations

import asyncio
import time
import typing
from enum import Enum, auto
from typing import TYPE_CHECKING

from mindroom.matrix.cache.thread_cache_helpers import latest_visible_thread_event_id
from mindroom.matrix.cache.thread_history_result import (
    ThreadHistoryResult,
    thread_history_result,
)
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from mindroom.timing import elapsed_ms_since

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


_DISPATCH_THREAD_READ_COORDINATOR_TIMEOUT_SECONDS = 1.0
_DISPATCH_THREAD_READ_FETCH_TIMEOUT_SECONDS = 1.0
_CACHE_COORDINATOR_TIMEOUT = "cache_coordinator_timeout"
_DISPATCH_READ_TIMEOUT = "dispatch_read_timeout"


class ThreadReadMode(Enum):
    """Named thread-read policies for cache coordination and source freshness."""

    ADVISORY_FULL = auto()
    DISPATCH_SNAPSHOT = auto()
    DISPATCH_FULL = auto()
    STRICT_FULL = auto()

    @property
    def full_history(self) -> bool:
        """Return whether this mode requires fully hydrated thread history."""
        return self in {
            ThreadReadMode.ADVISORY_FULL,
            ThreadReadMode.DISPATCH_FULL,
            ThreadReadMode.STRICT_FULL,
        }

    @property
    def dispatch_safe(self) -> bool:
        """Return whether this mode is on the live dispatch fail-open path."""
        # STRICT_FULL intentionally stays false: it may block for authoritative post-lock model context.
        return self in {
            ThreadReadMode.DISPATCH_SNAPSHOT,
            ThreadReadMode.DISPATCH_FULL,
        }


class _ThreadHistoryFetcher(typing.Protocol):
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
        fetch_thread_history_from_client: _ThreadHistoryFetcher,
        fetch_dispatch_thread_history_from_client: _ThreadHistoryFetcher,
        fetch_dispatch_thread_snapshot_from_client: _ThreadHistoryFetcher,
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self.fetch_thread_history_from_client = fetch_thread_history_from_client
        self.fetch_dispatch_thread_history_from_client = fetch_dispatch_thread_history_from_client
        self.fetch_dispatch_thread_snapshot_from_client = fetch_dispatch_thread_snapshot_from_client

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    def _coordinator(self) -> EventCacheWriteCoordinator | None:
        return self.runtime.event_cache_write_coordinator

    def _fetcher_for_mode(self, mode: ThreadReadMode) -> _ThreadHistoryFetcher:
        """Return the client fetcher matching one named read policy."""
        match mode:
            case ThreadReadMode.ADVISORY_FULL:
                return self.fetch_thread_history_from_client
            case ThreadReadMode.DISPATCH_SNAPSHOT:
                return self.fetch_dispatch_thread_snapshot_from_client
            case ThreadReadMode.DISPATCH_FULL | ThreadReadMode.STRICT_FULL:
                return self.fetch_dispatch_thread_history_from_client
            case _:
                msg = f"Unsupported thread read mode: {mode!r}"
                raise ValueError(msg)

    def _operation_name_for_mode(self, mode: ThreadReadMode) -> str:
        """Return the cache coordinator operation name for one queued read mode."""
        match mode:
            case ThreadReadMode.ADVISORY_FULL:
                return "matrix_cache_refresh_thread_history"
            case ThreadReadMode.STRICT_FULL:
                return "matrix_cache_refresh_strict_thread_history"
            case ThreadReadMode.DISPATCH_SNAPSHOT | ThreadReadMode.DISPATCH_FULL:
                msg = f"Dispatch read mode {mode.name} does not use the refresh queue"
                raise ValueError(msg)
            case _:
                msg = f"Unsupported thread read mode: {mode!r}"
                raise ValueError(msg)

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

    def _degraded_dispatch_timeout_result(
        self,
        *,
        room_id: str,
        thread_id: str,
        caller_label: str,
        queue_wait_started: float,
        error_code: str,
        fetch_started: float | None = None,
    ) -> ThreadHistoryResult:
        coordinator_queue_wait_ms = elapsed_ms_since(queue_wait_started, clock=time.perf_counter)
        dispatch_fetch_wait_ms = (
            elapsed_ms_since(fetch_started, clock=time.perf_counter) if fetch_started is not None else None
        )
        diagnostics = {
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
            THREAD_HISTORY_ERROR_DIAGNOSTIC: error_code,
            "coordinator_queue_wait_ms": coordinator_queue_wait_ms,
            "caller_label": caller_label,
        }
        if dispatch_fetch_wait_ms is not None:
            diagnostics["dispatch_fetch_wait_ms"] = dispatch_fetch_wait_ms
        log_fields = {
            "room_id": room_id,
            "thread_id": thread_id,
            "caller_label": caller_label,
            "thread_read_degraded": True,
            "thread_read_error": error_code,
            "coordinator_queue_wait_ms": coordinator_queue_wait_ms,
        }
        if dispatch_fetch_wait_ms is not None:
            log_fields["dispatch_fetch_wait_ms"] = dispatch_fetch_wait_ms
        self.logger.warning(
            "matrix_cache_thread_read_degraded",
            **log_fields,
        )
        return thread_history_result(
            [],
            is_full_history=False,
            diagnostics=diagnostics,
        )

    async def _load_thread_read(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: _ThreadHistoryFetcher,
        full_history: bool,
        caller_label: str,
        queue_wait_started: float,
    ) -> ThreadHistoryResult:
        coordinator_queue_wait_ms = elapsed_ms_since(queue_wait_started, clock=time.perf_counter)
        thread_history = await fetcher(
            room_id,
            thread_id,
            caller_label=caller_label,
            coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        )
        if full_history:
            return self._full_history_result(thread_history)
        return thread_history

    async def _load_dispatch_thread_read(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: _ThreadHistoryFetcher,
        full_history: bool,
        caller_label: str,
        queue_wait_started: float,
    ) -> ThreadHistoryResult:
        fetch_started = time.perf_counter()
        try:
            # Dispatch read-through fetches are bounded live reads. Cancelling them on timeout
            # is intentional; cache mutation tasks are protected by the write coordinator.
            return await asyncio.wait_for(
                self._load_thread_read(
                    room_id,
                    thread_id,
                    fetcher=fetcher,
                    full_history=full_history,
                    caller_label=caller_label,
                    queue_wait_started=queue_wait_started,
                ),
                timeout=_DISPATCH_THREAD_READ_FETCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return self._degraded_dispatch_timeout_result(
                room_id=room_id,
                thread_id=thread_id,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
                error_code=_DISPATCH_READ_TIMEOUT,
                fetch_started=fetch_started,
            )

    async def _run_thread_read(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: _ThreadHistoryFetcher,
        name: str,
        full_history: bool,
        caller_label: str,
        queue_wait_started: float,
    ) -> ThreadHistoryResult:
        coordinator = self._coordinator()
        if coordinator is None:
            return await self._load_thread_read(
                room_id,
                thread_id,
                fetcher=fetcher,
                full_history=full_history,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
            )
        return typing.cast(
            "ThreadHistoryResult",
            await coordinator.run_thread_update(
                room_id,
                thread_id,
                lambda: self._load_thread_read(
                    room_id,
                    thread_id,
                    fetcher=fetcher,
                    full_history=full_history,
                    caller_label=caller_label,
                    queue_wait_started=queue_wait_started,
                ),
                name=name,
                ignore_cancelled_room_fences=True,
            ),
        )

    async def read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        mode: ThreadReadMode,
        caller_label: str,
    ) -> ThreadHistoryResult:
        """Resolve one thread read through the same-thread barrier and fetch selection path."""
        queue_wait_started = time.perf_counter()
        try:
            if mode.dispatch_safe:
                await asyncio.wait_for(
                    self._wait_for_pending_thread_cache_updates(room_id, thread_id),
                    timeout=_DISPATCH_THREAD_READ_COORDINATOR_TIMEOUT_SECONDS,
                )
            else:
                await self._wait_for_pending_thread_cache_updates(room_id, thread_id)
        except TimeoutError:
            return self._degraded_dispatch_timeout_result(
                room_id=room_id,
                thread_id=thread_id,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
                error_code=_CACHE_COORDINATOR_TIMEOUT,
            )
        fetcher = self._fetcher_for_mode(mode)
        if mode.dispatch_safe:
            return await self._load_dispatch_thread_read(
                room_id,
                thread_id,
                fetcher=fetcher,
                full_history=mode.full_history,
                caller_label=caller_label,
                queue_wait_started=queue_wait_started,
            )
        return await self._run_thread_read(
            room_id,
            thread_id,
            fetcher=fetcher,
            name=self._operation_name_for_mode(mode),
            full_history=mode.full_history,
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
                mode=ThreadReadMode.ADVISORY_FULL,
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
