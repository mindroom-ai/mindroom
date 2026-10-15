"""Tests for the shared directory ledger and its storage."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003

import pytest

from mindroom.shared_directories.store import (
    SharedDirectory,
    SharedDirectoryError,
    add_shared_directory_member,
    create_shared_directory,
    delete_shared_directory,
    load_shared_directories,
    remove_shared_directory_member,
)
from tests.conftest import test_runtime_paths

_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_create_writes_ledger_and_real_directory(tmp_path: Path) -> None:
    """Creating a directory records it and makes a real directory below the storage root."""
    runtime_paths = test_runtime_paths(tmp_path)

    created = create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)

    assert created == SharedDirectory(owner="alice", name="report", members=(), created_at=_NOW.isoformat())
    assert (runtime_paths.storage_root / "shared_directories" / "alice" / "report").is_dir()
    assert load_shared_directories(runtime_paths) == (created,)


def test_create_refuses_duplicate(tmp_path: Path) -> None:
    """An owner cannot create two directories with the same name."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)

    with pytest.raises(SharedDirectoryError, match="alice already has a shared directory named 'report'"):
        create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)


@pytest.mark.parametrize("name", ["..", "a/b", "Report", "-x", "x" * 65, ""])
def test_invalid_names_are_refused(tmp_path: Path, name: str) -> None:
    """Names that could escape or collide are refused before touching the filesystem."""
    runtime_paths = test_runtime_paths(tmp_path)

    with pytest.raises(SharedDirectoryError, match="Shared directory names"):
        create_shared_directory(runtime_paths, owner="alice", name=name, now=_NOW)

    assert not (runtime_paths.storage_root / "shared_directories").exists()
    assert load_shared_directories(runtime_paths) == ()


def test_create_refuses_owner_directory_replaced_by_link(tmp_path: Path) -> None:
    """A linked owner directory is refused, so nothing is created through the link."""
    runtime_paths = test_runtime_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime_paths.storage_root / "shared_directories").mkdir(parents=True)
    (runtime_paths.storage_root / "shared_directories" / "alice").symlink_to(outside)

    with pytest.raises(SharedDirectoryError, match="could not be created"):
        create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)

    assert list(outside.iterdir()) == []
    assert load_shared_directories(runtime_paths) == ()


def test_members_add_and_remove(tmp_path: Path) -> None:
    """Adding and removing members is idempotent and keeps members sorted."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)

    added, changed = add_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")
    assert (added.members, changed) == (("bob",), True)
    assert add_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")[1] is False
    added, _ = add_shared_directory_member(runtime_paths, owner="alice", name="report", member="ann")
    assert added.members == ("ann", "bob")
    assert load_shared_directories(runtime_paths)[0].members == ("ann", "bob")

    removed, changed = remove_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")
    assert (removed.members, changed) == (("ann",), True)
    assert remove_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")[1] is False
    assert load_shared_directories(runtime_paths)[0].members == ("ann",)


def test_delete_empties_the_directory_without_following_links(tmp_path: Path) -> None:
    """Deleting removes the record and every file, never what a planted link points at."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    add_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")
    share = runtime_paths.storage_root / "shared_directories" / "alice" / "report"
    (share / "notes").mkdir()
    (share / "notes" / "summary.md").write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (share / "escape").symlink_to(outside)

    removed = delete_shared_directory(runtime_paths, owner="alice", name="report")

    assert removed == SharedDirectory(owner="alice", name="report", members=("bob",), created_at=_NOW.isoformat())
    assert list(share.iterdir()) == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert load_shared_directories(runtime_paths) == ()


def test_delete_succeeds_when_the_tree_is_already_gone(tmp_path: Path) -> None:
    """Deleting a directory whose tree is already gone still forgets it."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    (runtime_paths.storage_root / "shared_directories" / "alice" / "report").rmdir()

    delete_shared_directory(runtime_paths, owner="alice", name="report")

    assert load_shared_directories(runtime_paths) == ()


@pytest.mark.parametrize(
    "operation",
    [
        lambda runtime_paths: add_shared_directory_member(runtime_paths, owner="alice", name="nope", member="bob"),
        lambda runtime_paths: remove_shared_directory_member(runtime_paths, owner="alice", name="nope", member="bob"),
        lambda runtime_paths: delete_shared_directory(runtime_paths, owner="alice", name="nope"),
    ],
)
def test_unknown_directory_is_refused(tmp_path: Path, operation: object) -> None:
    """Mutating a directory the owner does not have is refused."""
    runtime_paths = test_runtime_paths(tmp_path)

    with pytest.raises(SharedDirectoryError, match="alice has no shared directory named 'nope'"):
        operation(runtime_paths)  # type: ignore[operator]


def test_unreadable_ledger_lists_nothing_and_refuses_changes(tmp_path: Path) -> None:
    """An unparseable ledger lists no directories, and no change writes over its records."""
    runtime_paths = test_runtime_paths(tmp_path)
    ledger = runtime_paths.storage_root / "tracking" / "shared_directories.json"
    ledger.parent.mkdir(parents=True)
    broken = '{"alice/report": {"owner": "alice"}, "broken": 3'
    ledger.write_text(broken, encoding="utf-8")

    assert load_shared_directories(runtime_paths) == ()
    with pytest.raises(SharedDirectoryError, match="nothing was changed"):
        create_shared_directory(runtime_paths, owner="alice", name="notes", now=_NOW)
    assert ledger.read_text(encoding="utf-8") == broken


def test_recreating_a_deleted_directory_reuses_it(tmp_path: Path) -> None:
    """Workers that still mount a deleted directory reach the files of the one created again under its name."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    share = runtime_paths.storage_root / "shared_directories" / "alice" / "report"
    mounted = share.stat()
    delete_shared_directory(runtime_paths, owner="alice", name="report")

    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)

    assert share.stat().st_ino == mounted.st_ino


def test_malformed_records_are_dropped(tmp_path: Path) -> None:
    """Records with an invalid shape or name are ignored."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    ledger = runtime_paths.storage_root / "tracking" / "shared_directories.json"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            "{",
            '{"bob/x": {"owner": "bob", "name": "../x", "members": [], "created_at": ""}, ',
            1,
        ),
        encoding="utf-8",
    )

    assert [directory.name for directory in load_shared_directories(runtime_paths)] == ["report"]


def test_concurrent_invites_keep_every_member(tmp_path: Path) -> None:
    """Concurrent invitations never lose a member."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    members = [f"m{index}" for index in range(16)]
    threads = [
        threading.Thread(
            target=add_shared_directory_member,
            args=(runtime_paths,),
            kwargs={"owner": "alice", "name": "report", "member": member},
        )
        for member in members
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert load_shared_directories(runtime_paths)[0].members == tuple(sorted(members))


def test_delete_keeps_the_record_when_the_owner_directory_is_a_link(tmp_path: Path) -> None:
    """A linked owner directory is never followed, and the record stays so the delete can be retried."""
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    owner_directory = runtime_paths.storage_root / "shared_directories" / "alice"
    moved = tmp_path / "moved"
    owner_directory.rename(moved)
    owner_directory.symlink_to(moved)

    with pytest.raises(SharedDirectoryError, match="could not be deleted"):
        delete_shared_directory(runtime_paths, owner="alice", name="report")

    assert (moved / "report").is_dir()
    assert [directory.name for directory in load_shared_directories(runtime_paths)] == ["report"]
