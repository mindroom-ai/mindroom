"""Standalone runtime support service lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.event_cache import EventCache
from mindroom.matrix.event_cache_write_coordinator import EventCacheWriteCoordinator

if TYPE_CHECKING:
    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(slots=True)
class StandaloneRuntimeSupport:
    """Concrete standalone-owned runtime support services for one bot."""

    event_cache: EventCache | None
    event_cache_write_coordinator: EventCacheWriteCoordinator | None


async def create_standalone_runtime_support(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
) -> StandaloneRuntimeSupport:
    """Create the standalone runtime support services for one direct bot runtime.

    The event cache is advisory, so SQLite init failures degrade to no cache support.
    The cache and its write coordinator are therefore injected together or not at all.
    """
    event_cache = await _initialize_standalone_event_cache(
        config=config,
        runtime_paths=runtime_paths,
        logger=logger,
    )
    if event_cache is None:
        return StandaloneRuntimeSupport(
            event_cache=None,
            event_cache_write_coordinator=None,
        )

    event_cache_write_coordinator = EventCacheWriteCoordinator(
        logger=logger,
        background_task_owner=background_task_owner,
    )
    return StandaloneRuntimeSupport(
        event_cache=event_cache,
        event_cache_write_coordinator=event_cache_write_coordinator,
    )


async def close_standalone_runtime_support(
    support: StandaloneRuntimeSupport,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Close one standalone-owned runtime support bundle in dependency order."""
    await _close_standalone_event_cache_write_coordinator(
        support.event_cache_write_coordinator,
        logger=logger,
    )
    await _close_standalone_event_cache(support.event_cache, logger=logger)


async def _initialize_standalone_event_cache(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    logger: structlog.stdlib.BoundLogger,
) -> EventCache | None:
    event_cache = EventCache(config.cache.resolve_db_path(runtime_paths))
    try:
        await event_cache.initialize()
    except Exception as exc:
        logger.warning("Failed to initialize event cache", error=str(exc))
        try:
            await event_cache.close()
        except Exception as close_exc:
            logger.warning("Failed to close partially initialized event cache", error=str(close_exc))
        return None
    return event_cache


async def _close_standalone_event_cache_write_coordinator(
    event_cache_write_coordinator: EventCacheWriteCoordinator | None,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    if event_cache_write_coordinator is None:
        return
    try:
        await event_cache_write_coordinator.close()
    except Exception as exc:
        logger.warning("Failed to close event cache write coordinator", error=str(exc))


async def _close_standalone_event_cache(
    event_cache: EventCache | None,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    if event_cache is None:
        return
    try:
        await event_cache.close()
    except Exception as exc:
        logger.warning("Failed to close event cache", error=str(exc))
