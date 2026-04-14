"""Standalone runtime support service lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.conversation_cache import (
    EventCache as _EventCache,
)
from mindroom.matrix.conversation_cache import (
    EventCacheWriteCoordinator as _EventCacheWriteCoordinator,
)

if TYPE_CHECKING:
    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(slots=True)
class StandaloneRuntimeSupport:
    """Concrete standalone-owned runtime support services for one bot."""

    event_cache: _EventCache
    event_cache_write_coordinator: _EventCacheWriteCoordinator


def build_standalone_runtime_support(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
) -> StandaloneRuntimeSupport:
    """Build the standalone runtime support services for one direct bot runtime."""
    return StandaloneRuntimeSupport(
        event_cache=_EventCache(config.cache.resolve_db_path(runtime_paths)),
        event_cache_write_coordinator=_EventCacheWriteCoordinator(
            logger=logger,
            background_task_owner=background_task_owner,
        ),
    )


async def initialize_standalone_runtime_support(
    support: StandaloneRuntimeSupport,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Initialize the standalone-owned event cache and fail fast on SQLite errors."""
    try:
        await support.event_cache.initialize()
    except Exception:
        try:
            await support.event_cache.close()
        except Exception:
            logger.warning("Failed to close partially initialized event cache", exc_info=True)
        raise


async def create_standalone_runtime_support(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
) -> StandaloneRuntimeSupport:
    """Build and initialize the standalone runtime support services for one direct bot runtime."""
    support = build_standalone_runtime_support(
        config=config,
        runtime_paths=runtime_paths,
        logger=logger,
        background_task_owner=background_task_owner,
    )
    await initialize_standalone_runtime_support(support, logger=logger)
    return support


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


async def _close_standalone_event_cache_write_coordinator(
    event_cache_write_coordinator: _EventCacheWriteCoordinator,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    try:
        await event_cache_write_coordinator.close()
    except Exception as exc:
        logger.warning("Failed to close event cache write coordinator", error=str(exc))


async def _close_standalone_event_cache(
    event_cache: _EventCache,
    *,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    try:
        await event_cache.close()
    except Exception as exc:
        logger.warning("Failed to close event cache", error=str(exc))
