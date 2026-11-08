"""Source-change scheduling for published knowledge index refreshes."""

from __future__ import annotations

import asyncio
import os
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from watchfiles import Change, DefaultFilter, awatch

from mindroom.knowledge.file_listing import include_semantic_knowledge_relative_path, pinned_canonical_directory
from mindroom.knowledge.legacy_chroma_storage import chroma_era_shared_base_ids
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    KnowledgeSourceRoot,
    mark_knowledge_source_changed_async,
    resolve_refresh_target,
    source_root_for_refresh_target,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler

logger = get_logger(__name__)

# Each new folder makes the watcher walk the base again, which takes time in proportion to its folders.
_MAX_WATCHED_FOLDERS = 10_000
# Watchfiles drops every change below these folders, so watching them would only use up watches.
_IGNORED_FOLDER_NAMES = frozenset(DefaultFilter.ignore_dirs)
_DEFAULT_FILTER = DefaultFilter()


@dataclass(frozen=True, slots=True)
class _WatchTarget:
    key: KnowledgeSourceRoot
    path: Path
    base_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PollTarget:
    key: KnowledgeRefreshTarget
    base_id: str
    poll_interval_seconds: float
    git: bool


@dataclass(slots=True)
class _WatchTask:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


def _ensure_watch_root(path: Path) -> None:
    if path.exists() and not path.is_dir():
        msg = f"Knowledge path {path} must be a directory"
        raise ValueError(msg)
    path.mkdir(parents=True, exist_ok=True)


def _skip_unusable_base(base_ids: list[str], exc: OSError | ValueError) -> None:
    logger.warning("Knowledge source not watched: its path is unusable", base_ids=base_ids, error=str(exc))


def _refresh_target_or_none(
    base_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> KnowledgeRefreshTarget | None:
    """Resolve one base's refresh target, or None when its path is unusable, so other bases still start.

    Worker code can replace a shared base's folder inside its own workspace with a link or a file.
    """
    try:
        return resolve_refresh_target(base_id, config=config, runtime_paths=runtime_paths, create=True)
    except (OSError, ValueError) as exc:
        _skip_unusable_base([base_id], exc)
        return None


def _shared_local_watch_targets(config: Config, runtime_paths: RuntimePaths) -> dict[KnowledgeSourceRoot, _WatchTarget]:
    targets_by_key: dict[KnowledgeSourceRoot, list[str]] = {}
    for base_id in sorted(config.knowledge_bases):
        base_config = config.get_knowledge_base_config(base_id)
        if base_config.mode != "semantic" or not base_config.watch or base_config.git is not None:
            continue
        if config.get_private_knowledge_base_agent(base_id) is not None:
            continue

        refresh_target = _refresh_target_or_none(base_id, config, runtime_paths)
        if refresh_target is None:
            continue
        targets_by_key.setdefault(source_root_for_refresh_target(refresh_target), []).append(base_id)

    targets: dict[KnowledgeSourceRoot, _WatchTarget] = {}
    for key, base_ids in targets_by_key.items():
        path = Path(key.knowledge_path)
        try:
            _ensure_watch_root(path)
        except (OSError, ValueError) as exc:
            _skip_unusable_base(base_ids, exc)
            continue
        targets[key] = _WatchTarget(key=key, path=path, base_ids=tuple(base_ids))
    return targets


def _shared_poll_targets(
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[KnowledgeRefreshTarget, _PollTarget]:
    targets: dict[KnowledgeRefreshTarget, _PollTarget] = {}
    for base_id in sorted(config.knowledge_bases):
        base_config = config.get_knowledge_base_config(base_id)
        poll_interval_seconds = base_config.effective_poll_interval_seconds
        if poll_interval_seconds is None:
            continue
        if config.get_private_knowledge_base_agent(base_id) is not None:
            continue

        refresh_target = _refresh_target_or_none(base_id, config, runtime_paths)
        if refresh_target is None:
            continue
        targets[refresh_target] = _PollTarget(
            key=refresh_target,
            base_id=base_id,
            poll_interval_seconds=float(poll_interval_seconds),
            git=base_config.git is not None,
        )
    return targets


def _watched_folders(root: Path) -> list[Path]:
    """Return the base folder and the folders below it, reached without a link, up to the cap."""
    folders: list[Path] = []
    with pinned_canonical_directory(root) as root_fd:
        for folder, subfolders, _files, _folder_fd in os.fwalk(".", dir_fd=root_fd):
            subfolders[:] = [name for name in subfolders if name not in _IGNORED_FOLDER_NAMES]
            folders.append(root / folder)
            if len(folders) == _MAX_WATCHED_FOLDERS:
                break
    return folders


def _is_new_watched_folder(change: Change, path: str) -> bool:
    changed = Path(path)
    return (
        change == Change.added and changed.name not in _IGNORED_FOLDER_NAMES and changed.is_dir(follow_symlinks=False)
    )


def _report_change(change: Change, path: str) -> bool:
    """Filter changes as watchfiles does by default, but report every new folder, since it must be watched too."""
    return _DEFAULT_FILTER(change, path) or _is_new_watched_folder(change, path)


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

    name = "knowledge_source_watchers"

    def __init__(self, refresh_scheduler: KnowledgeRefreshScheduler, runtime_paths: RuntimePaths) -> None:
        self._refresh_scheduler = refresh_scheduler
        self._runtime_paths = runtime_paths
        self._filesystem_tasks: dict[KnowledgeSourceRoot, _WatchTask] = {}
        self._poll_tasks: dict[KnowledgeRefreshTarget, _WatchTask] = {}

    async def sync(self, config: Config) -> None:
        """Replace watcher tasks so they match the current shared knowledge config, and start Chroma-era conversions."""
        await self.stop()
        runtime_paths = self._runtime_paths
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

        for poll_target in _shared_poll_targets(config, runtime_paths).values():
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                self._poll_source(poll_target, config=config, runtime_paths=runtime_paths, stop_event=stop_event),
            )
            self._poll_tasks[poll_target.key] = _WatchTask(stop_event=stop_event, task=task)
            logger.info(
                "Knowledge poller started",
                base_id=poll_target.base_id,
                poll_interval_seconds=poll_target.poll_interval_seconds,
                git=poll_target.git,
            )

        for base_id in chroma_era_shared_base_ids(config, runtime_paths):
            target = resolve_refresh_target(base_id, config=config, runtime_paths=runtime_paths)
            # A reload during a conversion must not queue the same conversion again.
            if not self._refresh_scheduler.is_refreshing(target):
                self._refresh_scheduler.schedule_refresh(base_id, config=config, runtime_paths=runtime_paths)

    async def stop(self) -> None:
        """Stop all source watchers owned by this instance."""
        tasks = list(self._filesystem_tasks.values())
        tasks.extend(self._poll_tasks.values())
        self._filesystem_tasks.clear()
        self._poll_tasks.clear()
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
        # Changes made while the watcher restarts go unseen, so the first batch after a restart refreshes the base.
        refresh_pending = False
        warned_cap = False
        try:
            while not stop_event.is_set():
                folders = await asyncio.to_thread(_watched_folders, target.path)
                if len(folders) == _MAX_WATCHED_FOLDERS and not warned_cap:
                    warned_cap = True
                    logger.warning(
                        "Knowledge filesystem watcher watches only some folders; changes in the others refresh on search",
                        knowledge_path=str(target.path),
                        base_ids=list(target.base_ids),
                        watched_folders=len(folders),
                    )
                # A recursive watch follows links out of the base, so each folder is watched alone.
                # Yielding on timeouts delivers a first batch within a second of the watch starting, even without a change.
                watcher = awatch(
                    *folders,
                    watch_filter=_report_change,
                    stop_event=stop_event,
                    rust_timeout=1_000,
                    yield_on_timeout=True,
                    recursive=False,
                )
                try:
                    async with aclosing(watcher) as batches:
                        async for changes in batches:
                            if any(_is_new_watched_folder(change, path) for change, path in changes):
                                refresh_pending = True
                                break
                            if refresh_pending or _changes_include_indexable_path(target, config, changes):
                                refresh_pending = False
                                await self._schedule_refresh_for_target(
                                    target,
                                    config=config,
                                    runtime_paths=runtime_paths,
                                )
                except FileNotFoundError:
                    # A folder disappeared between the walk and the watch, so walk again.
                    refresh_pending = True
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

    async def _poll_source(
        self,
        target: _PollTarget,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        stop_event: asyncio.Event,
    ) -> None:
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=target.poll_interval_seconds)
                    continue
                except TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                self._refresh_scheduler.schedule_refresh(
                    target.base_id,
                    config=config,
                    runtime_paths=runtime_paths,
                )
                logger.debug(
                    "Knowledge poller scheduled refresh",
                    base_id=target.base_id,
                    poll_interval_seconds=target.poll_interval_seconds,
                    git=target.git,
                )
        except Exception:
            logger.exception("Knowledge poller stopped after failure", base_id=target.base_id, git=target.git)
        finally:
            logger.info(
                "Knowledge poller stopped",
                base_id=target.base_id,
                poll_interval_seconds=target.poll_interval_seconds,
                git=target.git,
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
            changed_base_ids = await mark_knowledge_source_changed_async(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                reason="filesystem_watch",
            )
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
                )
