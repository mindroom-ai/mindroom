"""Heavy knowledge refresh path run outside request handling."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING

from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.manager import KnowledgeManager, knowledge_source_signature
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    KnowledgeSourceRoot,
    PublishedIndexKey,
    PublishedIndexState,
    indexing_settings_metadata_equal,
    load_published_index_state,
    mark_knowledge_source_changed_async,
    mark_published_index_refresh_failed_preserving_last_good,
    mark_published_index_refresh_running,
    mark_published_index_refresh_succeeded,
    mark_published_index_stale,
    prune_private_index_bookkeeping,
    publish_knowledge_index_from_state,
    published_index_availability_for_state,
    published_index_collection_exists_for_state,
    published_index_metadata_path,
    published_index_settings_compatible,
    refresh_target_for_published_index_key,
    resolve_published_index_key,
    resolve_refresh_target,
    save_published_index_state,
    source_root_for_published_index_key,
    source_root_for_refresh_target,
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

    key: PublishedIndexKey
    indexed_count: int
    index_published: bool
    availability: KnowledgeAvailability
    last_error: str | None = None


_refresh_locks_guard = Lock()
_active_refresh_counts: dict[KnowledgeRefreshTarget, int] = {}
_active_refresh_counts_guard = Lock()
_MAX_REFRESH_LOCKS = 512


@dataclass
class _RefreshLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    borrowers: int = 0


_refresh_locks: dict[KnowledgeSourceRoot, _RefreshLockEntry] = {}


def _borrow_refresh_lock_for_key(key: KnowledgeSourceRoot) -> _RefreshLockEntry:
    with _refresh_locks_guard:
        entry = _refresh_locks.get(key)
        if entry is None:
            _prune_refresh_locks_locked(reserve_slots=1)
            entry = _RefreshLockEntry()
            _refresh_locks[key] = entry
        entry.borrowers += 1
        return entry


def _release_refresh_lock_for_key(key: KnowledgeSourceRoot, entry: _RefreshLockEntry) -> None:
    with _refresh_locks_guard:
        if entry.borrowers <= 0:
            return
        entry.borrowers -= 1
        if _refresh_locks.get(key) is entry:
            _prune_refresh_locks_locked()


def _prune_refresh_locks_locked(*, reserve_slots: int = 0) -> None:
    target_size = max(_MAX_REFRESH_LOCKS - reserve_slots, 0)
    if len(_refresh_locks) <= target_size:
        return
    excess = len(_refresh_locks) - target_size
    for key, entry in tuple(_refresh_locks.items()):
        if excess <= 0:
            break
        if entry.borrowers > 0 or entry.lock.locked():
            continue
        _refresh_locks.pop(key, None)
        excess -= 1


@asynccontextmanager
async def _acquire_refresh_lock(key: KnowledgeSourceRoot) -> AsyncIterator[None]:
    entry = _borrow_refresh_lock_for_key(key)
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        _release_refresh_lock_for_key(key, entry)


def _mark_refresh_active(key: KnowledgeRefreshTarget) -> None:
    with _active_refresh_counts_guard:
        _active_refresh_counts[key] = _active_refresh_counts.get(key, 0) + 1


def _mark_refresh_inactive(key: KnowledgeRefreshTarget) -> None:
    with _active_refresh_counts_guard:
        count = _active_refresh_counts.get(key, 0)
        if count <= 1:
            _active_refresh_counts.pop(key, None)
        else:
            _active_refresh_counts[key] = count - 1


def mark_refresh_active(key: KnowledgeRefreshTarget) -> None:
    """Record scheduler-level refresh activity before a task reaches the runner."""
    _mark_refresh_active(key)


def mark_refresh_inactive(key: KnowledgeRefreshTarget) -> None:
    """Clear scheduler-level refresh activity after a scheduled task finishes."""
    _mark_refresh_inactive(key)


def is_refresh_active(key: KnowledgeRefreshTarget) -> bool:
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
        key = resolve_refresh_target(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            create=False,
        )
    except Exception:
        return False
    return is_refresh_active(key)


@asynccontextmanager
async def knowledge_binding_mutation_lock(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> AsyncIterator[None]:
    """Serialize source mutations with refresh publishes in this runtime event loop."""
    key = resolve_refresh_target(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    async with _acquire_refresh_lock(source_root_for_refresh_target(key)):
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
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    refresh_target = refresh_target_for_published_index_key(key)
    _mark_refresh_active(refresh_target)
    try:
        initial_state = await asyncio.to_thread(
            load_published_index_state,
            published_index_metadata_path(key),
        )
        try:
            await _save_refreshing_state(key)
            async with _acquire_refresh_lock(source_root_for_published_index_key(key)):
                return await _refresh_knowledge_binding_locked(
                    key,
                    config=config,
                    runtime_paths=runtime_paths,
                    execution_identity=execution_identity,
                    force_reindex=force_reindex,
                )
        except asyncio.CancelledError:
            await _reconcile_cancelled_refresh(
                key,
                initial_state=initial_state,
                config=config,
                runtime_paths=runtime_paths,
            )
            raise
    finally:
        _mark_refresh_inactive(refresh_target)
        prune_private_index_bookkeeping()


async def _save_refreshing_state(key: PublishedIndexKey) -> None:
    write_task = asyncio.create_task(asyncio.to_thread(mark_published_index_refresh_running, key))
    try:
        await asyncio.shield(write_task)
    except asyncio.CancelledError:
        write_completed = False
        with suppress(Exception):
            await write_task
            write_completed = True
        if write_completed:
            with suppress(Exception):
                await asyncio.to_thread(mark_published_index_stale, key, reason="refresh_cancelled", refresh_job="idle")
        raise


async def _refresh_knowledge_binding_locked(
    key: PublishedIndexKey,
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
        )
        unchanged_result = await _maybe_publish_unchanged_index(
            manager,
            key,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )
        if unchanged_result is not None:
            return unchanged_result
        indexed_count = await _reindex_manager_index(manager, key)
        if manager._last_refresh_error is not None:
            error = redact_credentials_in_text(manager._last_refresh_error)
            await asyncio.to_thread(mark_published_index_refresh_failed_preserving_last_good, key, error=error)
            return KnowledgeRefreshResult(
                key=key,
                indexed_count=indexed_count,
                index_published=False,
                availability=KnowledgeAvailability.REFRESH_FAILED,
                last_error=error,
            )
    except Exception as exc:
        if manager is None:
            await _mark_refresh_setup_failed(key, config=config, runtime_paths=runtime_paths, error=str(exc))
        else:
            await _mark_refresh_failed(manager, key, config=config, runtime_paths=runtime_paths, error=str(exc))
        raise
    if manager._git_config() is not None:
        manager._mark_git_initial_sync_complete()

    return await _refresh_result_from_persisted_state(
        key,
        indexed_count=indexed_count,
        config=config,
        runtime_paths=runtime_paths,
    )


async def _maybe_publish_unchanged_index(
    manager: KnowledgeManager,
    key: PublishedIndexKey,
    *,
    execution_identity: ToolExecutionIdentity | None,
    force_reindex: bool,
) -> KnowledgeRefreshResult | None:
    force_reindex = force_reindex or manager._needs_full_reindex_on_create()
    if manager._git_config() is not None:
        git_sync_result = await manager.sync_git_source()
        if force_reindex or git_sync_result.get("updated", False):
            if git_sync_result.get("updated", False):
                await mark_knowledge_source_changed_async(
                    key.base_id,
                    config=manager.config,
                    runtime_paths=manager.runtime_paths,
                    execution_identity=execution_identity,
                    reason="git_source_updated",
                )
            return None
        return await _publish_unchanged_index(
            manager,
            key,
            published_revision=manager._git_last_successful_commit,
            mark_git_initial_sync_complete=True,
        )
    if force_reindex:
        await mark_knowledge_source_changed_async(
            key.base_id,
            config=manager.config,
            runtime_paths=manager.runtime_paths,
            execution_identity=execution_identity,
            reason="manual_reindex",
        )
        return None
    return await _publish_unchanged_index(
        manager,
        key,
        mark_stale_on_source_change=True,
        execution_identity=execution_identity,
    )


async def _refresh_result_from_persisted_state(
    key: PublishedIndexKey,
    *,
    indexed_count: int,
    config: Config,
    runtime_paths: RuntimePaths,
) -> KnowledgeRefreshResult:
    state = await asyncio.to_thread(load_published_index_state, published_index_metadata_path(key))
    if state is None:
        error = "Published index metadata was missing after refresh"
        await asyncio.to_thread(mark_published_index_refresh_failed_preserving_last_good, key, error=error)
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            index_published=False,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            last_error=error,
        )
    if state.status != "complete":
        error = "Published index metadata was incomplete after refresh"
        await asyncio.to_thread(mark_published_index_refresh_failed_preserving_last_good, key, error=error)
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            index_published=False,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            last_error=error,
        )

    availability = published_index_availability_for_state(key=key, state=state)
    if not published_index_settings_compatible(state.settings, key.indexing_settings):
        await asyncio.to_thread(
            mark_published_index_stale,
            key,
            reason="published_index_config_mismatch",
        )
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            index_published=False,
            availability=availability,
            last_error=None,
        )
    index = publish_knowledge_index_from_state(
        key,
        state=state,
        config=config,
        runtime_paths=runtime_paths,
        metadata_path=published_index_metadata_path(key),
    )
    if index is None:
        error = "Published index collection was missing after refresh"
        await asyncio.to_thread(mark_published_index_refresh_failed_preserving_last_good, key, error=error)
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            index_published=False,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            last_error=error,
        )
    await asyncio.to_thread(mark_published_index_refresh_succeeded, key)
    return KnowledgeRefreshResult(
        key=key,
        indexed_count=indexed_count,
        index_published=True,
        availability=KnowledgeAvailability.READY,
        last_error=None,
    )


async def _publish_unchanged_index(
    manager: KnowledgeManager,
    key: PublishedIndexKey,
    *,
    published_revision: str | None = None,
    mark_git_initial_sync_complete: bool = False,
    mark_stale_on_source_change: bool = False,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeRefreshResult | None:
    state = await asyncio.to_thread(load_published_index_state, published_index_metadata_path(key))
    if (
        state is None
        or state.status != "complete"
        or state.source_signature is None
        or not indexing_settings_metadata_equal(state.settings, key.indexing_settings)
        or not await asyncio.to_thread(published_index_collection_exists_for_state, key, state)
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
        if mark_stale_on_source_change:
            await mark_knowledge_source_changed_async(
                key.base_id,
                config=manager.config,
                runtime_paths=manager.runtime_paths,
                execution_identity=execution_identity,
                reason="source_changed",
            )
        return None

    updated_state = state
    if mark_git_initial_sync_complete:
        manager._mark_git_initial_sync_complete()
    if state.settings != key.indexing_settings:
        updated_state = replace(updated_state, settings=key.indexing_settings)
    if published_revision is not None:
        updated_state = replace(
            updated_state,
            last_published_at=datetime.now(tz=UTC).isoformat(),
            published_revision=published_revision,
        )
    if updated_state != state:
        await asyncio.to_thread(save_published_index_state, published_index_metadata_path(key), updated_state)
    index = publish_knowledge_index_from_state(
        key,
        state=updated_state,
        config=manager.config,
        runtime_paths=manager.runtime_paths,
        metadata_path=published_index_metadata_path(key),
    )
    if index is None:
        error = "Published index collection was missing during unchanged refresh"
        await asyncio.to_thread(mark_published_index_refresh_failed_preserving_last_good, key, error=error)
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=updated_state.indexed_count or 0,
            index_published=False,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            last_error=error,
        )
    await asyncio.to_thread(mark_published_index_refresh_succeeded, key)
    return KnowledgeRefreshResult(
        key=key,
        indexed_count=updated_state.indexed_count or 0,
        index_published=True,
        availability=KnowledgeAvailability.READY,
        last_error=None,
    )


def _published_state_fingerprint(state: PublishedIndexState | None) -> tuple[object, ...] | None:
    if state is None:
        return None
    return (
        state.settings,
        state.status,
        state.collection,
        state.last_published_at,
        state.published_revision,
        state.indexed_count,
        state.source_signature,
    )


async def _reconcile_cancelled_refresh(
    key: PublishedIndexKey,
    *,
    initial_state: PublishedIndexState | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    state = await asyncio.to_thread(load_published_index_state, published_index_metadata_path(key))
    state_advanced = _published_state_fingerprint(state) != _published_state_fingerprint(initial_state)
    if (
        state_advanced
        and state is not None
        and state.status == "complete"
        and published_index_settings_compatible(state.settings, key.indexing_settings)
        and published_index_availability_for_state(key=key, state=state) is KnowledgeAvailability.READY
        and await asyncio.to_thread(published_index_collection_exists_for_state, key, state)
    ):
        index = publish_knowledge_index_from_state(
            key,
            state=state,
            config=config,
            runtime_paths=runtime_paths,
            metadata_path=published_index_metadata_path(key),
        )
        if index is not None:
            await asyncio.to_thread(mark_published_index_refresh_succeeded, key)
            return
    await asyncio.to_thread(mark_published_index_stale, key, reason="refresh_cancelled", refresh_job="idle")


async def _reindex_manager_index(manager: KnowledgeManager, key: PublishedIndexKey) -> int:
    _ = key
    return await manager.reindex_all()


async def _mark_refresh_failed(
    manager: KnowledgeManager,
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    error: str,
) -> None:
    """Record refresh failure while preserving any last complete collection."""
    _ = (manager, config, runtime_paths)
    await asyncio.to_thread(
        mark_published_index_refresh_failed_preserving_last_good,
        key,
        error=redact_credentials_in_text(error),
    )


async def _mark_refresh_setup_failed(
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    error: str,
) -> None:
    """Record refresh failure before a KnowledgeManager can be constructed."""
    redacted_error = redact_credentials_in_text(error)
    _ = (config, runtime_paths)
    await asyncio.to_thread(mark_published_index_refresh_failed_preserving_last_good, key, error=redacted_error)
