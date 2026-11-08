"""Shared knowledge folder watching, driven by real filesystem events."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs
from watchfiles import Change

import mindroom.knowledge.watch as knowledge_watch
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from tests.conftest import SETTLE_TIMEOUT_SECONDS, runtime_paths_for
from tests.knowledge_test_support import _config, _wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# Only the Linux backend follows links while it watches a folder tree, so only Linux watches each folder on its own.
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="each folder is watched on its own only on Linux")


@dataclass
class _RecordedWatch:
    """The folders each watcher start watched, whether recursively, and every batch of changes it received."""

    starts: list[tuple[frozenset[str], bool]] = field(default_factory=list)
    batches: list[set[tuple[Change, str]]] = field(default_factory=list)

    def changed_paths(self) -> set[str]:
        return {path for batch in self.batches for _change, path in batch}


@pytest.fixture
def recorded_watch(monkeypatch: pytest.MonkeyPatch) -> _RecordedWatch:
    """Record the real watcher's starts and batches without changing what it does."""
    recorded = _RecordedWatch()
    real_awatch = knowledge_watch.awatch

    async def _recording_awatch(*paths: Path, **kwargs: object) -> AsyncIterator[set[tuple[Change, str]]]:
        recorded.starts.append((frozenset(map(str, paths)), bool(kwargs.get("recursive", True))))
        async with aclosing(real_awatch(*paths, **kwargs)) as batches:
            async for changes in batches:
                recorded.batches.append(changes)
                yield changes

    monkeypatch.setattr(knowledge_watch, "awatch", _recording_awatch)
    return recorded


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """Return a canonical base folder holding one subfolder, ``notes``."""
    path = tmp_path / "docs"
    (path / "notes").mkdir(parents=True)
    return path.resolve()


async def _write_until_seen(recorded: _RecordedWatch, path: Path) -> None:
    """Write ``path`` again until the watcher reports it, since a folder created moments ago may not be watched yet."""
    try:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            while str(path) not in recorded.changed_paths():
                path.write_text("changed", encoding="utf-8")
                await asyncio.sleep(0.2)
    except TimeoutError:
        pytest.fail(f"the watcher never reported {path}")


@asynccontextmanager
async def _watching(tmp_path: Path, docs: Path, scheduler: MagicMock, recorded: _RecordedWatch) -> AsyncIterator[None]:
    """Watch the ``docs`` base from its first watcher start until the block ends."""
    config = _config(tmp_path, bases={"docs": docs}, agent_bases=["docs"], watch=True)
    watcher = KnowledgeSourceWatcher(scheduler, runtime_paths_for(config))
    await watcher.sync(config)
    try:
        await _wait_until(lambda: recorded.starts, "the watcher did not start")
        yield
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watch_never_follows_a_link_out_of_the_base(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
) -> None:
    """A link inside a base adds no watch outside it, so changes behind the link schedule no refresh."""
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (docs / "linked").symlink_to(outside, target_is_directory=True)
    inside = docs / "notes" / "inside.md"

    async with _watching(tmp_path, docs, MagicMock(), recorded_watch):
        (outside / "deep" / "outside.md").write_text("outside", encoding="utf-8")
        inside.write_text("inside", encoding="utf-8")
        await _wait_until(
            lambda: str(inside) in recorded_watch.changed_paths(),
            "the change inside the base was not seen",
        )

    # Changes arrive in order, so the change behind the link, made first, would have arrived by now.
    assert not [path for path in recorded_watch.changed_paths() if path.startswith(str(docs / "linked"))]
    assert recorded_watch.starts == [(frozenset({str(docs), str(docs / "notes")}), False)]


@pytest.mark.asyncio
async def test_watch_ignores_a_link_created_while_watching(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
) -> None:
    """A link to a folder, created in a watched base, is not watched like a new folder."""
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = docs / "linked"

    async with _watching(tmp_path, docs, MagicMock(), recorded_watch):
        linked.symlink_to(outside, target_is_directory=True)
        await _wait_until(lambda: str(linked) in recorded_watch.changed_paths(), "the new link was not seen")
        await _write_until_seen(recorded_watch, docs / "notes" / "after.md")

    assert len(recorded_watch.starts) == 1


def test_watched_folders_refuse_a_base_whose_folder_became_a_link(tmp_path: Path) -> None:
    """Looking for folders again never walks a base folder that was replaced by a link."""
    (tmp_path / "elsewhere" / "deep").mkdir(parents=True)
    replaced = tmp_path.resolve() / "docs"
    replaced.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(OSError, match=r"Too many levels of symbolic links|Not a directory"):
        knowledge_watch._watched_folders(replaced, set())


@pytest.mark.asyncio
async def test_watch_keeps_one_recursive_watch_where_the_backend_never_follows_links(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off Linux the base keeps one recursive watch, since watching many folders one by one costs those backends more."""
    monkeypatch.setattr(knowledge_watch, "_WATCH_EACH_FOLDER", False)
    scheduler = MagicMock()
    async with _watching(tmp_path, docs, scheduler, recorded_watch):
        await _write_until_seen(recorded_watch, docs / "notes" / "image.bin")
        # The watcher takes its next batch only after handling the last one.
        await _write_until_seen(recorded_watch, docs / "notes" / "other.bin")
        assert not scheduler.schedule_refresh.called
        await _write_until_seen(recorded_watch, docs / "notes" / "doc.md")
        await _wait_until(lambda: scheduler.schedule_refresh.called, "a change in the base did not refresh")

    assert recorded_watch.starts == [(frozenset({str(docs)}), True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_folder", ["new/nested", "drafts~"], ids=["nested", "name-watchfiles-filters"])
async def test_watch_refreshes_for_changes_in_existing_and_new_folders(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    new_folder: str,
) -> None:
    """A change in a folder that existed at start, or in one created while watching, schedules a refresh."""
    scheduler = MagicMock()
    async with _watching(tmp_path, docs, scheduler, recorded_watch):
        await _write_until_seen(recorded_watch, docs / "notes" / "existing.md")
        await _wait_until(lambda: scheduler.schedule_refresh.called, "a change in an existing folder did not refresh")

        (docs / new_folder).mkdir(parents=True)
        scheduler.reset_mock()
        await _write_until_seen(recorded_watch, docs / new_folder / "doc.md")
        await _wait_until(lambda: scheduler.schedule_refresh.called, "a change in a new folder did not refresh")

    assert scheduler.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
async def test_watch_refreshes_for_a_file_written_into_a_new_folder_at_once(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
) -> None:
    """A file written into a new folder before the watcher reaches that folder still schedules one refresh."""
    scheduler = MagicMock()
    async with _watching(tmp_path, docs, scheduler, recorded_watch):
        # A second batch means the watcher has checked its first folders, so only the new folder's event finds "fresh".
        await _wait_until(lambda: len(recorded_watch.batches) >= 2, "the watcher did not settle")
        (docs / "fresh").mkdir()
        (docs / "fresh" / "doc.md").write_text("fresh", encoding="utf-8")
        await _wait_until(lambda: scheduler.schedule_refresh.called, "the file in the new folder did not refresh")
        batches_after_refresh = len(recorded_watch.batches) + 2
        await _wait_until(lambda: len(recorded_watch.batches) >= batches_after_refresh, "the watcher stopped reporting")

    assert scheduler.schedule_refresh.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["notes", "drafts~"], ids=["plain", "name-watchfiles-filters"])
async def test_watch_refreshes_when_a_folder_leaves_the_base(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    folder: str,
) -> None:
    """Moving a folder out of the base schedules a refresh, since the files it holds leave the base with it.

    Deleting a file that is not indexable schedules none.
    """
    (docs / folder).mkdir(exist_ok=True)
    (docs / folder / "doc.md").write_text("indexed", encoding="utf-8")
    image = docs / "image.bin"
    image.write_bytes(b"")
    scheduler = MagicMock()
    async with _watching(tmp_path, docs, scheduler, recorded_watch):
        await _wait_until(lambda: len(recorded_watch.batches) >= 2, "the watcher did not settle")
        image.unlink()
        await _wait_until(lambda: str(image) in recorded_watch.changed_paths(), "the deletion was not seen")
        # The watcher takes its next batch only after handling the last one.
        await _write_until_seen(recorded_watch, docs / "other.bin")
        assert not scheduler.schedule_refresh.called
        (docs / folder).rename(tmp_path / "moved")
        await _wait_until(lambda: scheduler.schedule_refresh.called, "moving a folder out of the base did not refresh")

    assert scheduler.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "created_while_watching"),
    [(b"drafts\xff", False), (b"drafts\xff", True), (b"draft\xff.md", True)],
    ids=["folder-at-start", "folder-while-watching", "file-while-watching"],
)
async def test_watch_skips_a_folder_holding_a_name_that_is_not_utf8(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    name: bytes,
    created_while_watching: bool,
) -> None:
    """A folder holding a name that is not valid UTF-8 is left unwatched with one warning, and the rest stays watched.

    Watchfiles ends a watch at any event naming such a path, even one that only opens it, as every listing does.
    """
    notes = docs / "notes"
    entry = notes / os.fsdecode(name)

    def _create_entry() -> None:
        if entry.suffix:
            entry.write_text("draft", encoding="utf-8")
        else:
            entry.mkdir()

    if not created_while_watching:
        _create_entry()
    scheduler = MagicMock()
    with capture_logs() as logs:

        def _skipped() -> list[dict[str, object]]:
            return [entry for entry in logs if entry["event"].startswith("Knowledge filesystem watcher skips")]

        async with _watching(tmp_path, docs, scheduler, recorded_watch):
            if created_while_watching:
                await _wait_until(lambda: len(recorded_watch.batches) >= 2, "the watcher did not settle")
                _create_entry()
            await _wait_until(_skipped, "the watcher did not skip the folder")
            # A new folder makes the watcher walk the base again, which finds the skipped folder again.
            (docs / "later").mkdir()
            await _write_until_seen(recorded_watch, docs / "later" / "doc.md")
            await _wait_until(lambda: scheduler.schedule_refresh.called, "a change beside the folder did not refresh")
            starts = len(recorded_watch.starts)
            # Every listing of the base opens the entry, which must not end the watch again.
            os.close(os.open(entry, os.O_RDONLY))
            await _write_until_seen(recorded_watch, docs / "later" / "other.md")

    assert len(recorded_watch.starts) == starts
    assert str(notes) not in recorded_watch.starts[-1][0]
    assert [entry["folder"] for entry in _skipped()] == [str(notes)]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0, reason="root enters every folder")
async def test_watch_keeps_watching_after_a_change_in_a_folder_it_can_no_longer_enter(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
) -> None:
    """A folder that loses its search permission before the watcher handles a change in it does not stop the watcher."""
    notes = docs / "notes"
    scheduler = MagicMock()
    async with _watching(tmp_path, docs, scheduler, recorded_watch):
        await _wait_until(lambda: len(recorded_watch.batches) >= 2, "the watcher did not settle")
        (notes / "doc.md").write_text("changed", encoding="utf-8")
        notes.chmod(0o600)
        try:
            await _wait_until(
                lambda: str(notes / "doc.md") in recorded_watch.changed_paths(),
                "the change was not seen",
            )
        finally:
            notes.chmod(0o700)
        await _wait_until(lambda: scheduler.schedule_refresh.called, "the change did not refresh")
        await _write_until_seen(recorded_watch, docs / "after.md")


@pytest.mark.asyncio
async def test_watch_keeps_watching_when_a_folder_disappears_before_its_watch_starts(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder deleted between finding it and watching it makes the watcher look again instead of stopping."""
    real_watched_folders = knowledge_watch._watched_folders
    walks: list[Path] = []

    def _first_walk_finds_a_deleted_folder(root: Path, skipped: set[Path]) -> list[Path]:
        walks.append(root)
        folders = real_watched_folders(root, skipped)
        return [*folders, root / "deleted"] if len(walks) == 1 else folders

    monkeypatch.setattr(knowledge_watch, "_watched_folders", _first_walk_finds_a_deleted_folder)
    scheduler = MagicMock()
    async with _watching(tmp_path, docs, scheduler, recorded_watch):
        await _wait_until(lambda: len(recorded_watch.starts) == 2, "the watcher did not look again")
        await _wait_until(lambda: scheduler.schedule_refresh.called, "the watcher did not refresh after looking again")
        await _write_until_seen(recorded_watch, docs / "notes" / "doc.md")


@pytest.mark.asyncio
async def test_watch_stops_and_logs_when_watching_fails_for_another_reason(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a name watchfiles cannot decode makes a running watch walk the base again; other failures stop the watcher."""

    async def _failing_awatch(*paths: Path, **_kwargs: object) -> AsyncIterator[set[tuple[Change, str]]]:
        recorded_watch.starts.append((frozenset(map(str, paths)), False))
        yield set()
        message = "unhandled errors in a TaskGroup"
        raise ExceptionGroup(message, [RuntimeError("watcher closed")])

    monkeypatch.setattr(knowledge_watch, "awatch", _failing_awatch)
    with capture_logs() as logs:
        async with _watching(tmp_path, docs, MagicMock(), recorded_watch):
            await _wait_until(
                lambda: any(entry["event"] == "Knowledge filesystem watcher stopped after failure" for entry in logs),
                "the watcher did not stop after the failure",
            )

    assert len(recorded_watch.starts) == 1


@pytest.mark.asyncio
async def test_watch_stops_and_logs_when_scheduling_a_refresh_fails(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
) -> None:
    """A missing file while scheduling a refresh stops the watcher with its error, instead of being taken for a vanished folder."""
    scheduler = MagicMock()
    scheduler.schedule_refresh.side_effect = FileNotFoundError("missing")
    with capture_logs() as logs:
        async with _watching(tmp_path, docs, scheduler, recorded_watch):
            (docs / "notes" / "doc.md").write_text("changed", encoding="utf-8")
            await _wait_until(
                lambda: any(entry["event"] == "Knowledge filesystem watcher stopped after failure" for entry in logs),
                "the watcher did not stop after the failure",
            )

    assert len(recorded_watch.starts) == 1


@pytest.mark.asyncio
async def test_watch_covers_a_folder_created_while_its_watch_starts(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder created after the watcher found its parent, but before watching began, is watched too."""
    real_watched_folders = knowledge_watch._watched_folders
    late = docs / "notes" / "late"

    def _folder_appears_after_the_walk(root: Path, skipped: set[Path]) -> list[Path]:
        folders = real_watched_folders(root, skipped)
        late.mkdir(exist_ok=True)
        return folders

    monkeypatch.setattr(knowledge_watch, "_watched_folders", _folder_appears_after_the_walk)
    async with _watching(tmp_path, docs, MagicMock(), recorded_watch):
        monkeypatch.setattr(knowledge_watch, "_watched_folders", real_watched_folders)
        await _write_until_seen(recorded_watch, late / "doc.md")


@pytest.mark.asyncio
async def test_watch_skips_folders_whose_changes_are_ignored(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
) -> None:
    """Version-control and dependency folders, whose changes never refresh a base, use no watches."""
    for ignored in (".git/objects", "node_modules/pkg"):
        (docs / ignored).mkdir(parents=True)

    async with _watching(tmp_path, docs, MagicMock(), recorded_watch):
        pass

    assert recorded_watch.starts == [(frozenset({str(docs), str(docs / "notes")}), False)]
    assert not knowledge_watch._is_new_watched_folder(Change.added, str(docs / "node_modules"))
    assert knowledge_watch._is_new_watched_folder(Change.added, str(docs / "notes"))


@pytest.mark.asyncio
async def test_watch_stops_at_the_folder_cap(
    tmp_path: Path,
    docs: Path,
    recorded_watch: _RecordedWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base with more folders than the cap watches only that many, and warns once however often it looks again."""
    monkeypatch.setattr(knowledge_watch, "_MAX_WATCHED_FOLDERS", 2)
    (docs / "more").mkdir()

    with capture_logs() as logs:
        async with _watching(tmp_path, docs, MagicMock(), recorded_watch):
            (docs / "another").mkdir()
            await _wait_until(lambda: len(recorded_watch.starts) == 2, "the watcher did not look again")

    assert [len(folders) for folders, _recursive in recorded_watch.starts] == [2, 2]
    warnings = [
        entry for entry in logs if entry["event"].startswith("Knowledge filesystem watcher stops at its folder cap")
    ]
    assert [entry["watched_folders"] for entry in warnings] == [2]
