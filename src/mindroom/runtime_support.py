"""Shared ownership and lifecycle helpers for runtime Matrix event-cache services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.matrix.cache import _EventCache, _EventCacheWriteCoordinator

if TYPE_CHECKING:
    from pathlib import Path

    import structlog


@dataclass(slots=True)
class StartupThreadPrewarmRegistry:
    """Track startup thread-prewarm claims and completion per room."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _states: dict[str, str] = field(default_factory=dict)

    async def try_claim(self, room_id: str) -> bool:
        """Claim one room for startup prewarm unless it is already running or done."""
        async with self._lock:
            if room_id in self._states:
                return False
            self._states[room_id] = "running"
            return True

    async def mark_done(self, room_id: str) -> None:
        """Mark one room's startup prewarm as finished."""
        async with self._lock:
            if self._states.get(room_id) == "running":
                self._states[room_id] = "done"

    async def release(self, room_id: str) -> None:
        """Release an in-flight room claim so another bot may retry later."""
        async with self._lock:
            if self._states.get(room_id) == "running":
                self._states.pop(room_id, None)


@dataclass(slots=True)
class OwnedRuntimeSupport:
    """Concrete event-cache services owned by one runtime lifecycle."""

    event_cache: _EventCache
    event_cache_write_coordinator: _EventCacheWriteCoordinator
    startup_thread_prewarm_registry: StartupThreadPrewarmRegistry


def build_owned_runtime_support(
    *,
    db_path: Path,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
) -> OwnedRuntimeSupport:
    """Build one owned runtime-support bundle without initializing the cache."""
    return OwnedRuntimeSupport(
        event_cache=_EventCache(db_path),
        event_cache_write_coordinator=_EventCacheWriteCoordinator(
            logger=logger,
            background_task_owner=background_task_owner,
        ),
        startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
    )


async def sync_owned_runtime_support(
    support: OwnedRuntimeSupport | None,
    *,
    db_path: Path,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
    init_failure_reason_prefix: str,
    log_db_path_change: bool,
) -> OwnedRuntimeSupport:
    """Build, rebind, and initialize one owned runtime-support bundle."""
    if support is None:
        support = build_owned_runtime_support(
            db_path=db_path,
            logger=logger,
            background_task_owner=background_task_owner,
        )
    else:
        support.event_cache_write_coordinator.background_task_owner = background_task_owner
        if not support.event_cache.is_initialized and support.event_cache.db_path != db_path:
            support = build_owned_runtime_support(
                db_path=db_path,
                logger=logger,
                background_task_owner=background_task_owner,
            )
        elif support.event_cache.db_path != db_path and log_db_path_change:
            logger.info(
                "Event cache db_path change will apply after restart",
                active_db_path=str(support.event_cache.db_path),
                configured_db_path=str(db_path),
            )

    if support.event_cache.is_initialized:
        return support

    try:
        await support.event_cache.initialize()
    except Exception as exc:
        support.event_cache.disable(f"{init_failure_reason_prefix}:{exc}")
        logger.warning(
            "Event cache init failed; continuing without advisory cache",
            db_path=str(support.event_cache.db_path),
            error=str(exc),
        )
    return support


async def close_owned_runtime_support(
    support: OwnedRuntimeSupport,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Close one owned runtime-support bundle in dependency order."""
    try:
        await support.event_cache_write_coordinator.close()
    except Exception as exc:
        logger.warning("Failed to close event cache write coordinator", error=str(exc))

    try:
        await support.event_cache.close()
    except Exception as exc:
        logger.warning("Failed to close event cache", error=str(exc))
