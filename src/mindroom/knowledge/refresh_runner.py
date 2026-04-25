"""Heavy knowledge refresh path run outside request handling."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING

from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.manager import KnowledgeManager, knowledge_source_signature
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.registry import (
    KnowledgeRefreshKey,
    KnowledgeSnapshotKey,
    KnowledgeSourceKey,
    PublishedIndexingState,
    active_snapshot_collection_names,
    clear_stale_ready_snapshot_markers,
    indexing_settings_metadata_equal,
    indexing_settings_snapshot_compatible,
    load_published_indexing_state,
    mark_published_snapshot_stale,
    prune_private_snapshot_bookkeeping,
    publish_snapshot_from_state,
    refresh_key_for_snapshot_key,
    resolve_refresh_key,
    resolve_snapshot_key,
    save_published_indexing_state,
    snapshot_availability_for_state,
    snapshot_collection_exists_for_state,
    snapshot_metadata_path,
    source_key_for_refresh_key,
    source_key_for_snapshot_key,
)
from mindroom.runtime_resolution import resolve_knowledge_binding

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class KnowledgeRefreshResult:
    """Result of one explicit knowledge refresh."""

    key: KnowledgeSnapshotKey
    indexed_count: int
    published: bool
    availability: KnowledgeAvailability
    last_error: str | None = None


_RefreshLockKey = tuple[KnowledgeSourceKey, int]
_refresh_locks: dict[_RefreshLockKey, asyncio.Lock] = {}
_refresh_lock_accessed_at: dict[_RefreshLockKey, float] = {}
_refresh_locks_guard = Lock()
_active_refresh_counts: dict[KnowledgeRefreshKey, int] = {}
_active_refresh_counts_guard = Lock()
_MAX_REFRESH_LOCKS = 512


def _running_loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def _refresh_lock_for_key(key: KnowledgeSourceKey) -> asyncio.Lock:
    lock_key = (key, _running_loop_key())
    with _refresh_locks_guard:
        _refresh_lock_accessed_at[lock_key] = time.monotonic()
        lock = _refresh_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _refresh_locks[lock_key] = lock
            _prune_refresh_locks_locked()
        return lock


def _prune_refresh_locks_locked() -> None:
    if len(_refresh_locks) <= _MAX_REFRESH_LOCKS:
        return
    excess = len(_refresh_locks) - _MAX_REFRESH_LOCKS
    candidates = sorted(_refresh_lock_accessed_at, key=_refresh_lock_accessed_at.__getitem__)
    for key in candidates:
        if excess <= 0:
            break
        lock = _refresh_locks.get(key)
        if lock is None or lock.locked():
            continue
        _refresh_locks.pop(key, None)
        _refresh_lock_accessed_at.pop(key, None)
        excess -= 1


def _mark_refresh_active(key: KnowledgeRefreshKey) -> None:
    with _active_refresh_counts_guard:
        _active_refresh_counts[key] = _active_refresh_counts.get(key, 0) + 1


def _mark_refresh_inactive(key: KnowledgeRefreshKey) -> None:
    with _active_refresh_counts_guard:
        count = _active_refresh_counts.get(key, 0)
        if count <= 1:
            _active_refresh_counts.pop(key, None)
        else:
            _active_refresh_counts[key] = count - 1


def mark_refresh_active(key: KnowledgeRefreshKey) -> None:
    """Record owner-level refresh activity before a task reaches the runner."""
    _mark_refresh_active(key)


def mark_refresh_inactive(key: KnowledgeRefreshKey) -> None:
    """Clear owner-level refresh activity after a scheduled task finishes."""
    _mark_refresh_inactive(key)


def is_refresh_active(key: KnowledgeRefreshKey) -> bool:
    """Return whether a refresh is active for one resolved physical binding."""
    with _active_refresh_counts_guard:
        return _active_refresh_counts.get(key, 0) > 0


def is_refresh_active_for_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> bool:
    """Resolve a binding and return whether it has an active refresh."""
    try:
        key = resolve_refresh_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            create=False,
        )
    except Exception:
        return False
    return is_refresh_active(key)


def _repersisted_settings(
    persisted_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> tuple[str, ...]:
    if indexing_settings_metadata_equal(persisted_settings, current_settings):
        return current_settings
    return persisted_settings


@asynccontextmanager
async def knowledge_binding_mutation_lock(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> AsyncIterator[None]:
    """Serialize direct source mutations with refresh publishes for one binding."""
    key = resolve_refresh_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    async with _refresh_lock_for_key(source_key_for_refresh_key(key)):
        yield


async def refresh_knowledge_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    force_reindex: bool = False,
) -> KnowledgeRefreshResult:
    """Build and publish one resolved knowledge binding."""
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    refresh_key = refresh_key_for_snapshot_key(key)
    _mark_refresh_active(refresh_key)
    try:
        async with _refresh_lock_for_key(source_key_for_snapshot_key(key)):
            return await _refresh_knowledge_binding_locked(
                key,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                force_reindex=force_reindex,
            )
    finally:
        _mark_refresh_inactive(refresh_key)
        prune_private_snapshot_bookkeeping()


async def _refresh_knowledge_binding_locked(
    key: KnowledgeSnapshotKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    force_reindex: bool = False,
) -> KnowledgeRefreshResult:
    base_id = key.base_id
    manager: KnowledgeManager | None = None
    try:
        binding = resolve_knowledge_binding(
            base_id,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            start_watchers=False,
            create=True,
        )
        manager = KnowledgeManager(
            base_id=base_id,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=binding.storage_root,
            knowledge_path=binding.knowledge_path,
            git_background_startup_allowed=False,
        )
        unchanged_result = await _maybe_publish_unchanged_snapshot(
            manager,
            key,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )
        if unchanged_result is not None:
            return unchanged_result
        indexed_count = await _reindex_manager_snapshot(manager, key)
    except Exception as exc:
        if manager is None:
            await _mark_refresh_setup_failed(key, config=config, runtime_paths=runtime_paths, error=str(exc))
        else:
            await _mark_refresh_failed(manager, key, config=config, runtime_paths=runtime_paths, error=str(exc))
        raise
    if manager._git_config() is not None:
        manager._mark_git_initial_sync_complete()

    return await _refresh_result_from_persisted_state(
        manager,
        key,
        indexed_count=indexed_count,
        config=config,
        runtime_paths=runtime_paths,
    )


async def _maybe_publish_unchanged_snapshot(
    manager: KnowledgeManager,
    key: KnowledgeSnapshotKey,
    *,
    execution_identity: ToolExecutionIdentity | None,
    force_reindex: bool,
) -> KnowledgeRefreshResult | None:
    if manager._git_config() is not None:
        git_sync_result = await manager.sync_git_repository(index_changes=False)
        if force_reindex or git_sync_result.get("updated", False):
            if git_sync_result.get("updated", False):
                mark_published_snapshot_stale(
                    key.base_id,
                    config=manager.config,
                    runtime_paths=manager.runtime_paths,
                    execution_identity=execution_identity,
                )
            return None
        return await _publish_unchanged_snapshot(
            manager,
            key,
            published_revision=manager._git_last_successful_commit,
            mark_git_initial_sync_complete=True,
        )
    if force_reindex:
        return None
    return await _publish_unchanged_snapshot(manager, key)


async def _refresh_result_from_persisted_state(
    manager: KnowledgeManager,
    key: KnowledgeSnapshotKey,
    *,
    indexed_count: int,
    config: Config,
    runtime_paths: RuntimePaths,
) -> KnowledgeRefreshResult:
    state = await asyncio.to_thread(load_published_indexing_state, snapshot_metadata_path(key))
    if state is not None and state.status != "complete":
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            published=False,
            availability=snapshot_availability_for_state(key=key, state=state),
            last_error=state.last_error,
        )
    if state is None:
        source_signature = await asyncio.to_thread(
            knowledge_source_signature,
            manager.config,
            manager.base_id,
            manager._knowledge_source_path(),
            tracked_relative_paths=manager._git_tracked_relative_paths,
        )
        state = PublishedIndexingState(
            settings=key.indexing_settings,
            status="complete",
            collection=manager._current_collection_name(),
            availability="ready",
            indexed_count=indexed_count,
            source_signature=source_signature,
        )
        await asyncio.to_thread(save_published_indexing_state, snapshot_metadata_path(key), state)
    availability = snapshot_availability_for_state(key=key, state=state)
    if not indexing_settings_snapshot_compatible(state.settings, key.indexing_settings):
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            published=False,
            availability=availability,
            last_error=state.last_error,
        )
    if availability is KnowledgeAvailability.REFRESH_FAILED:
        publish_snapshot_from_state(
            key,
            state=state,
            config=config,
            runtime_paths=runtime_paths,
            metadata_path=snapshot_metadata_path(key),
        )
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            published=False,
            availability=availability,
            last_error=state.last_error,
        )
    snapshot = publish_snapshot_from_state(
        key,
        state=state,
        config=config,
        runtime_paths=runtime_paths,
        metadata_path=snapshot_metadata_path(key),
    )
    if snapshot is None:
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            published=False,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            last_error=state.last_error,
        )
    _clear_stale_markers_after_publish(refresh_key_for_snapshot_key(key), state=state)
    return KnowledgeRefreshResult(
        key=key,
        indexed_count=indexed_count,
        published=True,
        availability=availability,
        last_error=state.last_error,
    )


async def _publish_unchanged_snapshot(
    manager: KnowledgeManager,
    key: KnowledgeSnapshotKey,
    *,
    published_revision: str | None = None,
    mark_git_initial_sync_complete: bool = False,
) -> KnowledgeRefreshResult | None:
    state = await asyncio.to_thread(load_published_indexing_state, snapshot_metadata_path(key))
    if (
        state is None
        or state.status != "complete"
        or state.source_signature is None
        or state.availability == KnowledgeAvailability.REFRESH_FAILED.value
        or snapshot_availability_for_state(key=key, state=state) is not KnowledgeAvailability.READY
        or not indexing_settings_metadata_equal(state.settings, key.indexing_settings)
        or not snapshot_collection_exists_for_state(key, state)
    ):
        return None

    current_source_signature = await asyncio.to_thread(
        knowledge_source_signature,
        manager.config,
        manager.base_id,
        manager._knowledge_source_path(),
        tracked_relative_paths=manager._git_tracked_relative_paths,
    )
    if current_source_signature != state.source_signature:
        return None

    updated_state = replace(
        state,
        settings=key.indexing_settings,
        availability="ready",
        last_published_at=datetime.now(tz=UTC).isoformat(),
        published_revision=published_revision or state.published_revision,
        last_error=None,
    )
    await asyncio.to_thread(
        save_published_indexing_state,
        snapshot_metadata_path(key),
        updated_state,
    )
    if mark_git_initial_sync_complete:
        manager._mark_git_initial_sync_complete()
    publish_snapshot_from_state(
        key,
        state=updated_state,
        config=manager.config,
        runtime_paths=manager.runtime_paths,
        metadata_path=snapshot_metadata_path(key),
    )
    _clear_stale_markers_after_publish(refresh_key_for_snapshot_key(key), state=updated_state)
    return KnowledgeRefreshResult(
        key=key,
        indexed_count=updated_state.indexed_count or 0,
        published=True,
        availability=snapshot_availability_for_state(key=key, state=updated_state),
        last_error=updated_state.last_error,
    )


async def _reindex_manager_snapshot(manager: KnowledgeManager, key: KnowledgeSnapshotKey) -> int:
    protected_collections = active_snapshot_collection_names(refresh_key_for_snapshot_key(key))
    if protected_collections:
        return await manager.reindex_all(protected_collections=protected_collections)
    return await manager.reindex_all()


async def _mark_refresh_failed(
    manager: KnowledgeManager,
    key: KnowledgeSnapshotKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    error: str,
) -> None:
    """Record refresh failure while preserving any last complete collection."""
    redacted_error = redact_credentials_in_text(error)
    state = await asyncio.to_thread(manager._load_persisted_indexing_state)
    if state is None:
        await asyncio.to_thread(
            manager._save_persisted_indexing_state,
            "indexing",
            availability="refresh_failed",
            last_error=redacted_error,
        )
        return
    if state.status != "complete":
        repersisted_settings = _repersisted_settings(state.settings, key.indexing_settings)
        await asyncio.to_thread(
            manager._save_persisted_indexing_state,
            state.status,
            settings=repersisted_settings,
            collection=state.collection,
            availability="refresh_failed",
            last_published_at=state.last_published_at,
            published_revision=state.published_revision,
            last_error=redacted_error,
            indexed_count=state.indexed_count,
            source_signature=state.source_signature,
            retained_collections=state.retained_collections,
        )
        return
    repersisted_settings = _repersisted_settings(state.settings, key.indexing_settings)
    await asyncio.to_thread(
        manager._save_persisted_indexing_state,
        "complete",
        settings=repersisted_settings,
        collection=state.collection,
        availability="refresh_failed",
        last_published_at=state.last_published_at,
        published_revision=state.published_revision,
        last_error=redacted_error,
        indexed_count=state.indexed_count,
        source_signature=state.source_signature,
        retained_collections=state.retained_collections,
    )
    refreshed_state = await asyncio.to_thread(load_published_indexing_state, snapshot_metadata_path(key))
    if refreshed_state is None:
        return
    if not indexing_settings_snapshot_compatible(refreshed_state.settings, key.indexing_settings):
        return
    if snapshot_availability_for_state(key=key, state=refreshed_state) is KnowledgeAvailability.CONFIG_MISMATCH:
        return
    publish_snapshot_from_state(
        key,
        state=refreshed_state,
        config=config,
        runtime_paths=runtime_paths,
        metadata_path=snapshot_metadata_path(key),
    )


async def _mark_refresh_setup_failed(
    key: KnowledgeSnapshotKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    error: str,
) -> None:
    """Record refresh failure before a KnowledgeManager can be constructed."""
    redacted_error = redact_credentials_in_text(error)
    metadata_path = snapshot_metadata_path(key)
    state = await asyncio.to_thread(load_published_indexing_state, metadata_path)
    if state is None:
        failed_state = PublishedIndexingState(
            settings=key.indexing_settings,
            status="indexing",
            availability=KnowledgeAvailability.REFRESH_FAILED.value,
            last_error=redacted_error,
            indexed_count=0,
        )
    else:
        repersisted_settings = _repersisted_settings(state.settings, key.indexing_settings)
        failed_state = replace(
            state,
            settings=repersisted_settings,
            availability=KnowledgeAvailability.REFRESH_FAILED.value,
            last_error=redacted_error,
        )
    await asyncio.to_thread(save_published_indexing_state, metadata_path, failed_state)
    if failed_state.status != "complete":
        return
    if not indexing_settings_snapshot_compatible(failed_state.settings, key.indexing_settings):
        return
    if snapshot_availability_for_state(key=key, state=failed_state) is KnowledgeAvailability.CONFIG_MISMATCH:
        return
    publish_snapshot_from_state(
        key,
        state=failed_state,
        config=config,
        runtime_paths=runtime_paths,
        metadata_path=metadata_path,
    )


def _clear_stale_markers_after_publish(key: KnowledgeRefreshKey, *, state: PublishedIndexingState) -> None:
    if state.status == "complete" and state.availability in {None, "", KnowledgeAvailability.READY.value}:
        clear_stale_ready_snapshot_markers(key)
