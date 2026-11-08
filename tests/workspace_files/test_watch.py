"""A workspace's changes, as its change streams receive them from one shared watcher."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import watchfiles

from mindroom.workspace_files import watch
from mindroom.workspace_files.watch import workspace_changes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


async def _until(next_changes: Callable[[], Awaitable[list[str]]], *paths: str) -> set[str]:
    """Collect every reported path until all of ``paths`` show, failing after five seconds."""
    seen: set[str] = set()
    async with asyncio.timeout(5):
        while not seen.issuperset(paths):
            seen.update(await next_changes())
    return seen


async def _settled(root: Path) -> None:
    """Wait until the watcher follows ``root``: a probe file written now shows."""
    async with workspace_changes(root) as next_changes:
        for attempt in range(50):
            (root / f"probe{attempt}").write_text("x")
            try:
                async with asyncio.timeout(0.1):
                    if await next_changes():
                        return
            except TimeoutError:
                continue


@pytest.mark.asyncio
async def test_a_stream_hears_what_changes_but_never_git_credentials_or_save_temporaries(tmp_path: Path) -> None:
    """Changed paths come relative to the root and sorted, without Git's, credentials, caches or atomic save names."""
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / ".git" / "index").write_text("x")
        (root / ".env").write_text("TOKEN=1\n")
        (root / ".mindroom-0123456789abcdef0123456789abcdef.tmp").write_text("x")
        (root / ".cache" / "uv").mkdir(parents=True)
        (root / ".cache" / "uv" / "wheel").write_text("x")
        (root / "src" / "b.py").write_text("x")
        (root / "src" / "a.py").write_text("x")
        (root / "zz.md").write_text("x")

        seen = await _until(next_changes, "zz.md")

    assert {"src/a.py", "src/b.py", "zz.md"} <= seen
    assert not {path for path in seen if path.startswith((".git", ".env", ".mindroom-", ".cache"))}


@pytest.mark.asyncio
async def test_each_batch_is_sorted(tmp_path: Path) -> None:
    """One batch lists its paths in order."""
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        for name in ("c.md", "a.md", "b.md"):
            (root / name).write_text("x")
        await asyncio.sleep(0.4)

        batch = await next_changes()

    assert batch == sorted(batch)
    assert {"a.md", "b.md", "c.md"} <= set(batch)


@pytest.mark.asyncio
async def test_changes_beyond_a_link_out_of_the_workspace_are_never_followed(tmp_path: Path) -> None:
    """A link the agent plants to a folder outside reports nothing of what changes there."""
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "out").symlink_to(outside)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (outside / "secret.txt").write_text("x")
        (root / "inside.txt").write_text("x")

        seen = await _until(next_changes, "inside.txt")
        await asyncio.sleep(0.4)
        (root / "later.txt").write_text("x")
        seen |= await _until(next_changes, "later.txt")

    assert not {path for path in seen if path.startswith("out")}


@pytest.mark.asyncio
async def test_a_folder_made_while_following_is_followed_too(tmp_path: Path) -> None:
    """A new folder is watched too, so a file written in it shows."""
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "new").mkdir()
        await _until(next_changes, "new")
        for attempt in range(25):
            (root / "new" / f"file{attempt}.txt").write_text("x")
            try:
                async with asyncio.timeout(0.2):
                    if any(path.startswith("new/") for path in await next_changes()):
                        return
            except TimeoutError:
                continue
    pytest.fail("A file in a folder made while following was never reported.")


@pytest.mark.asyncio
async def test_a_workspace_made_after_following_starts_is_followed(tmp_path: Path) -> None:
    """Before the agent's first turn the root does not exist; once it does, its changes come through."""
    root = tmp_path / "workspace"
    async with workspace_changes(root) as next_changes:
        # The watcher finds no workspace first, and looks again.
        await asyncio.sleep(0.3)
        root.mkdir()
        await _settled(root)
        (root / "first.md").write_text("x")

        assert "first.md" in await _until(next_changes, "first.md")


@pytest.mark.asyncio
async def test_streams_of_one_workspace_share_one_watcher_until_the_last_leaves(tmp_path: Path) -> None:
    """However many streams follow a root, one watcher runs for it, and it stops once none follows."""
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace_changes(root) as first, workspace_changes(root) as second:
        assert list(watch._watches) == [root]
        await _settled(root)
        (root / "shared.md").write_text("x")

        assert "shared.md" in await _until(first, "shared.md")
        assert "shared.md" in await _until(second, "shared.md")
    assert watch._watches == {}


@pytest.mark.asyncio
async def test_batches_come_at_most_every_150_ms(tmp_path: Path) -> None:
    """Changes made in a quick run arrive in batches at least 150 ms apart."""
    root = tmp_path / "workspace"
    root.mkdir()
    loop = asyncio.get_running_loop()
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        times = []
        for attempt in range(4):
            (root / f"burst{attempt}.md").write_text("x")
            await next_changes()
            times.append(loop.time())

    assert all(later - earlier >= 0.149 for earlier, later in itertools.pairwise(times))


async def _silent(next_changes: Callable[[], Awaitable[list[str]]], seconds: float = 1.0) -> list[str]:
    """Return what arrives within ``seconds``, an empty list when nothing does."""
    try:
        async with asyncio.timeout(seconds):
            return await next_changes()
    except TimeoutError:
        return []


@pytest.mark.asyncio
async def test_a_workspace_replaced_by_a_link_is_not_followed(tmp_path: Path) -> None:
    """Code the agent runs could swap its workspace for a link elsewhere; the watcher never watches through it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "workspace"
    root.symlink_to(elsewhere)
    async with workspace_changes(root) as next_changes:
        await asyncio.sleep(0.5)
        (elsewhere / "secret.txt").write_text("x")

        assert await _silent(next_changes) == []


@pytest.mark.asyncio
async def test_a_folder_moved_out_of_the_workspace_is_no_longer_followed(tmp_path: Path) -> None:
    """Once a folder leaves the workspace, what changes in it is not reported under its old name."""
    root = tmp_path / "workspace"
    (root / "box").mkdir(parents=True)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "box").rename(tmp_path / "box")
        await _until(next_changes, "box")
        await asyncio.sleep(0.5)
        (tmp_path / "box" / "later.txt").write_text("x")
        (root / "marker.txt").write_text("x")

        seen = await _until(next_changes, "marker.txt")

    assert "box/later.txt" not in seen


@pytest.mark.asyncio
async def test_folders_past_the_cap_are_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each folder costs one of the system's file watches, so at most the cap's folders are watched."""
    monkeypatch.setattr(watch, "_MAX_FOLDERS", 1)
    root = tmp_path / "workspace"
    (root / "deep").mkdir(parents=True)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "deep" / "inner.txt").write_text("x")
        (root / "top.txt").write_text("x")

        seen = await _until(next_changes, "top.txt")
        seen |= set(await _silent(next_changes, 0.5))

    assert "deep/inner.txt" not in seen


def test_a_path_reached_through_a_link_out_is_never_reported(tmp_path: Path) -> None:
    """A folder swapped for a link while the watcher walked could be watched; its changes still never come through."""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (root / "out").symlink_to(tmp_path / "outside")

    assert watch._reported(root, watchfiles.Change.added, str(root / "out" / "secret.txt")) is None
    assert watch._reported(root, watchfiles.Change.added, str(root / "inside.txt")) == "inside.txt"


@pytest.mark.asyncio
async def test_nothing_goes_unreported_while_a_new_folder_is_taken_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the tree is walked again for a new folder, the old watch still reports, and the new folder's content shows."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "old.md").write_text("x")
    walked = threading.Event()
    real_folders = watch._folders

    def slow_folders(path: Path) -> list[str]:
        folders = real_folders(path)
        if "new" in {Path(folder).name for folder in folders}:
            walked.set()
            time.sleep(0.5)
        return folders

    monkeypatch.setattr(watch, "_folders", slow_folders)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "new").mkdir()
        await _until(next_changes, "new")
        assert await asyncio.to_thread(walked.wait, 5)
        (root / "old.md").write_text("changed while walking")
        (root / "new" / "inner.md").write_text("written before its folder is watched")

        seen = await _until(next_changes, "old.md", "new/inner.md")

    assert {"old.md", "new/inner.md"} <= seen


@pytest.mark.asyncio
async def test_a_workspace_stored_below_a_folder_named_like_a_cache_is_followed(tmp_path: Path) -> None:
    """Only names inside the workspace are filtered, not the folders MindRoom's storage sits in."""
    root = tmp_path / ".cache" / "node_modules" / "workspace"
    root.mkdir(parents=True)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "notes.md").write_text("x")

        assert "notes.md" in await _until(next_changes, "notes.md")


@pytest.mark.asyncio
async def test_a_follower_hears_every_change_made_once_it_follows(tmp_path: Path) -> None:
    """Following begins once the workspace is watched, so a change made right away is never missed."""
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    async with workspace_changes(root) as next_changes:
        (root / "src" / "first.md").write_text("x")

        assert "src/first.md" in await _until(next_changes, "src/first.md")


@pytest.mark.asyncio
async def test_what_a_workspace_holds_when_it_appears_is_reported(tmp_path: Path) -> None:
    """A workspace made after following began reports what it was made with, as the agent's first turn writes it."""
    root = tmp_path / "workspace"
    async with workspace_changes(root) as next_changes:
        (root / "memory").mkdir(parents=True)
        (root / "memory" / "MEMORY.md").write_text("x")

        assert "memory/MEMORY.md" in await _until(next_changes, "memory/MEMORY.md")


def test_the_folder_cap_leaves_out_the_deepest_folders_and_hidden_ones_are_never_watched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool's deep cache in the agent's HOME can't take the watches the project's own top folders need.

    Ignored folders, those holding credentials, and links are never watched.
    """
    root = tmp_path / "workspace"
    for top in ("a", "b", "c"):
        (root / top / "deep" / "deeper").mkdir(parents=True)
    (root / ".ssh").mkdir()
    (root / ".config" / "gh").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "out").symlink_to(tmp_path / "outside")
    monkeypatch.setattr(watch, "_MAX_FOLDERS", 5)

    folders = {Path(folder).relative_to(root).as_posix() for folder in watch._folders(root)}

    assert folders == {".", "a", "b", "c", ".config"}


@pytest.mark.asyncio
async def test_a_change_just_before_the_new_watch_begins_is_still_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old watch, which holds a change it hasn't sent yet, keeps running until it has sent it."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "old.md").write_text("x")
    real_folders = watch._folders

    def folders_then_a_change(path: Path) -> list[str]:
        folders = real_folders(path)
        if "new" in {Path(folder).name for folder in folders}:
            (root / "old.md").write_text("changed as the walk ends")
        return folders

    monkeypatch.setattr(watch, "_folders", folders_then_a_change)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "new").mkdir()
        await _until(next_changes, "new")

        assert "old.md" in await _until(next_changes, "old.md")


@pytest.mark.asyncio
async def test_a_folder_made_after_the_walk_is_watched_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder that appears once the tree was walked, in a watched folder or a new one, is walked and watched next."""
    root = tmp_path / "workspace"
    root.mkdir()
    real_folders = watch._folders
    made = threading.Event()

    def folders_then_more(path: Path) -> list[str]:
        folders = real_folders(path)
        if "new" in {Path(folder).name for folder in folders} and not made.is_set():
            made.set()
            (root / "late").mkdir()
            (root / "new" / "sub").mkdir()
        return folders

    monkeypatch.setattr(watch, "_folders", folders_then_more)
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / "new").mkdir()
        await _until(next_changes, "new/sub")
        await asyncio.sleep(1)
        (root / "late" / "a.md").write_text("x")
        (root / "new" / "sub" / "b.md").write_text("x")

        seen = await _until(next_changes, "late/a.md", "new/sub/b.md")

    assert {"late/a.md", "new/sub/b.md"} <= seen


def test_only_a_folder_the_walk_takes_in_calls_for_a_new_walk(tmp_path: Path) -> None:
    """A cache, a credential folder or a link appearing never makes the whole tree be walked again."""
    root = tmp_path / "workspace"
    for name in ("src", "__pycache__", "node_modules", ".ssh"):
        (root / name).mkdir(parents=True)
    (root / "link").symlink_to(root / "src")
    added = watchfiles.Change.added

    assert watch._batch(root, {(added, str(root / "src"))})[1]
    for name in ("__pycache__", "node_modules", ".ssh", "link"):
        assert not watch._batch(root, {(added, str(root / name))})[1], name


@pytest.mark.asyncio
async def test_a_link_made_in_the_workspace_is_reported_even_when_it_leads_outside(tmp_path: Path) -> None:
    """The link is an entry of its folder, so it shows as changed; nothing beyond it ever does."""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    async with workspace_changes(root) as next_changes:
        (root / "out").symlink_to(tmp_path / "outside")

        assert "out" in await _until(next_changes, "out")
