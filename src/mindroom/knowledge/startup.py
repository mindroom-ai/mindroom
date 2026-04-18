"""Startup orchestration helpers for knowledge managers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.knowledge.manager import KnowledgeManager


@dataclass(frozen=True)
class StartupInitializationResult:
    """Normalized result for one startup path."""

    startup_mode: Literal["full_reindex", "resume", "incremental"]
    git: dict[str, Any] | None
    index: dict[str, int] | None
    deferred: bool = False


async def sync_manager_without_full_reindex(
    manager: KnowledgeManager,
) -> dict[str, Any]:
    """Run the non-resume incremental startup path."""
    if manager._git_config() is not None:
        return {"git": await manager.sync_git_repository(), "index": None}
    return {"git": None, "index": await manager.sync_indexed_files()}


async def resume_manager_without_full_reindex(
    manager: KnowledgeManager,
) -> dict[str, Any]:
    """Resume a partially initialized startup without a full rebuild."""
    if manager._git_config() is not None:
        git_result = await manager.sync_git_repository(index_changes=False)
        sync_result = await manager.sync_indexed_files()
        manager._mark_git_initial_sync_complete()
        return {"git": git_result, "index": sync_result}
    return {"git": None, "index": await manager.sync_indexed_files()}


async def initialize_manager_for_startup(
    manager: KnowledgeManager,
    *,
    reindex_on_create: bool,
) -> StartupInitializationResult:
    """Initialize one manager according to its current startup mode."""
    startup_mode: Literal["full_reindex", "resume", "incremental"] = (
        "full_reindex" if reindex_on_create else manager._startup_index_mode()
    )
    if startup_mode != "full_reindex" and manager._git_background_startup_enabled():
        deferred_result = await manager.prepare_background_git_startup(startup_mode)
        return StartupInitializationResult(
            startup_mode=startup_mode,
            git=None,
            index={
                "loaded_count": int(deferred_result["loaded_count"]),
                "indexed_count": int(deferred_result["indexed_count"]),
                "removed_count": int(deferred_result["removed_count"]),
            },
            deferred=True,
        )

    if startup_mode == "full_reindex":
        await manager.initialize()
        return StartupInitializationResult(startup_mode=startup_mode, git=None, index=None)

    sync_result = (
        await resume_manager_without_full_reindex(manager)
        if startup_mode == "resume"
        else await sync_manager_without_full_reindex(manager)
    )
    await asyncio.to_thread(manager._save_persisted_indexing_settings)
    return StartupInitializationResult(
        startup_mode=startup_mode,
        git=sync_result["git"],
        index=sync_result["index"],
    )


def startup_log_context(
    *,
    base_id: str,
    knowledge_path: Path,
    result: StartupInitializationResult,
) -> dict[str, object]:
    """Build a stable log payload for startup initialization."""
    context: dict[str, object] = {
        "base_id": base_id,
        "path": str(knowledge_path),
        "startup_mode": result.startup_mode,
    }
    if result.git is not None:
        context.update(
            {
                "git_updated": result.git["updated"],
                "git_changed_count": result.git["changed_count"],
                "git_removed_count": result.git["removed_count"],
            },
        )
    if result.index is not None:
        context.update(result.index)
    return context
