"""The changes in agents' workspaces, from one watcher per workspace however many streams follow it.

A follower gets the sorted workspace-relative paths that changed, at most every 150 ms. Left out are what Git,
editors, caches and atomic saves churn (``watchfiles``' default filter, ``.cache`` and MindRoom's temporary save
names) and the credentials ``operations.is_hidden`` names. Code the agent runs can plant links, and ``watchfiles``
would follow them out of the workspace, so the watcher watches each folder reached from the root without a link on its
own, walks the tree again when a folder appears while the old watch still runs, and reports only paths that stay
inside.
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
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection

logger = get_logger(__name__)

_INTERVAL_SECONDS = 0.15
_STEP_MS = 50
# A watch sends a change at most its debounce and a step after it happens, and drops what it holds when stopped, so a
# replaced watch runs on this long beside its successor.
_HANDOVER_SECONDS = _INTERVAL_SECONDS + 2 * _STEP_MS / 1000
# How long a watcher waits before looking again for a workspace not made yet, and after a failure.
_ABSENT_RETRY_SECONDS = 1.0
_FAILURE_RETRY_SECONDS = 30.0
# Each watched folder holds one of the primary's file watches, which the operating system limits.
_MAX_FOLDERS = 10_000
# The workspace is the agent's HOME, so tools keep their caches, such as uv's, in its `.cache`.
_FILTER = watchfiles.DefaultFilter(
    ignore_dirs=(*watchfiles.DefaultFilter.ignore_dirs, ".cache"),
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
    # Set once the root is watched, or found missing, so whatever changes from then on is reported.
    following: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


# One watch per workspace root, on the API's event loop.
_watches: dict[Path, _Watch] = {}


@asynccontextmanager
async def workspace_changes(root: Path) -> AsyncIterator[Callable[[], Awaitable[list[str]]]]:
    """Follow the changes below ``root``, which may not exist yet; yield a call that waits for the next batch.

    It yields once the root is watched, so a change made after that is reported.
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


def _folders(root: Path) -> list[str]:
    """Return the root and the folders below it reached without a link, breadth first so the cap leaves out the deepest.

    Folders the filter ignores, or that hold credentials, are left out.
    """
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return []
    except OSError:
        return []
    folders = [str(root)]
    # The list grows while it is read.
    for folder in folders:
        with suppress(OSError), os.scandir(folder) as entries:
            folders.extend(
                entry.path for entry in entries if entry.is_dir(follow_symlinks=False) and _walked(root, entry.path)
            )
        if len(folders) >= _MAX_FOLDERS:
            logger.warning("Workspace has too many folders to follow them all", root=str(root), limit=_MAX_FOLDERS)
            return folders[:_MAX_FOLDERS]
    return folders


def _walked(root: Path, folder: str) -> bool:
    """Return whether a folder below the root is one the walk takes in: not ignored and holding no credentials."""
    relative = PurePosixPath(Path(folder).relative_to(root))
    return _FILTER(watchfiles.Change.added, str(relative)) and not is_hidden(relative)


def _new_folder(root: Path, raw: str) -> bool:
    """Return whether a path is a folder, not a link, that the walk would take in."""
    path = Path(raw)
    return path.is_dir() and not path.is_symlink() and _walked(root, raw)


def _reported(root: Path, change: watchfiles.Change, raw: str) -> str | None:
    """Return a changed path relative to the root, or None for the root, what is left out, or a path through a link.

    The filter sees only the relative path, never the folders the root sits in.
    """
    relative = PurePosixPath(Path(raw).relative_to(root))
    if not relative.parts or not _FILTER(change, str(relative)) or is_hidden(relative):
        return None
    try:
        # A link is an entry of its folder, wherever it leads; the folders above it must be no links.
        resolve_path_within_root(root, str(relative), symlinks="preserve_leaf")
    except (OSError, ValueError):
        return None
    return str(relative)


def _batch(root: Path, changes: set[tuple[watchfiles.Change, str]]) -> tuple[set[str], bool]:
    """Return a batch's reported paths, and whether a folder appeared, so the tree needs walking again."""
    paths = {path for change, raw in changes if (path := _reported(root, change, raw)) is not None}
    grown = any(change is watchfiles.Change.added and _new_folder(root, raw) for change, raw in changes)
    return paths, grown


async def _wait(watch: _Watch, seconds: float) -> None:
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await watch.stop.wait()


def _send(watch: _Watch, paths: set[str]) -> None:
    if paths:
        for follower in watch.followers:
            follower.add(paths)


def _held(root: Path, folders: Collection[str]) -> set[str]:
    """Return the reported paths of what ``folders`` hold now."""
    paths: set[str] = set()
    for folder in folders:
        with suppress(OSError):
            paths |= {
                path
                for entry in Path(folder).iterdir()
                if (path := _reported(root, watchfiles.Change.added, str(entry))) is not None
            }
    return paths


async def _follow(watch: _Watch, folders: list[str], stop: asyncio.Event, grown: asyncio.Event) -> None:
    """Report what changes in ``folders`` until ``stop``, setting ``grown`` once a folder appears in one of them."""
    try:
        async for changes in watchfiles.awatch(
            *folders,
            watch_filter=None,
            debounce=int(_INTERVAL_SECONDS * 1000),
            step=_STEP_MS,
            stop_event=stop,
            recursive=False,
            ignore_permission_denied=True,
        ):
            paths, appeared = await asyncio.to_thread(_batch, watch.root, changes)
            _send(watch, paths)
            if appeared:
                grown.set()
    except Exception:
        logger.exception("Workspace watcher failed; retrying", root=str(watch.root))


async def _run(watch: _Watch) -> None:
    # The folders watched last, empty while the root is missing, None before the first watch or after a failure.
    watched: list[str] | None = None
    stop_watching = asyncio.Event()
    # Set by any watch, old or new, once a folder appears after the walk began, or by the walk that checks the watch,
    # so the tree is walked again.
    grown = asyncio.Event()
    # A replaced watch keeps running until it sees its stop, so it is held until then.
    running: set[asyncio.Task[None]] = set()
    try:
        while not watch.stop.is_set():
            grown.clear()
            folders = await asyncio.to_thread(_folders, watch.root)
            if not folders:
                stop_watching.set()
                watched = []
                watch.following.set()
                await _wait(watch, _ABSENT_RETRY_SECONDS)
                continue
            previous, stop_watching = stop_watching, asyncio.Event()
            following = asyncio.create_task(_follow(watch, folders, stop_watching, grown))
            running.add(following)
            following.add_done_callback(running.discard)
            # The new watch takes in its folders as its task starts; the old one sends what it still holds first.
            asyncio.get_running_loop().call_later(_HANDOVER_SECONDS, previous.set)
            await asyncio.sleep(0)
            watch.following.set()
            if watched is not None:
                # What a new folder, or a root that was missing, held before its watch began.
                _send(watch, await asyncio.to_thread(_held, watch.root, set(folders).difference(watched)))
            watched = folders
            # A folder made between the walk and the watch went unwatched: once watched, the tree is walked again.
            if set(await asyncio.to_thread(_folders, watch.root)).difference(folders):
                grown.set()
            waits = [asyncio.create_task(grown.wait()), asyncio.create_task(watch.stop.wait())]
            await asyncio.wait([following, *waits], return_when=asyncio.FIRST_COMPLETED)
            for waiting in waits:
                waiting.cancel()
            if following.done() and not watch.stop.is_set():
                watched = None
                await _wait(watch, _FAILURE_RETRY_SECONDS)
    finally:
        stop_watching.set()
        watch.following.set()
