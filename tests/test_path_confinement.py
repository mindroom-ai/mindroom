"""Filesystem behavior for the shared workspace containment boundary."""

import os
from pathlib import Path

import pytest

from mindroom.path_confinement import (
    open_directory_within_root,
    open_regular_file_within_root,
    resolve_path_within_root,
)


@pytest.mark.parametrize("source", ["inside", "link", "nested/../inside"])
def test_resolve_internal_links(tmp_path: Path, source: str) -> None:
    """Internal links and traversal that stays under the root remain supported."""
    (tmp_path / "inside").write_text("safe")
    (tmp_path / "nested").mkdir()
    (tmp_path / "link").symlink_to("inside")
    assert resolve_path_within_root(tmp_path, source, symlinks="internal", strict=True) == tmp_path / "inside"


@pytest.mark.parametrize("source", ["../secret", "escape/secret", "dangling", "loop"])
def test_resolve_rejects_escape_and_loop(tmp_path: Path, source: str) -> None:
    """Neither escaping targets nor loops are accepted, including dangling links."""
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    (tmp_path / "dangling").symlink_to(tmp_path.parent / "secret")
    (tmp_path / "loop").symlink_to("loop")
    with pytest.raises((ValueError, OSError, RuntimeError)):
        resolve_path_within_root(tmp_path, source, symlinks="internal")


def test_resolution_policies(tmp_path: Path) -> None:
    """Private paths reject links while atomic replacement preserves the leaf."""
    (tmp_path / "inside").write_text("safe")
    (tmp_path / "link").symlink_to("inside")
    with pytest.raises(ValueError, match="authorized root"):
        resolve_path_within_root(tmp_path, "link", symlinks="reject")
    assert resolve_path_within_root(tmp_path, "link", symlinks="preserve_leaf") == tmp_path / "link"
    (tmp_path / "parent").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="authorized root"):
        resolve_path_within_root(tmp_path, "parent/inside", symlinks="preserve_leaf")
    assert resolve_path_within_root(tmp_path, "new/file", symlinks="internal") == tmp_path / "new/file"
    with pytest.raises(FileNotFoundError):
        resolve_path_within_root(tmp_path, "new/file", symlinks="internal", strict=True)


@pytest.mark.parametrize("source", ["../escape", "/absolute"])
def test_descriptor_walk_rejects_invalid_paths_before_creation(tmp_path: Path, source: str) -> None:
    """Invalid descriptor paths cannot create files outside the root."""
    with pytest.raises(ValueError, match="Descriptor paths"), open_directory_within_root(tmp_path, source, create=True):
        pytest.fail("unsafe path admitted")
    assert not list(tmp_path.iterdir())


def test_directory_creation_and_borrowed_descriptor_lifetime(tmp_path: Path) -> None:
    """Creation honors permissions and never closes a caller-owned root."""
    with open_directory_within_root(tmp_path) as root_fd:
        with open_directory_within_root(root_fd, "nested/child", create=True, mode=0o700) as child_fd:
            assert os.fstat(child_fd).st_mode & 0o777 == 0o700
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(child_fd)
        os.fstat(root_fd)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(root_fd)


@pytest.mark.parametrize("part", ["parent", "leaf"])
def test_regular_file_rejects_swap_after_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str) -> None:
    """Parent and final-entry swaps cannot redirect a validated read."""
    workspace = tmp_path / "workspace"
    parent = workspace / "parent"
    parent.mkdir(parents=True)
    (parent / "file").write_text("safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("secret")
    resolved = resolve_path_within_root(workspace, "parent/file", symlinks="internal")
    original_open = os.open

    def swap(name: object, flags: int, *args: object, **kwargs: object) -> int:
        if name == ("parent" if part == "parent" else "file"):
            if part == "parent":
                parent.rename(workspace / "old-parent")
                parent.symlink_to(outside, target_is_directory=True)
            else:
                (parent / "file").unlink()
                (parent / "file").symlink_to(outside / "file")
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with (
        pytest.raises(OSError, match=r"Not a directory|Too many levels"),
        open_regular_file_within_root(workspace, resolved.relative_to(workspace)),
    ):
        pytest.fail("swapped symlink admitted")
    assert (outside / "file").read_text() == "secret"


def test_open_regular_file_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """A named pipe must not block or be treated as a regular file."""
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ValueError, match="regular file"), open_regular_file_within_root(tmp_path, "fifo"):
        pytest.fail("FIFO admitted")


def test_open_regular_file_reads_and_closes(tmp_path: Path) -> None:
    """The borrowed descriptor is usable inside the context and closed after it."""
    (tmp_path / "file").write_bytes(b"original")
    with open_regular_file_within_root(tmp_path, "file") as descriptor:
        assert os.read(descriptor, 20) == b"original"
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_trusted_root_alias_and_absolute_input(tmp_path: Path) -> None:
    """Trusted root aliases canonicalize without admitting an outside absolute input."""
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert resolve_path_within_root(alias, root / "new", symlinks="internal") == root / "new"
    with pytest.raises(ValueError, match="authorized root"):
        resolve_path_within_root(root, tmp_path / "other", symlinks="internal")


def test_reject_policy_checks_links_before_parent_traversal(tmp_path: Path) -> None:
    """Normalization must not hide a forbidden intermediate link."""
    (tmp_path / "dir").mkdir()
    (tmp_path / "link").symlink_to("dir", target_is_directory=True)
    with pytest.raises(ValueError, match="authorized root"):
        resolve_path_within_root(tmp_path, "link/../new", symlinks="reject")


def test_preserved_leaf_can_replace_outside_link(tmp_path: Path) -> None:
    """Replacement targets the directory entry without inspecting the link's target."""
    (tmp_path / "leaf").symlink_to(tmp_path.parent / "outside")
    assert resolve_path_within_root(tmp_path, "leaf", symlinks="preserve_leaf") == tmp_path / "leaf"


def test_directory_creation_rejects_link_created_during_mkdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A competing mkdir/link publication cannot redirect subsequent child creation."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = os.mkdir

    def swap(path: object, *args: object, **kwargs: object) -> None:
        if path == "parent":
            (root / "parent").symlink_to(outside, target_is_directory=True)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", swap)
    with (
        pytest.raises(OSError, match="Not a directory"),
        open_directory_within_root(root, "parent/child", create=True),
    ):
        pytest.fail("mkdir race admitted")
    assert not list(outside.iterdir())


def test_failed_walk_closes_owned_descriptors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error halfway through traversal closes every descriptor it acquired."""
    (tmp_path / "parent").mkdir()
    descriptors: list[int] = []
    original_open = os.open

    def tracked_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, *args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(FileNotFoundError), open_directory_within_root(tmp_path, "parent/missing"):
        pytest.fail("missing directory admitted")
    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
