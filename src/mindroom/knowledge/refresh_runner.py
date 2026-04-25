"""Heavy knowledge refresh path run outside request handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.registry import (
    KnowledgeRefreshKey,
    KnowledgeSnapshotKey,
    PublishedIndexingState,
    indexing_settings_snapshot_compatible,
    load_published_indexing_state,
    publish_snapshot,
    refresh_key_for_snapshot_key,
    resolve_snapshot_key,
    snapshot_availability_for_state,
    snapshot_metadata_path,
)
from mindroom.runtime_resolution import resolve_knowledge_binding

if TYPE_CHECKING:
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


_refresh_locks: dict[KnowledgeRefreshKey, asyncio.Lock] = {}
_refresh_locks_guard = Lock()


def _refresh_lock_for_key(key: KnowledgeRefreshKey) -> asyncio.Lock:
    with _refresh_locks_guard:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _refresh_locks[key] = lock
        return lock


async def refresh_knowledge_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeRefreshResult:
    """Build and publish one resolved knowledge binding."""
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    async with _refresh_lock_for_key(refresh_key_for_snapshot_key(key)):
        return await _refresh_knowledge_binding_locked(
            key,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )


async def _refresh_knowledge_binding_locked(
    key: KnowledgeSnapshotKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeRefreshResult:
    base_id = key.base_id
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
    try:
        if manager._git_config() is not None:
            await manager.sync_git_repository(index_changes=False)
        indexed_count = await manager.reindex_all()
    except Exception as exc:
        await _mark_refresh_failed(manager, key, error=str(exc))
        raise
    if manager._git_config() is not None:
        manager._mark_git_initial_sync_complete()

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
        state = PublishedIndexingState(
            settings=key.indexing_settings,
            status="complete",
            collection=manager._current_collection_name(),
            availability="ready",
            indexed_count=indexed_count,
        )
    availability = snapshot_availability_for_state(key=key, state=state)
    if (
        not indexing_settings_snapshot_compatible(state.settings, key.indexing_settings)
        or availability is KnowledgeAvailability.REFRESH_FAILED
    ):
        return KnowledgeRefreshResult(
            key=key,
            indexed_count=indexed_count,
            published=False,
            availability=availability,
            last_error=state.last_error,
        )
    publish_snapshot(
        key,
        knowledge=manager.get_knowledge(),
        state=state,
        metadata_path=snapshot_metadata_path(key),
    )
    return KnowledgeRefreshResult(
        key=key,
        indexed_count=indexed_count,
        published=True,
        availability=availability,
        last_error=state.last_error,
    )


async def _mark_refresh_failed(manager: KnowledgeManager, key: KnowledgeSnapshotKey, *, error: str) -> None:
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
        await asyncio.to_thread(
            manager._save_persisted_indexing_state,
            state.status,
            settings=state.settings,
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
    await asyncio.to_thread(
        manager._save_persisted_indexing_state,
        "complete",
        settings=state.settings,
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
    publish_snapshot(
        key,
        knowledge=manager.get_knowledge(),
        state=refreshed_state,
        metadata_path=snapshot_metadata_path(key),
    )
