"""Thread read and repair policy for Matrix conversation cache."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_cache import resolved_thread_cache_entry
from mindroom.matrix.thread_cache_helpers import (
    event_id_from_event_source,
    latest_visible_thread_event_id,
    log_resolved_thread_cache,
    resolved_cache_diagnostics,
    sort_thread_history_root_first,
)
from mindroom.matrix.thread_history_result import (
    THREAD_HISTORY_SOURCE_HOMESERVER,
    ThreadHistoryResult,
    thread_history_cache_refilled,
    thread_history_is_authoritative_refill,
    thread_history_read_source,
    thread_history_result,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.matrix.thread_cache import ResolvedThreadCache, ResolvedThreadCacheEntry


_SYNC_FRESHNESS_WINDOW_SECONDS = 30.0
_INCREMENTAL_RESOLVED_CACHE_EVENT_LIMIT = 1


class ThreadRepairRequiredError(RuntimeError):
    """Raised when a repair-required thread cannot be authoritatively refilled."""


class ThreadReadPolicy:
    """Own thread-history read, reuse, and repair policy for one cache facade."""

    def __init__(self, cache: MatrixConversationCache) -> None:
        self._get_logger = lambda: cache.logger
        self.runtime = cache.runtime
        self._get_resolved_thread_cache = lambda: cache._resolved_thread_cache
        self.thread_version = cache.thread_version
        self.thread_requires_refresh = cache._thread_requires_refresh
        self.clear_thread_refresh_required = cache._clear_thread_refresh_required
        self.adopt_room_lookup_repairs_locked = cache._writes._adopt_room_lookup_repairs_locked
        self.fetch_thread_history_from_client = cache._fetch_thread_history_from_client
        self.fetch_thread_snapshot_from_client = cache._fetch_thread_snapshot_from_client
        self.resolve_thread_history_delta_from_client = cache._resolve_thread_history_delta_from_client

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._get_logger()

    def _resolved_thread_cache(self) -> ResolvedThreadCache:
        return self._get_resolved_thread_cache()

    def _seconds_since_last_sync_activity(self) -> float | None:
        last_sync_activity_monotonic = self.runtime.last_sync_activity_monotonic
        if last_sync_activity_monotonic is None:
            return None
        return max(time.monotonic() - last_sync_activity_monotonic, 0.0)

    async def _should_refresh_cached_thread_history(self, room_id: str, thread_id: str) -> bool:
        if await self.thread_requires_refresh(room_id, thread_id):
            self.logger.debug(
                "Forcing Matrix thread refresh because local cache repair is required",
                room_id=room_id,
                thread_id=thread_id,
            )
            return True
        sync_age_seconds = self._seconds_since_last_sync_activity()
        if sync_age_seconds is None or sync_age_seconds >= _SYNC_FRESHNESS_WINDOW_SECONDS:
            return True
        self.logger.debug(
            "Skipping incremental Matrix thread refresh because sync is fresh",
            room_id=room_id,
            thread_id=thread_id,
            sync_age_ms=round(sync_age_seconds * 1000, 1),
        )
        return False

    async def _wait_for_pending_room_cache_updates(self, room_id: str) -> None:
        await self.runtime.event_cache_write_coordinator.wait_for_room_idle(room_id)

    async def _cached_thread_event_sources(
        self,
        room_id: str,
        thread_id: str,
    ) -> Sequence[dict[str, object]] | None:
        return await self.runtime.event_cache.get_thread_events(room_id, thread_id)

    async def _cached_thread_source_event_ids(
        self,
        room_id: str,
        thread_id: str,
    ) -> frozenset[str]:
        try:
            cached_event_sources = await self._cached_thread_event_sources(room_id, thread_id)
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
        thread_version: int,
    ) -> frozenset[str]:
        resolved_thread_cache = self._resolved_thread_cache()
        source_event_ids = await self._cached_thread_source_event_ids(room_id, thread_id)
        if history and not source_event_ids:
            resolved_thread_cache.invalidate(room_id, thread_id)
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_skip_store",
                room_id=room_id,
                thread_id=thread_id,
                reason="missing_source_event_ids",
                thread_version=thread_version,
            )
            return source_event_ids
        resolved_thread_cache.store(
            room_id,
            thread_id,
            resolved_thread_cache_entry(
                history=history,
                source_event_ids=source_event_ids,
                thread_version=thread_version,
            ),
        )
        log_resolved_thread_cache(
            self.logger,
            "resolved_thread_cache_store",
            room_id=room_id,
            thread_id=thread_id,
            thread_version=thread_version,
        )
        return source_event_ids

    @staticmethod
    def _should_store_resolved_thread_cache_entry(history: ThreadHistoryResult) -> bool:
        if thread_history_read_source(history) != THREAD_HISTORY_SOURCE_HOMESERVER:
            return True
        return thread_history_is_authoritative_refill(history)

    @staticmethod
    def _repair_history_is_authoritative(history: ThreadHistoryResult) -> bool:
        return thread_history_read_source(
            history,
        ) == THREAD_HISTORY_SOURCE_HOMESERVER and thread_history_is_authoritative_refill(history)

    def _repair_history_durably_refilled(self, history: ThreadHistoryResult) -> bool:
        return self._repair_history_is_authoritative(history) and thread_history_cache_refilled(history)

    @staticmethod
    def _raise_thread_repair_required(thread_id: str) -> None:
        raise ThreadRepairRequiredError(thread_id)

    async def _invalidate_raw_thread_before_repair(self, room_id: str, thread_id: str) -> None:
        try:
            await self.runtime.event_cache.invalidate_thread(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to invalidate stale raw thread cache before repair",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )

    async def _incrementally_refresh_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
        *,
        entry: ResolvedThreadCacheEntry,
        entry_version: int,
        current_thread_version: int,
    ) -> ThreadHistoryResult | None:
        resolved_thread_cache = self._resolved_thread_cache()
        if await self.thread_requires_refresh(room_id, thread_id):
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="repair_required",
                thread_version=current_thread_version,
            )
            return None
        cache_read_started = time.perf_counter()
        invalidation_reason: str | None = None
        try:
            cached_event_sources = await self._cached_thread_event_sources(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Resolved thread cache refresh could not read raw thread events",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return None
        cache_read_ms = round((time.perf_counter() - cache_read_started) * 1000, 1)
        current_event_ids: frozenset[str] = frozenset()
        new_event_sources: list[dict[str, object]] = []
        if cached_event_sources is None:
            invalidation_reason = "missing_raw_cache"
        else:
            current_event_ids = frozenset(
                event_id
                for event_source in cached_event_sources
                if (event_id := event_id_from_event_source(event_source)) is not None
            )
            if not entry.source_event_ids.issubset(current_event_ids):
                invalidation_reason = "redaction_or_missing_source"
            else:
                new_event_sources = [
                    event_source
                    for event_source in cached_event_sources
                    if (event_id := event_id_from_event_source(event_source)) is not None
                    and event_id not in entry.source_event_ids
                ]
                if not new_event_sources:
                    invalidation_reason = "version_changed_without_raw_delta"
                elif len(new_event_sources) > _INCREMENTAL_RESOLVED_CACHE_EVENT_LIMIT:
                    invalidation_reason = "multi_event_delta"
                elif EventInfo.from_event(new_event_sources[0]).is_edit:
                    invalidation_reason = "edit_delta"

        if invalidation_reason is not None:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason=invalidation_reason,
                thread_version=current_thread_version,
            )
            return None

        delta_history = await self.resolve_thread_history_delta_from_client(
            thread_id=thread_id,
            event_sources=new_event_sources,
        )
        merged_history_by_event_id = {message.event_id: message for message in entry.clone_history()}
        for message in delta_history:
            merged_history_by_event_id[message.event_id] = message
        merged_history = list(merged_history_by_event_id.values())
        sort_thread_history_root_first(merged_history, thread_id=thread_id)
        resolved_thread_cache.store(
            room_id,
            thread_id,
            resolved_thread_cache_entry(
                history=merged_history,
                source_event_ids=current_event_ids,
                thread_version=current_thread_version,
            ),
        )
        log_resolved_thread_cache(
            self.logger,
            "resolved_thread_cache_incremental_refresh",
            room_id=room_id,
            thread_id=thread_id,
            reason=f"{entry_version}->{current_thread_version}",
            thread_version=current_thread_version,
        )
        return thread_history_result(
            merged_history,
            is_full_history=True,
            thread_version=current_thread_version,
            diagnostics=resolved_cache_diagnostics(
                cache_read_ms=cache_read_ms,
                incremental_refresh_ms=delta_history.diagnostics.get("resolution_ms", 0.0),
                resolution_ms=delta_history.diagnostics.get("resolution_ms", 0.0),
                sidecar_hydration_ms=delta_history.diagnostics.get("sidecar_hydration_ms", 0.0),
            ),
        )

    async def _maybe_use_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
        *,
        current_thread_version: int,
        repair_required: bool,
    ) -> ThreadHistoryResult | None:
        resolved_thread_cache = self._resolved_thread_cache()
        lookup_started = time.perf_counter()
        cache_lookup = resolved_thread_cache.lookup(room_id, thread_id)
        cache_read_ms = round((time.perf_counter() - lookup_started) * 1000, 1)
        entry = cache_lookup.entry

        if cache_lookup.expired:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="ttl_expired",
                thread_version=current_thread_version,
            )
            entry = None

        if entry is None:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_miss",
                room_id=room_id,
                thread_id=thread_id,
                thread_version=current_thread_version,
            )
            if repair_required:
                log_resolved_thread_cache(
                    self.logger,
                    "resolved_thread_cache_invalidate",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason="repair_required",
                    thread_version=current_thread_version,
                )
        elif not entry.source_event_ids:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="missing_source_event_ids",
                thread_version=current_thread_version,
            )
            resolved_thread_cache.invalidate(room_id, thread_id)
        elif entry.thread_version == current_thread_version and not repair_required:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_hit",
                room_id=room_id,
                thread_id=thread_id,
                thread_version=current_thread_version,
            )
            return thread_history_result(
                entry.clone_history(),
                is_full_history=True,
                thread_version=current_thread_version,
                diagnostics=resolved_cache_diagnostics(cache_read_ms=cache_read_ms),
            )
        elif entry.thread_version != current_thread_version:
            incrementally_refreshed = await self._incrementally_refresh_resolved_thread_cache(
                room_id,
                thread_id,
                entry=entry,
                entry_version=entry.thread_version,
                current_thread_version=current_thread_version,
            )
            if incrementally_refreshed is not None:
                return incrementally_refreshed
            resolved_thread_cache.invalidate(room_id, thread_id)
        elif repair_required:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="repair_required",
                thread_version=current_thread_version,
            )
            resolved_thread_cache.invalidate(room_id, thread_id)
        return None

    async def _fetch_full_thread_history_from_source(
        self,
        room_id: str,
        thread_id: str,
        *,
        current_thread_version: int,
        repair_required: bool,
    ) -> ThreadHistoryResult:
        if repair_required:
            await self._invalidate_raw_thread_before_repair(room_id, thread_id)
        history = self._full_history_result(
            await self.fetch_thread_history_from_client(
                room_id,
                thread_id,
                refresh_cache=await self._should_refresh_cached_thread_history(room_id, thread_id),
            ),
            thread_version=current_thread_version,
        )
        if repair_required and not self._repair_history_is_authoritative(history):
            msg = "Repair-required Matrix thread history could not be authoritatively refilled"
            raise ThreadRepairRequiredError(msg)
        if self._should_store_resolved_thread_cache_entry(history):
            await self._store_resolved_thread_cache_entry(
                room_id,
                thread_id,
                history=history,
                thread_version=current_thread_version,
            )
        if repair_required and self._repair_history_durably_refilled(history):
            await self.clear_thread_refresh_required(room_id, thread_id)
        return history

    async def _read_full_thread_history(self, room_id: str, thread_id: str) -> ThreadHistoryResult:
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache().entry_lock(room_id, thread_id):
            await self.adopt_room_lookup_repairs_locked(room_id, thread_id)
            current_thread_version = self.thread_version(room_id, thread_id)
            repair_required = await self.thread_requires_refresh(room_id, thread_id)
            cached_history = await self._maybe_use_resolved_thread_cache(
                room_id,
                thread_id,
                current_thread_version=current_thread_version,
                repair_required=repair_required,
            )
            if cached_history is not None:
                return cached_history
            return await self._fetch_full_thread_history_from_source(
                room_id,
                thread_id,
                current_thread_version=current_thread_version,
                repair_required=repair_required,
            )

    async def _read_snapshot_thread(self, room_id: str, thread_id: str) -> ThreadHistoryResult:
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache().entry_lock(room_id, thread_id):
            await self.adopt_room_lookup_repairs_locked(room_id, thread_id)
            current_thread_version = self.thread_version(room_id, thread_id)
            repair_required = await self.thread_requires_refresh(room_id, thread_id)
            if repair_required:
                return await self._fetch_full_thread_history_from_source(
                    room_id,
                    thread_id,
                    current_thread_version=current_thread_version,
                    repair_required=repair_required,
                )
            return self._snapshot_result(
                await self.fetch_thread_snapshot_from_client(room_id, thread_id),
                thread_version=current_thread_version,
            )

    @staticmethod
    def _snapshot_result(
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_version: int,
    ) -> ThreadHistoryResult:
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=history.is_full_history,
                thread_version=thread_version,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(
            list(history),
            is_full_history=False,
            thread_version=thread_version,
        )

    @staticmethod
    def _full_history_result(
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_version: int,
    ) -> ThreadHistoryResult:
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=True,
                thread_version=thread_version,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(
            list(history),
            is_full_history=True,
            thread_version=thread_version,
        )

    async def _read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        require_full_history: bool,
    ) -> ThreadHistoryResult:
        if require_full_history:
            return await self._read_full_thread_history(room_id, thread_id)
        return await self._read_snapshot_thread(room_id, thread_id)

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadHistoryResult:
        """Resolve lightweight snapshot history for one thread."""
        return await self._read_thread(room_id, thread_id, require_full_history=False)

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadHistoryResult:
        """Resolve authoritative full history for one thread."""
        return await self._read_thread(room_id, thread_id, require_full_history=True)

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it.

        This path intentionally bypasses resolved-thread cache reuse and the
        sync-fresh shortcut used for normal thread-history reads. MSC3440
        fallback must prefer a fresh authoritative view of the thread tail.
        """
        if thread_id is None or existing_event_id is not None or reply_to_event_id is not None:
            return None
        await self._wait_for_pending_room_cache_updates(room_id)
        try:
            async with self._resolved_thread_cache().entry_lock(room_id, thread_id):
                await self.adopt_room_lookup_repairs_locked(room_id, thread_id)
                current_thread_version = self.thread_version(room_id, thread_id)
                repair_required = await self.thread_requires_refresh(room_id, thread_id)
                if repair_required:
                    await self._invalidate_raw_thread_before_repair(room_id, thread_id)
                thread_history = self._full_history_result(
                    await self.fetch_thread_history_from_client(
                        room_id,
                        thread_id,
                        refresh_cache=True,
                    ),
                    thread_version=current_thread_version,
                )
                if repair_required:
                    if not self._repair_history_is_authoritative(thread_history):
                        self._raise_thread_repair_required(thread_id)
                    if self._repair_history_durably_refilled(thread_history):
                        await self.clear_thread_refresh_required(room_id, thread_id)
                if self._should_store_resolved_thread_cache_entry(thread_history):
                    await self._store_resolved_thread_cache_entry(
                        room_id,
                        thread_id,
                        history=thread_history,
                        thread_version=current_thread_version,
                    )
        except Exception:
            return thread_id
        return latest_visible_thread_event_id(thread_history) or thread_id
