"""Removing a worker's own tree through descriptors."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from mindroom.workers.worker_retirement import remove_directory_tree_at

if TYPE_CHECKING:
    from pathlib import Path


def _remove(root: Path, name: str) -> None:
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        remove_directory_tree_at(parent_fd, name)
    finally:
        os.close(parent_fd)


def test_a_tree_with_read_only_directories_is_removed(tmp_path: Path) -> None:
    """Directories a tool left read-only, such as Go's module cache, do not stop the removal."""
    locked = tmp_path / "worker" / "cache" / "mod" / "pkg"
    locked.mkdir(parents=True)
    (locked / "module.go").write_text("package m\n", encoding="utf-8")
    for directory in (locked, locked.parent):
        directory.chmod(0o555)

    _remove(tmp_path, "worker")

    assert not (tmp_path / "worker").exists()


def test_removal_never_changes_a_directory_a_link_points_to(tmp_path: Path) -> None:
    """A link the worker planted to an outside directory is removed, and that directory keeps its mode and files."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("primary file\n", encoding="utf-8")
    outside.chmod(0o500)
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "escape").symlink_to(outside, target_is_directory=True)
    try:
        _remove(tmp_path, "worker")

        assert not worker.exists()
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "primary file\n"
    finally:
        outside.chmod(0o700)
