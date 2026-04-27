"""Filesystem watch scheduling for knowledge snapshot refreshes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from watchfiles import Change, awatch

from mindroom.knowledge.manager import include_semantic_knowledge_relative_path
from mindroom.knowledge.registry import (
    KnowledgeSourceKey,
    mark_source_dirty_async,
    resolve_refresh_key,
    source_key_for_refresh_key,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_owner import KnowledgeRefreshOwner

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _WatchTarget:
    key: KnowledgeSourceKey
    path: Path
    base_ids: tuple[str, ...]


@dataclass(slots=True)
class _WatchTask:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


def _ensure_watch_root(path: Path) -> None:
    if path.exists() and not path.is_dir():
        msg = f"Knowledge path {path} must be a directory"
        raise ValueError(msg)
    path.mkdir(parents=True, exist_ok=True)


def _shared_local_watch_targets(config: Config, runtime_paths: RuntimePaths) -> dict[KnowledgeSourceKey, _WatchTarget]:
    targets_by_key: dict[KnowledgeSourceKey, list[str]] = {}
    for base_id in sorted(config.knowledge_bases):
        base_config = config.get_knowledge_base_config(base_id)
        if not base_config.watch or base_config.git is not None:
            continue
        if config.get_private_knowledge_base_agent(base_id) is not None:
            continue

        refresh_key = resolve_refresh_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            create=True,
        )
        targets_by_key.setdefault(source_key_for_refresh_key(refresh_key), []).append(base_id)

    targets: dict[KnowledgeSourceKey, _WatchTarget] = {}
    for key, base_ids in targets_by_key.items():
        path = Path(key.knowledge_path)
        _ensure_watch_root(path)
        targets[key] = _WatchTarget(key=key, path=path, base_ids=tuple(base_ids))
    return targets


def _changed_path_is_indexable(target: _WatchTarget, config: Config, changed_path: Path) -> bool:
    try:
        relative_path = changed_path.relative_to(target.path)
    except ValueError:
        return False
    if not relative_path.parts:
        return False
    relative = relative_path.as_posix()
    return any(include_semantic_knowledge_relative_path(config, base_id, relative) for base_id in target.base_ids)


def _changes_include_indexable_path(
    target: _WatchTarget,
    config: Config,
    changes: set[tuple[Change, str]],
) -> bool:
    for change, changed_path in changes:
        if change not in {Change.added, Change.modified, Change.deleted}:
            continue
        if _changed_path_is_indexable(target, config, Path(changed_path)):
            return True
    return False


class KnowledgeFilesystemWatchOwner:
    """Own filesystem watchers that schedule atomic knowledge snapshot refreshes."""

    def __init__(self, refresh_owner: KnowledgeRefreshOwner) -> None:
        self._refresh_owner = refresh_owner
        self._tasks: dict[KnowledgeSourceKey, _WatchTask] = {}

    async def sync(self, *, config: Config | None, runtime_paths: RuntimePaths) -> None:
        """Replace watcher tasks so they match the current shared local knowledge config."""
        await self.shutdown()
        if config is None:
            return

        for target in _shared_local_watch_targets(config, runtime_paths).values():
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                self._watch_source(target, config=config, runtime_paths=runtime_paths, stop_event=stop_event),
            )
            self._tasks[target.key] = _WatchTask(stop_event=stop_event, task=task)
            logger.info(
                "Knowledge filesystem watcher started",
                knowledge_path=str(target.path),
                base_ids=list(target.base_ids),
            )

    async def shutdown(self) -> None:
        """Stop all filesystem watchers owned by this instance."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for watch_task in tasks:
            watch_task.stop_event.set()
            watch_task.task.cancel()
        for watch_task in tasks:
            with suppress(asyncio.CancelledError):
                await watch_task.task

    async def _watch_source(
        self,
        target: _WatchTarget,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        stop_event: asyncio.Event,
    ) -> None:
        try:
            async for changes in awatch(target.path, stop_event=stop_event):
                if not changes or not _changes_include_indexable_path(target, config, changes):
                    continue
                await self._schedule_refresh_for_target(target, config=config, runtime_paths=runtime_paths)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Knowledge filesystem watcher stopped after failure",
                knowledge_path=str(target.path),
                base_ids=list(target.base_ids),
            )
        finally:
            logger.info(
                "Knowledge filesystem watcher stopped",
                knowledge_path=str(target.path),
                base_ids=list(target.base_ids),
            )

    async def _schedule_refresh_for_target(
        self,
        target: _WatchTarget,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        scheduled_base_ids: set[str] = set()
        for base_id in target.base_ids:
            dirty_base_ids = await mark_source_dirty_async(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                reason="filesystem_watch",
            )
            for dirty_base_id in dirty_base_ids:
                if dirty_base_id in scheduled_base_ids:
                    continue
                dirty_config = config.get_knowledge_base_config(dirty_base_id)
                if not dirty_config.watch or dirty_config.git is not None:
                    continue
                scheduled_base_ids.add(dirty_base_id)
                self._refresh_owner.schedule_refresh(
                    dirty_base_id,
                    config=config,
                    runtime_paths=runtime_paths,
                )
