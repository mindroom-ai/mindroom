"""Heavy knowledge refresh path run outside request handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.registry import (
    KnowledgeSnapshotKey,
    PublishedIndexingState,
    load_published_indexing_state,
    publish_snapshot,
    resolve_snapshot_key,
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
    except Exception:
        await _mark_refresh_failed_if_snapshot_exists(manager, key)
        raise
    if manager._git_config() is not None:
        manager._mark_git_initial_sync_complete()

    state = await asyncio.to_thread(load_published_indexing_state, snapshot_metadata_path(key))
    if state is None:
        state = PublishedIndexingState(
            settings=key.indexing_settings,
            status="complete",
            collection=manager._current_collection_name(),
            availability="ready",
        )
    publish_snapshot(
        key,
        knowledge=manager.get_knowledge(),
        state=state,
        metadata_path=snapshot_metadata_path(key),
    )
    return KnowledgeRefreshResult(key=key, indexed_count=indexed_count)


async def _mark_refresh_failed_if_snapshot_exists(manager: KnowledgeManager, key: KnowledgeSnapshotKey) -> None:
    """Record refresh failure while preserving the last complete collection."""
    state = await asyncio.to_thread(manager._load_persisted_indexing_state)
    if state is None or state.status != "complete":
        return
    await asyncio.to_thread(
        manager._save_persisted_indexing_state,
        "complete",
        collection=state.collection,
        availability="refresh_failed",
        last_published_at=state.last_published_at,
        published_revision=state.published_revision,
    )
    refreshed_state = await asyncio.to_thread(load_published_indexing_state, snapshot_metadata_path(key))
    if refreshed_state is None:
        return
    publish_snapshot(
        key,
        knowledge=manager.get_knowledge(),
        state=refreshed_state,
        metadata_path=snapshot_metadata_path(key),
    )
