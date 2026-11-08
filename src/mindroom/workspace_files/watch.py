"""The changes in agents' workspaces, from one scanner per workspace however many streams follow it.

A follower gets the sorted workspace-relative paths that changed, at most every 150 ms. Left out are what Git,
editors, caches and atomic saves churn (``watchfiles``' default filter, ``.cache`` and MindRoom's temporary save
names) and the credentials ``operations.is_hidden`` names.

Code the agent runs writes everything below the root and can plant or swap links there at any moment, so the scanner
registers nothing by path: each scan opens every folder from the root without following a link, as the file routes
do, and compares what it finds with the scan before. It reports a path only from what such a scan saw, so nothing
outside the workspace is ever watched or reported, and a change between two scans is never lost.
"""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import watchfiles

from mindroom.path_confinement import open_directory_within_root
from mindroom.workspace_files.operations import ENTRY_KINDS, is_hidden

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

_INTERVAL_SECONDS = 0.15
# A scan rests at least this many times as long as it took, so following a large workspace costs a bounded share of
# one core; a small one is scanned every interval.
_REST_FACTOR = 9
# The folders and entries a scan takes in, breadth first, so a large tree leaves out its deepest ones.
_MAX_FOLDERS = 10_000
_MAX_ENTRIES = 100_000
# The workspace is the agent's HOME, so tools keep their caches, such as uv's, in its `.cache`.
_FILTER = watchfiles.DefaultFilter(
    ignore_dirs=(*watchfiles.DefaultFilter.ignore_dirs, ".cache"),
    ignore_entity_patterns=(*watchfiles.DefaultFilter.ignore_entity_patterns, r"^\.mindroom-[0-9a-f]{32}\.tmp$"),
)

# What a scan saw of one entry: its type, and for anything but a folder its size and modification time; and its inode,
# so a replaced entry counts as changed.
type _Seen = tuple[int, int, int, int]


@dataclass(eq=False)
class _Follower:
    """One stream's changes not sent yet; scans, at least ``_INTERVAL_SECONDS`` apart, add them."""

    pending: set[str] = field(default_factory=set)
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, paths: set[str]) -> None:
        self.pending |= paths
        self.ready.set()

    async def next(self) -> list[str]:
        """Wait for changes and return them sorted."""
        await self.ready.wait()
        self.ready.clear()
        paths, self.pending = sorted(self.pending), set()
        return paths


@dataclass(eq=False)
class _Watch:
    root: Path
    followers: set[_Follower] = field(default_factory=set)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # Set once the first scan is done, so whatever changes from then on is reported.
    following: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


# One watch per workspace root, on the API's event loop.
_watches: dict[Path, _Watch] = {}


@asynccontextmanager
async def workspace_changes(root: Path) -> AsyncIterator[Callable[[], Awaitable[list[str]]]]:
    """Follow the changes below ``root``, which may not exist yet; yield a call that waits for the next batch.

    It yields once the root has been scanned, so a change made after that is reported.
    """
    watch = _watches.get(root)
    if watch is None:
        watch = _watches[root] = _Watch(root)
        watch.task = asyncio.create_task(_run(watch))
    follower = _Follower()
    watch.followers.add(follower)
    try:
        await watch.following.wait()
        yield follower.next
    finally:
        watch.followers.discard(follower)
        if not watch.followers:
            watch.stop.set()
            del _watches[root]


def _scan(root: Path) -> dict[str, _Seen]:
    """Return what the workspace holds now by relative path, empty while the root is missing or a link."""
    seen: dict[str, _Seen] = {}
    with suppress(OSError), open_directory_within_root(root) as root_fd:
        folders = [PurePosixPath()]
        # The list grows while it is read.
        for folder in folders:
            with (
                suppress(OSError),
                open_directory_within_root(root_fd, str(folder)) as directory,
                os.scandir(directory) as items,
            ):
                for item in items:
                    relative = folder / item.name
                    # The folders above passed both checks, so the filter needs only the name.
                    if not _FILTER(watchfiles.Change.added, item.name) or is_hidden(relative):
                        continue
                    try:
                        status = item.stat(follow_symlinks=False)
                    except OSError:
                        # Gone since the folder was listed, as a temporary file often is.
                        continue
                    kind = stat.S_IFMT(status.st_mode)
                    if kind == stat.S_IFDIR:
                        seen[str(relative)] = (kind, 0, 0, status.st_ino)
                        if len(folders) < _MAX_FOLDERS:
                            folders.append(relative)
                    elif kind in ENTRY_KINDS:
                        seen[str(relative)] = (kind, status.st_size, status.st_mtime_ns, status.st_ino)
                    if len(seen) == _MAX_ENTRIES:
                        return seen
    return seen


def _changed(before: dict[str, _Seen], after: dict[str, _Seen]) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


async def _rest(watch: _Watch, seconds: float) -> None:
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await watch.stop.wait()


async def _run(watch: _Watch) -> None:
    loop = asyncio.get_running_loop()
    try:
        before = await asyncio.to_thread(_scan, watch.root)
    finally:
        # Followers wait for the first scan; one that fails must not keep them waiting.
        watch.following.set()
    while not watch.stop.is_set():
        started = loop.time()
        after = await asyncio.to_thread(_scan, watch.root)
        if paths := _changed(before, after):
            for follower in watch.followers:
                follower.add(paths)
        before = after
        await _rest(watch, max(_INTERVAL_SECONDS, (loop.time() - started) * _REST_FACTOR))
