"""A workspace's changes, as its change streams receive them from one shared watcher."""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING

import pytest

from mindroom.workspace_files import watch
from mindroom.workspace_files.watch import workspace_changes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


async def _until(next_changes: Callable[[], Awaitable[list[str]]], path: str) -> set[str]:
    """Collect every reported path until ``path`` shows, failing after five seconds."""
    seen: set[str] = set()
    async with asyncio.timeout(5):
        while path not in seen:
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
    """Changed paths come relative to the root and sorted, without Git's, credentials or atomic save names."""
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    async with workspace_changes(root) as next_changes:
        await _settled(root)
        (root / ".git" / "index").write_text("x")
        (root / ".env").write_text("TOKEN=1\n")
        (root / ".mindroom-0123456789abcdef0123456789abcdef.tmp").write_text("x")
        (root / "src" / "b.py").write_text("x")
        (root / "src" / "a.py").write_text("x")
        (root / "zz.md").write_text("x")

        seen = await _until(next_changes, "zz.md")

    assert {"src/a.py", "src/b.py", "zz.md"} <= seen
    assert not {path for path in seen if path.startswith((".git", ".env", ".mindroom-"))}


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
