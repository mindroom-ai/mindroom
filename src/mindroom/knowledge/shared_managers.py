"""Shared knowledge manager orchestration."""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.knowledge.manager import KnowledgeManager, _ensure_knowledge_directory_ready
from mindroom.knowledge.startup import (
    initialize_manager_for_startup,
    startup_log_context,
    sync_manager_without_full_reindex,
)
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import ResolvedKnowledgeBinding, resolve_knowledge_binding

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class _KnowledgeManagerKey:
    """Stable cache key for one effective knowledge manager instance."""

    base_id: str
    storage_path: str
    knowledge_path: str


@dataclass(frozen=True)
class _ResolvedKnowledgeManagerTarget:
    """Resolved binding plus stable manager key for one effective knowledge manager."""

    key: _KnowledgeManagerKey
    binding: ResolvedKnowledgeBinding


_shared_knowledge_managers: dict[str, KnowledgeManager] = {}
_shared_knowledge_manager_init_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_request_knowledge_manager_init_locks: weakref.WeakValueDictionary[_KnowledgeManagerKey, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _knowledge_manager_key_for_binding(
    base_id: str,
    binding: ResolvedKnowledgeBinding,
) -> _KnowledgeManagerKey:
    return _KnowledgeManagerKey(
        base_id=base_id,
        storage_path=str(binding.storage_root.resolve()),
        knowledge_path=str(binding.knowledge_path.resolve()),
    )


def _current_knowledge_manager_key(manager: KnowledgeManager) -> _KnowledgeManagerKey:
    storage_path = manager.storage_path
    knowledge_path = manager.knowledge_path
    if storage_path is None or knowledge_path is None:
        msg = f"Knowledge manager '{manager.base_id}' requires resolved storage_path and knowledge_path"
        raise ValueError(msg)
    return _KnowledgeManagerKey(
        base_id=manager.base_id,
        storage_path=str(storage_path.resolve()),
        knowledge_path=str(knowledge_path.resolve()),
    )


def _resolve_knowledge_manager_target(
    config: Config,
    runtime_paths: RuntimePaths,
    base_id: str,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    start_watchers: bool,
    create: bool = False,
) -> _ResolvedKnowledgeManagerTarget:
    binding = resolve_knowledge_binding(
        base_id,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        start_watchers=start_watchers,
        create=create,
    )
    if create:
        _ensure_knowledge_directory_ready(binding.knowledge_path)
    return _ResolvedKnowledgeManagerTarget(
        key=_knowledge_manager_key_for_binding(base_id, binding),
        binding=binding,
    )


async def _stop_and_remove_shared_manager(base_id: str) -> None:
    manager = _shared_knowledge_managers.pop(base_id, None)
    if manager is None:
        return
    await manager.stop_watcher()


def _shared_knowledge_manager_init_lock(base_id: str) -> asyncio.Lock:
    lock = _shared_knowledge_manager_init_locks.get(base_id)
    if lock is None:
        lock = asyncio.Lock()
        _shared_knowledge_manager_init_locks[base_id] = lock
    return lock


def _request_knowledge_manager_init_lock(key: _KnowledgeManagerKey) -> asyncio.Lock:
    lock = _request_knowledge_manager_init_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _request_knowledge_manager_init_locks[key] = lock
    return lock


def _shared_manager_matches_target(
    manager: KnowledgeManager,
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
) -> bool:
    binding = target.binding
    if binding.request_scoped:
        return False
    if _current_knowledge_manager_key(manager) != target.key:
        return False
    return manager.matches(config, binding.storage_root, binding.knowledge_path)


def _lookup_shared_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
) -> KnowledgeManager | None:
    manager = _shared_knowledge_managers.get(target.key.base_id)
    if manager is None:
        return None
    if not _shared_manager_matches_target(manager, target=target, config=config):
        return None
    return manager


def _shared_manager_runtime_mode(
    manager: KnowledgeManager,
    *,
    target: _ResolvedKnowledgeManagerTarget,
) -> Literal["watch", "git_sync", "on_access"]:
    if target.binding.start_background_watchers:
        return "watch"
    if manager._git_background_startup_enabled():
        return "git_sync"
    return "on_access"


def _task_is_running(task: asyncio.Task[object] | None) -> bool:
    return task is not None and not task.done()


def _shared_manager_has_background_runtime(manager: KnowledgeManager) -> bool:
    return _task_is_running(manager._watch_task) or _task_is_running(manager._git_sync_task)


def _shared_manager_background_runtime_mode(manager: KnowledgeManager) -> Literal["watch", "git_sync"] | None:
    if _task_is_running(manager._watch_task):
        return "watch"
    if _task_is_running(manager._git_sync_task):
        return "git_sync"
    return None


async def _start_shared_manager_runtime_mode(
    manager: KnowledgeManager,
    runtime_mode: Literal["watch", "git_sync"] | None,
) -> None:
    if runtime_mode == "watch":
        await manager.start_watcher()
    elif runtime_mode == "git_sync":
        await manager._start_git_sync()


async def _reconcile_shared_manager_runtime(
    manager: KnowledgeManager,
    *,
    target: _ResolvedKnowledgeManagerTarget,
) -> None:
    runtime_mode = _shared_manager_runtime_mode(manager, target=target)

    if runtime_mode != "watch" and manager._watch_task is not None:
        await manager.stop_watcher()
    elif runtime_mode == "on_access":
        await manager._stop_git_sync()

    if manager._git_background_startup_mode is not None and runtime_mode == "on_access":
        await manager.finish_pending_background_git_startup()
    elif target.binding.incremental_sync_on_access and runtime_mode == "on_access":
        await sync_manager_without_full_reindex(manager)

    if runtime_mode == "watch":
        await manager.start_watcher()
    elif runtime_mode == "git_sync":
        await manager._start_git_sync()


async def _create_knowledge_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    reindex_on_create: bool,
    initialize_on_create: bool = True,
    start_runtime: bool = True,
) -> KnowledgeManager:
    binding = target.binding
    manager = KnowledgeManager(
        base_id=target.key.base_id,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=binding.storage_root,
        knowledge_path=binding.knowledge_path,
        git_background_startup_allowed=not binding.request_scoped,
    )
    if not initialize_on_create:
        return manager

    init_result = await initialize_manager_for_startup(
        manager,
        reindex_on_create=reindex_on_create,
    )
    if init_result.deferred:
        logger.info(
            "Knowledge manager initialized with background git sync",
            **startup_log_context(
                base_id=target.key.base_id,
                knowledge_path=binding.knowledge_path,
                result=init_result,
            ),
        )
    elif init_result.startup_mode != "full_reindex":
        logger.info(
            "Knowledge manager initialized without full reindex",
            **startup_log_context(
                base_id=target.key.base_id,
                knowledge_path=binding.knowledge_path,
                result=init_result,
            ),
        )

    if start_runtime:
        desired_runtime_mode = _shared_manager_runtime_mode(manager, target=target)
        await _start_shared_manager_runtime_mode(
            manager,
            None if desired_runtime_mode == "on_access" else desired_runtime_mode,
        )
    return manager


async def _ensure_shared_knowledge_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    reindex_on_create: bool,
    reconcile_existing_runtime: bool,
    initialize_on_create: bool = True,
) -> KnowledgeManager:
    if target.binding.request_scoped:
        msg = f"Shared knowledge manager target '{target.key.base_id}' must not be request-scoped"
        raise ValueError(msg)

    async with _shared_knowledge_manager_init_lock(target.key.base_id):
        existing = _shared_knowledge_managers.get(target.key.base_id)
        if existing is not None:
            if existing.needs_full_reindex(
                config,
                target.binding.storage_root,
                target.binding.knowledge_path,
            ):
                preserved_runtime_mode = (
                    _shared_manager_background_runtime_mode(existing) if not reconcile_existing_runtime else None
                )
                await existing.stop_watcher()
                manager = await _create_knowledge_manager_for_target(
                    target=target,
                    config=config,
                    runtime_paths=runtime_paths,
                    reindex_on_create=True,
                    initialize_on_create=initialize_on_create,
                    start_runtime=reconcile_existing_runtime and initialize_on_create,
                )
                if initialize_on_create:
                    await _start_shared_manager_runtime_mode(manager, preserved_runtime_mode)
                else:
                    manager.defer_shared_runtime_restore(preserved_runtime_mode)
                _shared_knowledge_managers[target.key.base_id] = manager
                return manager

            existing._refresh_settings(
                config,
                runtime_paths,
                target.binding.storage_root,
                target.binding.knowledge_path,
            )
            if reconcile_existing_runtime:
                await _reconcile_shared_manager_runtime(existing, target=target)
            elif not _shared_manager_has_background_runtime(existing):
                if existing._git_background_startup_mode is not None:
                    await existing.finish_pending_background_git_startup()
                elif target.binding.incremental_sync_on_access:
                    await sync_manager_without_full_reindex(existing)
            return existing

        manager = await _create_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
            initialize_on_create=initialize_on_create,
            start_runtime=initialize_on_create,
        )
        _shared_knowledge_managers[target.key.base_id] = manager
        return manager


async def _create_request_knowledge_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    reindex_on_create: bool,
) -> KnowledgeManager:
    """Create one request-owned knowledge manager without registering it globally."""
    if not target.binding.request_scoped:
        msg = f"Request knowledge manager target '{target.key.base_id}' must be request-scoped"
        raise ValueError(msg)
    async with _request_knowledge_manager_init_lock(target.key):
        return await _create_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
        )


async def ensure_agent_knowledge_managers(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    start_watchers: bool = True,
    reindex_on_create: bool = False,
) -> dict[str, KnowledgeManager]:
    """Ensure knowledge managers exist for one agent in one execution scope."""
    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        return {}
    base_ids = config.get_agent_knowledge_base_ids(agent_name)
    if not base_ids:
        return {}

    managers: dict[str, KnowledgeManager] = {}
    for base_id in base_ids:
        target = _resolve_knowledge_manager_target(
            config,
            runtime_paths,
            base_id,
            execution_identity=execution_identity,
            start_watchers=start_watchers,
            create=True,
        )
        if target.binding.request_scoped:
            managers[base_id] = await _create_request_knowledge_manager_for_target(
                target=target,
                config=config,
                runtime_paths=runtime_paths,
                reindex_on_create=reindex_on_create,
            )
            continue

        managers[base_id] = await _ensure_shared_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
            reconcile_existing_runtime=False,
        )
    return managers


async def initialize_shared_knowledge_managers(
    config: Config,
    runtime_paths: RuntimePaths,
    start_watchers: bool = False,
    reindex_on_create: bool = True,
    reconcile_existing_runtime: bool = False,
) -> dict[str, KnowledgeManager]:
    """Initialize process-global shared knowledge managers for configured shared bases only."""
    configured_base_ids = set(config.knowledge_bases)
    managers: dict[str, KnowledgeManager] = {}

    for base_id in sorted(configured_base_ids):
        manager = await ensure_shared_knowledge_manager(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            start_watchers=start_watchers,
            reindex_on_create=reindex_on_create,
            reconcile_existing_runtime=reconcile_existing_runtime,
        )
        if manager is None:
            continue
        managers[base_id] = manager

    for base_id in [candidate for candidate in list(_shared_knowledge_managers) if candidate not in managers]:
        await _stop_and_remove_shared_manager(base_id)

    return managers


async def ensure_shared_knowledge_manager(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    start_watchers: bool = False,
    reindex_on_create: bool = True,
    reconcile_existing_runtime: bool = False,
    initialize_on_create: bool = True,
) -> KnowledgeManager | None:
    """Ensure one process-global shared knowledge manager exists for the given base."""
    target = _resolve_knowledge_manager_target(
        config,
        runtime_paths,
        base_id,
        start_watchers=start_watchers,
        create=True,
    )
    if target.binding.request_scoped:
        return None
    return await _ensure_shared_knowledge_manager_for_target(
        target=target,
        config=config,
        runtime_paths=runtime_paths,
        reindex_on_create=reindex_on_create,
        reconcile_existing_runtime=reconcile_existing_runtime,
        initialize_on_create=initialize_on_create,
    )


def _get_shared_knowledge_manager(base_id: str) -> KnowledgeManager | None:
    """Return the current shared knowledge manager for a base ID, if one exists."""
    return _shared_knowledge_managers.get(base_id)


def get_shared_knowledge_manager_for_config(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    candidate_manager: KnowledgeManager | None = None,
) -> KnowledgeManager | None:
    """Return the current shared manager for one config, treating stale candidates as cache misses."""
    try:
        target = _resolve_knowledge_manager_target(
            config,
            runtime_paths,
            base_id,
            start_watchers=False,
        )
    except ValueError:
        return None
    manager = candidate_manager
    if manager is not None and not _shared_manager_matches_target(manager, target=target, config=config):
        manager = None
    if manager is None:
        manager = _lookup_shared_manager_for_target(target=target, config=config)
    if manager is None:
        return None
    return manager


async def shutdown_shared_knowledge_managers() -> None:
    """Shutdown and clear all process-global shared knowledge managers."""
    for base_id in list(_shared_knowledge_managers):
        await _stop_and_remove_shared_manager(base_id)

    _shared_knowledge_manager_init_locks.clear()
    _request_knowledge_manager_init_locks.clear()
    _shared_knowledge_managers.clear()
