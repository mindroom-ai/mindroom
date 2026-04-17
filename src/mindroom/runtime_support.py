"""Shared ownership and lifecycle helpers for runtime Matrix event-cache services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.cache import _EventCache, _EventCacheWriteCoordinator

if TYPE_CHECKING:
    from pathlib import Path

    import structlog


class StartupThreadPrewarmRegistry:
    """Track one startup-wave claim set for room-level thread prewarm."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._claimed_room_ids: set[str] = set()

    async def try_claim(self, room_id: str) -> bool:
        """Claim one room for this startup wave unless another bot already did."""
        async with self._lock:
            if room_id in self._claimed_room_ids:
                return False
            self._claimed_room_ids.add(room_id)
            return True

    async def release(self, room_id: str) -> None:
        """Release one room claim so another bot may retry during the same startup wave."""
        async with self._lock:
            self._claimed_room_ids.discard(room_id)


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
