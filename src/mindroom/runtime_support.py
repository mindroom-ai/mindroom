"""Shared ownership and lifecycle helpers for runtime Matrix event-cache services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, cast

from mindroom.config.matrix import CacheConfig
from mindroom.constants import RuntimePaths
from mindroom.matrix.cache.postgres_redaction import redact_postgres_connection_info
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.tool_system.dependencies import ensure_optional_deps

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache


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


@dataclass(frozen=True, slots=True)
class EventCacheRuntimeIdentity:
    """Comparable runtime identity for one event-cache backend binding."""

    backend: str
    location: str
    namespace: str | None = None

    @property
    def redacted_location(self) -> str:
        """Return a log-safe description of the backing store location."""
        if self.backend != "postgres":
            return self.location
        return redact_postgres_connection_info(self.location)


@dataclass(slots=True)
class OwnedRuntimeSupport:
    """Concrete event-cache services owned by one runtime lifecycle."""

    event_cache: ConversationEventCache
    event_cache_write_coordinator: _EventCacheWriteCoordinator
    startup_thread_prewarm_registry: StartupThreadPrewarmRegistry
    event_cache_identity: EventCacheRuntimeIdentity


def event_cache_runtime_identity(
    cache_config: CacheConfig,
    runtime_paths: RuntimePaths,
) -> EventCacheRuntimeIdentity:
    """Return the concrete event-cache runtime identity implied by config."""
    if cache_config.backend != "postgres":
        return EventCacheRuntimeIdentity(
            backend="sqlite",
            location=str(cache_config.resolve_db_path(runtime_paths)),
        )
    return EventCacheRuntimeIdentity(
        backend="postgres",
        location=cache_config.resolve_postgres_database_url(runtime_paths),
        namespace=cache_config.resolve_namespace(runtime_paths),
    )


def _load_postgres_event_cache_class(runtime_paths: RuntimePaths) -> type[PostgresEventCache]:
    """Ensure Postgres dependencies are importable, then load the concrete backend class."""
    ensure_optional_deps(["psycopg"], "postgres", runtime_paths)
    postgres_module = import_module("mindroom.matrix.cache.postgres_event_cache")
    return cast("type[PostgresEventCache]", postgres_module.PostgresEventCache)


def build_event_cache(
    cache_config: CacheConfig,
    runtime_paths: RuntimePaths,
) -> ConversationEventCache:
    """Build the configured event-cache backend without initializing it."""
    if cache_config.backend != "postgres":
        return SqliteEventCache(cache_config.resolve_db_path(runtime_paths))

    database_url = cache_config.resolve_postgres_database_url(runtime_paths)
    namespace = cache_config.resolve_namespace(runtime_paths)
    postgres_event_cache_class = _load_postgres_event_cache_class(runtime_paths)
    return postgres_event_cache_class(database_url=database_url, namespace=namespace)


def build_owned_runtime_support(
    *,
    db_path: Path | None = None,
    cache_config: CacheConfig | None = None,
    runtime_paths: RuntimePaths | None = None,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
) -> OwnedRuntimeSupport:
    """Build one owned runtime-support bundle without initializing the cache."""
    if cache_config is None:
        if db_path is None:
            msg = "build_owned_runtime_support requires db_path or cache_config"
            raise ValueError(msg)
        cache_config = CacheConfig(db_path=str(db_path))
    if runtime_paths is None:
        if db_path is None:
            msg = "build_owned_runtime_support requires runtime_paths when db_path is omitted"
            raise ValueError(msg)
        runtime_paths = RuntimePaths(
            config_path=db_path.parent / "config.yaml",
            config_dir=db_path.parent,
            env_path=db_path.parent / ".env",
            storage_root=db_path.parent,
        )
    cache_identity = event_cache_runtime_identity(cache_config, runtime_paths)
    return OwnedRuntimeSupport(
        event_cache=build_event_cache(cache_config, runtime_paths),
        event_cache_write_coordinator=_EventCacheWriteCoordinator(
            logger=logger,
            background_task_owner=background_task_owner,
        ),
        startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        event_cache_identity=cache_identity,
    )


async def sync_owned_runtime_support(
    support: OwnedRuntimeSupport | None,
    *,
    db_path: Path | None = None,
    cache_config: CacheConfig | None = None,
    runtime_paths: RuntimePaths | None = None,
    logger: structlog.stdlib.BoundLogger,
    background_task_owner: object,
    init_failure_reason_prefix: str,
    log_db_path_change: bool,
) -> OwnedRuntimeSupport:
    """Build, rebind, and initialize one owned runtime-support bundle."""
    if cache_config is None:
        if db_path is None:
            msg = "sync_owned_runtime_support requires db_path or cache_config"
            raise ValueError(msg)
        cache_config = CacheConfig(db_path=str(db_path))
    if runtime_paths is None:
        if db_path is None:
            msg = "sync_owned_runtime_support requires runtime_paths when db_path is omitted"
            raise ValueError(msg)
        runtime_paths = RuntimePaths(
            config_path=db_path.parent / "config.yaml",
            config_dir=db_path.parent,
            env_path=db_path.parent / ".env",
            storage_root=db_path.parent,
        )
    target_identity = event_cache_runtime_identity(cache_config, runtime_paths)
    if support is None:
        support = build_owned_runtime_support(
            cache_config=cache_config,
            runtime_paths=runtime_paths,
            logger=logger,
            background_task_owner=background_task_owner,
        )
    else:
        support.event_cache_write_coordinator.background_task_owner = background_task_owner
        if not support.event_cache.is_initialized and support.event_cache_identity != target_identity:
            support = build_owned_runtime_support(
                cache_config=cache_config,
                runtime_paths=runtime_paths,
                logger=logger,
                background_task_owner=background_task_owner,
            )
        elif support.event_cache_identity != target_identity and log_db_path_change:
            logger.info(
                "Event cache backend change will apply after restart",
                active_backend=support.event_cache_identity.backend,
                active_location=support.event_cache_identity.redacted_location,
                active_namespace=support.event_cache_identity.namespace,
                configured_backend=target_identity.backend,
                configured_location=target_identity.redacted_location,
                configured_namespace=target_identity.namespace,
            )

    if support.event_cache.is_initialized:
        return support

    try:
        await support.event_cache.initialize()
    except Exception as exc:
        support.event_cache.disable(f"{init_failure_reason_prefix}:{exc}")
        logger.warning(
            "Event cache init failed; continuing without advisory cache",
            backend=support.event_cache_identity.backend,
            location=support.event_cache_identity.redacted_location,
            namespace=support.event_cache_identity.namespace,
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
