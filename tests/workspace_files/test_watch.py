"""A workspace's changes, as its change streams receive them from one shared watcher."""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from mindroom.workspace_files import watch
from mindroom.workspace_files.watch import workspace_changes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path


async def _until(next_changes: Callable[[], Awaitable[list[str]]], *paths: str) -> set[str]:
    """Collect every reported path until all of ``paths`` show, failing after five seconds."""
    seen: set[str] = set()
    async with asyncio.timeout(5):
        while not seen.issuperset(paths):
            seen.update(await next_changes())
    return seen


@pytest.mark.asyncio
async def test_a_stream_hears_what_changes_but_never_git_credentials_or_save_temporaries(tmp_path: Path) -> None:
    """Changed paths come relative to the root, without Git's, credentials, caches or atomic save names."""
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    async with workspace_changes(root) as next_changes:
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
    """Every batch lists its paths in order."""
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace_changes(root) as next_changes:
        for name in ("c.md", "a.md", "b.md"):
            (root / name).write_text("x")
        batches: list[list[str]] = []
        async with asyncio.timeout(5):
            while not {"a.md", "b.md", "c.md"} <= {path for batch in batches for path in batch}:
                batches.append(await next_changes())

    assert all(batch == sorted(batch) for batch in batches)


@pytest.mark.asyncio
async def test_changes_beyond_a_link_out_of_the_workspace_are_never_followed(tmp_path: Path) -> None:
    """A link the agent plants to a folder outside reports nothing of what changes there."""
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "out").symlink_to(outside)
    async with workspace_changes(root) as next_changes:
        (outside / "secret.txt").write_text("x")
        (root / "inside.txt").write_text("x")

        seen = await _until(next_changes, "inside.txt")

    assert not {path for path in seen if path.startswith("out")}


@pytest.mark.asyncio
async def test_streams_of_one_workspace_share_one_watcher_until_the_last_leaves(tmp_path: Path) -> None:
    """However many streams follow a root, one watcher runs for it, and it stops once none follows."""
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace_changes(root) as first, workspace_changes(root) as second:
        assert list(watch._watches) == [root]
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
        (elsewhere / "secret.txt").write_text("x")

        assert await _silent(next_changes) == []


@pytest.mark.asyncio
async def test_a_folder_moved_out_of_the_workspace_is_no_longer_followed(tmp_path: Path) -> None:
    """Once a folder leaves the workspace, what changes in it is not reported under its old name."""
    root = tmp_path / "workspace"
    (root / "box").mkdir(parents=True)
    async with workspace_changes(root) as next_changes:
        (root / "box").rename(tmp_path / "box")
        await _until(next_changes, "box")
        (tmp_path / "box" / "later.txt").write_text("x")
        (root / "marker.txt").write_text("x")

        seen = await _until(next_changes, "marker.txt")

    assert "box/later.txt" not in seen


@pytest.mark.asyncio
async def test_a_workspace_stored_below_a_folder_named_like_a_cache_is_followed(tmp_path: Path) -> None:
    """Only names inside the workspace are filtered, not the folders MindRoom's storage sits in."""
    root = tmp_path / ".cache" / "node_modules" / "workspace"
    root.mkdir(parents=True)
    async with workspace_changes(root) as next_changes:
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


@pytest.mark.asyncio
async def test_a_link_made_in_the_workspace_is_reported_even_when_it_leads_outside(tmp_path: Path) -> None:
    """The link is an entry of its folder, so it shows as changed; nothing beyond it ever does."""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    async with workspace_changes(root) as next_changes:
        (root / "out").symlink_to(tmp_path / "outside")

        assert "out" in await _until(next_changes, "out")


def test_a_scan_leaves_out_the_deepest_folders_past_the_cap_and_never_looks_into_hidden_ones_or_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool's deep cache in the agent's HOME can't crowd out the project's own top folders, and links lead nowhere."""
    root = tmp_path / "workspace"
    for top in ("a", "b", "c"):
        (root / top / "deep" / "deeper").mkdir(parents=True)
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_text("key")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("x")
    (root / "out").symlink_to(tmp_path / "outside")
    monkeypatch.setattr(watch, "_MAX_FOLDERS", 4)

    seen = set(watch._scan(root))

    assert seen == {"a", "b", "c", "out", "a/deep", "b/deep", "c/deep"}


@pytest.mark.asyncio
async def test_a_folder_swapped_for_a_link_out_never_reports_what_lies_beyond(tmp_path: Path) -> None:
    """Code the agent runs may swap a folder for a link and back at any moment; no name from beyond it ever shows."""
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    async with workspace_changes(root) as next_changes:
        for attempt in range(30):
            (root / "sub").rename(root / "held")
            (root / "sub").symlink_to(outside)
            (outside / f"secret{attempt}.txt").write_text("x")
            await asyncio.sleep(0.02)
            (root / "sub").unlink()
            (root / "held").rename(root / "sub")
        (root / "marker.md").write_text("x")
        seen = await _until(next_changes, "marker.md")

    assert not {path for path in seen if "secret" in path}


@pytest.mark.asyncio
async def test_what_a_recreated_folder_holds_is_reported(tmp_path: Path) -> None:
    """A folder deleted and made again under its name reports what it holds now."""
    root = tmp_path / "workspace"
    (root / "out").mkdir(parents=True)
    (root / "out" / "old.txt").write_text("x")
    async with workspace_changes(root) as next_changes:
        shutil.rmtree(root / "out")
        (root / "out").mkdir()
        (root / "out" / "new.txt").write_text("x")

        assert {"out/old.txt", "out/new.txt"} <= await _until(next_changes, "out/old.txt", "out/new.txt")


@pytest.mark.asyncio
async def test_files_written_into_folders_as_they_appear_are_all_reported(tmp_path: Path) -> None:
    """A build that makes folders and fills them at once loses no change."""
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace_changes(root) as next_changes:
        expected = set()
        for index in range(20):
            folder = root / f"pkg{index}" / "src"
            folder.mkdir(parents=True)
            (folder / "mod.py").write_text("x")
            expected.add(f"pkg{index}/src/mod.py")

        assert expected <= await _until(next_changes, *expected)


@pytest.mark.asyncio
async def test_a_file_rewritten_in_place_with_the_same_size_is_reported(tmp_path: Path) -> None:
    """An edit that keeps the file, its inode and its size still shows."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("draft-1")
    async with workspace_changes(root) as next_changes:
        with (root / "notes.md").open("r+") as file:
            file.write("draft-2")

        assert "notes.md" in await _until(next_changes, "notes.md")


def test_a_scan_takes_in_at_most_the_cap_s_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the entry cap a scan stops, so one huge folder can't hold the scanner forever."""
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(10):
        (root / f"f{index}.txt").write_text("x")
    monkeypatch.setattr(watch, "_MAX_ENTRIES", 4)

    assert len(watch._scan(root)) == 4


def test_an_entry_that_vanishes_mid_scan_leaves_its_neighbours_in_the_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary file gone between listing and reading its details drops only itself, so nothing else looks changed."""
    root = tmp_path / "workspace"
    (root / "proj" / "pkg").mkdir(parents=True)
    for name in ("a.py", "b.py", "pkg/c.py"):
        (root / "proj" / name).write_text("x")
    (root / "proj" / "tmp.swap").write_text("x")
    before = watch._scan(root)
    real_scandir = os.scandir

    class _Vanishing:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            if self.name == "tmp.swap":
                raise FileNotFoundError(self.name)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    @contextmanager
    def scandir_with_a_vanishing_entry(directory: int) -> Iterator[list[_Vanishing]]:
        with real_scandir(directory) as entries:
            yield [_Vanishing(entry) for entry in sorted(entries, key=lambda entry: entry.name != "tmp.swap")]

    monkeypatch.setattr(watch.os, "scandir", scandir_with_a_vanishing_entry)
    during = watch._scan(root)

    assert watch._changed(before, during) == {"proj/tmp.swap"}


def test_a_scan_leaves_out_pipes(tmp_path: Path) -> None:
    """Only what a folder listing shows can be reported as changed."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("x")
    os.mkfifo(root / "pipe")

    assert set(watch._scan(root)) == {"notes.md"}


def test_entries_a_scan_leaves_out_count_toward_its_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code the agent runs can plant countless hidden, cached or special files; each still counts, so a scan stays bounded."""
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(10):
        (root / f".env.{index}").write_text("x")
        os.mkfifo(root / f"pipe{index}")
    (root / "notes.md").write_text("x")
    monkeypatch.setattr(watch, "_MAX_ENTRIES", 5)
    examined = []
    real_is_hidden = watch.is_hidden
    monkeypatch.setattr(watch, "is_hidden", lambda path: examined.append(path) or real_is_hidden(path))

    watch._scan(root)

    assert len(examined) <= 5


@pytest.mark.asyncio
async def test_a_follower_that_falls_too_far_behind_is_told_to_connect_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that stops reading can't make the changes waiting for it grow without bound."""
    monkeypatch.setattr(watch, "_MAX_ENTRIES", 3)
    follower = watch._Follower()
    follower.add({"a", "b"})
    assert await follower.next() == ["a", "b"]

    follower.add({"c", "d"})
    follower.add({"e", "f"})
    follower.add({"g", "h"})
    assert not follower.pending

    with pytest.raises(HTTPException) as overflow:
        await follower.next()
    assert overflow.value.status_code == 503


def test_a_scan_leaves_out_folders_past_the_depth_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deep chain of folders, which each scan would open from the root, costs at most the cap's depth."""
    root = tmp_path / "workspace"
    (root / "a" / "b" / "c" / "d").mkdir(parents=True)
    monkeypatch.setattr(watch, "_MAX_DEPTH", 2)

    assert set(watch._scan(root)) == {"a", "a/b"}
