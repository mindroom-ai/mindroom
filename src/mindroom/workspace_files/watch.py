"""The changes in agents' workspaces, from one watcher per workspace however many streams follow it.

A follower gets the sorted workspace-relative paths that changed, at most every 150 ms. Left out are what Git,
editors, caches and atomic saves churn (``watchfiles``' default filter and MindRoom's temporary save names) and the
credentials ``operations.is_hidden`` names. Code the agent runs can plant links, and ``watchfiles`` would follow them
out of the workspace, so the watcher watches each folder reached from the root without a link on its own, walks the
tree again when a folder appears or goes, and reports only paths that stay inside.
"""

from __future__ import annotations

import asyncio
import math
import os
import stat
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import watchfiles

from mindroom.logging_config import get_logger
from mindroom.path_confinement import resolve_path_within_root
from mindroom.workspace_files.operations import is_hidden

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = get_logger(__name__)

_INTERVAL_SECONDS = 0.15
# How long a watcher waits before looking again for a workspace not made yet, and after a failure.
_ABSENT_RETRY_SECONDS = 1.0
_FAILURE_RETRY_SECONDS = 30.0
# Each watched folder holds one of the primary's file watches, which the operating system limits.
_MAX_FOLDERS = 10_000
_FILTER = watchfiles.DefaultFilter(
    ignore_entity_patterns=(*watchfiles.DefaultFilter.ignore_entity_patterns, r"^\.mindroom-[0-9a-f]{32}\.tmp$"),
)


@dataclass(eq=False)
class _Follower:
    """One stream's changes not sent yet."""

    pending: set[str] = field(default_factory=set)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    sent_at: float = -math.inf

    def add(self, paths: set[str]) -> None:
        self.pending |= paths
        self.ready.set()

    async def next(self) -> list[str]:
        """Wait for changes, at least ``_INTERVAL_SECONDS`` after the last batch, and return them sorted."""
        loop = asyncio.get_running_loop()
        await self.ready.wait()
        await asyncio.sleep(max(0.0, self.sent_at + _INTERVAL_SECONDS - loop.time()))
        self.ready.clear()
        paths, self.pending = sorted(self.pending), set()
        self.sent_at = loop.time()
        return paths


@dataclass(eq=False)
class _Watch:
    root: Path
    followers: set[_Follower] = field(default_factory=set)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


# One watch per workspace root, on the API's event loop.
_watches: dict[Path, _Watch] = {}


@asynccontextmanager
async def workspace_changes(root: Path) -> AsyncIterator[Callable[[], Awaitable[list[str]]]]:
    """Follow the changes below ``root``, which may not exist yet; yield a call that waits for the next batch."""
    watch = _watches.get(root)
    if watch is None:
        watch = _watches[root] = _Watch(root)
        watch.task = asyncio.create_task(_run(watch))
    follower = _Follower()
    watch.followers.add(follower)
    try:
        yield follower.next
    finally:
        watch.followers.discard(follower)
        if not watch.followers:
            watch.stop.set()
            del _watches[root]


def _folders(root: Path) -> list[str]:
    """Return the root and the folders below it reached without a link, leaving out those the filter ignores."""
    with suppress(OSError):
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return []
    folders: list[str] = []
    for current, names, _ in os.walk(root):
        if len(folders) == _MAX_FOLDERS:
            logger.warning("Workspace has too many folders to follow them all", root=str(root), limit=_MAX_FOLDERS)
            break
        folders.append(current)
        names[:] = [name for name in names if name not in _FILTER.ignore_dirs]
    return folders


def _reported(root: Path, raw: str) -> str | None:
    """Return a changed path relative to the root, or None when it is the root, hidden, or reached through a link out."""
    relative = PurePosixPath(Path(raw).relative_to(root))
    if not relative.parts or is_hidden(relative):
        return None
    try:
        resolve_path_within_root(root, str(relative), symlinks="internal")
    except (OSError, ValueError):
        return None
    return str(relative)


def _batch(root: Path, changes: set[tuple[watchfiles.Change, str]]) -> tuple[set[str], bool]:
    """Return a batch's reported paths, and whether a folder appeared, so the tree needs walking again."""
    paths = {path for _, raw in changes if (path := _reported(root, raw)) is not None}
    grown = any(
        change is watchfiles.Change.added and Path(raw).is_dir() and not Path(raw).is_symlink()
        for change, raw in changes
    )
    return paths, grown


async def _wait(watch: _Watch, seconds: float) -> None:
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await watch.stop.wait()


async def _run(watch: _Watch) -> None:
    while not watch.stop.is_set():
        folders = await asyncio.to_thread(_folders, watch.root)
        if not folders:
            await _wait(watch, _ABSENT_RETRY_SECONDS)
            continue
        try:
            async for changes in watchfiles.awatch(
                *folders,
                watch_filter=_FILTER,
                debounce=int(_INTERVAL_SECONDS * 1000),
                stop_event=watch.stop,
                recursive=False,
                ignore_permission_denied=True,
            ):
                paths, grown = await asyncio.to_thread(_batch, watch.root, changes)
                if paths:
                    for follower in watch.followers:
                        follower.add(paths)
                if grown:
                    break
        except Exception:
            logger.exception("Workspace watcher failed; retrying", root=str(watch.root))
            await _wait(watch, _FAILURE_RETRY_SECONDS)
