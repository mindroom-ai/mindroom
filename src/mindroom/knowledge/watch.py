"""Source-change scheduling for published knowledge index refreshes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from watchfiles import Change, awatch

from mindroom.knowledge.manager import include_semantic_knowledge_relative_path
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    KnowledgeSourceRoot,
    mark_knowledge_source_changed_async,
    resolve_refresh_targets,
    source_root_for_refresh_target,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _WatchBase:
    base_id: str
    execution_identity: ToolExecutionIdentity | None


@dataclass(frozen=True, slots=True)
class _WatchTarget:
    key: KnowledgeSourceRoot
    path: Path
    bases: tuple[_WatchBase, ...]

    @property
    def base_ids(self) -> tuple[str, ...]:
        return tuple(base.base_id for base in self.bases)


@dataclass(frozen=True, slots=True)
class _GitPollTarget:
    key: KnowledgeRefreshTarget
    base_id: str
    poll_interval_seconds: float
    execution_identity: ToolExecutionIdentity | None


@dataclass(slots=True)
class _WatchTask:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


def _ensure_watch_root(path: Path) -> None:
    if path.exists() and not path.is_dir():
        msg = f"Knowledge path {path} must be a directory"
        raise ValueError(msg)
    path.mkdir(parents=True, exist_ok=True)


def _shared_local_watch_targets(config: Config, runtime_paths: RuntimePaths) -> dict[KnowledgeSourceRoot, _WatchTarget]:
    targets_by_key: dict[KnowledgeSourceRoot, list[_WatchBase]] = {}
    for base_id in sorted(config.knowledge_bases):
        base_config = config.get_knowledge_base_config(base_id)
        if base_config.mode != "semantic" or not base_config.watch or base_config.git is not None:
            continue
        if config.get_private_knowledge_base_agent(base_id) is not None:
            continue

        for refresh_target in resolve_refresh_targets(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            create=True,
        ):
            key = source_root_for_refresh_target(refresh_target.key)
            targets_by_key.setdefault(key, []).append(
                _WatchBase(base_id=base_id, execution_identity=refresh_target.execution_identity),
            )

    targets: dict[KnowledgeSourceRoot, _WatchTarget] = {}
    for key, bases in targets_by_key.items():
        path = Path(key.knowledge_path)
        _ensure_watch_root(path)
        targets[key] = _WatchTarget(
            key=key,
            path=path,
            bases=tuple(bases),
        )
    return targets


def _shared_git_poll_targets(
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[KnowledgeRefreshTarget, _GitPollTarget]:
    targets: dict[KnowledgeRefreshTarget, _GitPollTarget] = {}
    for base_id in sorted(config.knowledge_bases):
        base_config = config.get_knowledge_base_config(base_id)
        if base_config.git is None:
            continue
        if config.get_private_knowledge_base_agent(base_id) is not None:
            continue

        for refresh_target in resolve_refresh_targets(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            create=True,
        ):
            targets[refresh_target.key] = _GitPollTarget(
                key=refresh_target.key,
                base_id=base_id,
                poll_interval_seconds=float(base_config.git.poll_interval_seconds),
                execution_identity=refresh_target.execution_identity,
            )
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


class KnowledgeSourceWatcher:
    """Own source watchers that schedule atomic published index refreshes."""

    def __init__(self, refresh_scheduler: KnowledgeRefreshScheduler) -> None:
        self._refresh_scheduler = refresh_scheduler
        self._filesystem_tasks: dict[KnowledgeSourceRoot, _WatchTask] = {}
        self._git_poll_tasks: dict[KnowledgeRefreshTarget, _WatchTask] = {}

    async def sync(self, *, config: Config | None, runtime_paths: RuntimePaths) -> None:
        """Replace watcher tasks so they match the current shared knowledge config."""
        await self.shutdown()
        if config is None:
            return

        for watch_target in _shared_local_watch_targets(config, runtime_paths).values():
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                self._watch_source(watch_target, config=config, runtime_paths=runtime_paths, stop_event=stop_event),
            )
            self._filesystem_tasks[watch_target.key] = _WatchTask(stop_event=stop_event, task=task)
            logger.info(
                "Knowledge filesystem watcher started",
                knowledge_path=str(watch_target.path),
                base_ids=list(watch_target.base_ids),
            )

        for poll_target in _shared_git_poll_targets(config, runtime_paths).values():
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                self._poll_git_source(poll_target, config=config, runtime_paths=runtime_paths, stop_event=stop_event),
            )
            self._git_poll_tasks[poll_target.key] = _WatchTask(stop_event=stop_event, task=task)
            logger.info(
                "Knowledge Git poller started",
                base_id=poll_target.base_id,
                poll_interval_seconds=poll_target.poll_interval_seconds,
            )

    async def shutdown(self) -> None:
        """Stop all source watchers owned by this instance."""
        tasks = list(self._filesystem_tasks.values())
        tasks.extend(self._git_poll_tasks.values())
        self._filesystem_tasks.clear()
        self._git_poll_tasks.clear()
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

    async def _poll_git_source(
        self,
        target: _GitPollTarget,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        stop_event: asyncio.Event,
    ) -> None:
        try:
            while not stop_event.is_set():
                self._refresh_scheduler.schedule_refresh(
                    target.base_id,
                    config=config,
                    runtime_paths=runtime_paths,
                    execution_identity=target.execution_identity,
                )
                logger.debug(
                    "Knowledge Git poller scheduled refresh",
                    base_id=target.base_id,
                    poll_interval_seconds=target.poll_interval_seconds,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=target.poll_interval_seconds)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Knowledge Git poller stopped after failure", base_id=target.base_id)
        finally:
            logger.info("Knowledge Git poller stopped", base_id=target.base_id)

    async def _schedule_refresh_for_target(
        self,
        target: _WatchTarget,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        scheduled_base_ids: set[str] = set()
        for watch_base in target.bases:
            try:
                changed_base_ids = await mark_knowledge_source_changed_async(
                    watch_base.base_id,
                    config=config,
                    runtime_paths=runtime_paths,
                    execution_identity=watch_base.execution_identity,
                    reason="filesystem_watch",
                )
            except Exception:
                logger.exception(
                    "Skipping unresolved knowledge source change mark",
                    base_id=watch_base.base_id,
                    knowledge_path=str(target.path),
                )
                continue
            for changed_base_id in changed_base_ids:
                if changed_base_id in scheduled_base_ids:
                    continue
                changed_config = config.get_knowledge_base_config(changed_base_id)
                if not changed_config.watch or changed_config.git is not None:
                    continue
                scheduled_base_ids.add(changed_base_id)
                self._refresh_scheduler.schedule_refresh(
                    changed_base_id,
                    config=config,
                    runtime_paths=runtime_paths,
                    execution_identity=watch_base.execution_identity,
                )
