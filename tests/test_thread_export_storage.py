"""Filesystem-boundary tests for thread exports."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.thread_export import storage
from mindroom.thread_export.models import ThreadExportRoom
from mindroom.thread_export.storage import (
    _safe_path_segment,
    _UnsafeThreadExportPathError,
    reconcile_room_directories,
    remove_room_export,
    remove_stale_thread_exports,
    write_room_index,
    write_thread_payload,
)

if TYPE_CHECKING:
    from pathlib import Path


def _room() -> ThreadExportRoom:
    return ThreadExportRoom(
        key="lobby",
        room_id="!lobby:localhost",
        alias="#lobby:localhost",
        name="Lobby",
    )


def test_safe_path_segment_blocks_dot_directory_segments() -> None:
    """Path segments should not allow current or parent directory traversal."""
    assert _safe_path_segment(".") == "%2E"
    assert _safe_path_segment("..") == "%2E%2E"
    assert _safe_path_segment("%2E") == "%252E"


def test_thread_index_entry_ignores_invalid_utf8(tmp_path: Path) -> None:
    """One invalid UTF-8 YAML file should not abort a room index rebuild."""
    room_dir = tmp_path / "lobby"
    room_dir.mkdir()
    invalid_file = room_dir / "invalid.yaml"
    invalid_file.write_bytes(b"\x80")

    write_room_index(tmp_path, _room())

    index = json.loads((room_dir / "index.json").read_text(encoding="utf-8"))
    assert index["threads"] == []


def test_symlinked_export_root_cannot_write_or_reconcile_outside_workspace(tmp_path: Path) -> None:
    """A workspace-controlled export-root symlink must never grant host filesystem writes."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.yaml"
    marker.write_text("secret", encoding="utf-8")
    output_dir = tmp_path / "thread_exports"
    output_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export root"):
        write_thread_payload(output_dir, _room(), "$thread:localhost", {"version": 1})
    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export root"):
        reconcile_room_directories(output_dir, set())

    assert marker.read_text(encoding="utf-8") == "secret"
    assert output_dir.is_symlink()


def test_symlinked_room_directory_is_never_followed(tmp_path: Path) -> None:
    """Room writes and stale-file deletion must reject a symlink below the export root."""
    output_dir = tmp_path / "thread_exports"
    output_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.yaml"
    marker.write_text("secret", encoding="utf-8")
    room_dir = output_dir / "lobby"
    room_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export room directory"):
        write_thread_payload(output_dir, _room(), "$thread:localhost", {"version": 1})
    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export room directory"):
        remove_stale_thread_exports(output_dir, _room(), [])

    assert marker.read_text(encoding="utf-8") == "secret"
    assert remove_room_export(output_dir, _room()) is True
    assert not room_dir.exists()
    assert marker.read_text(encoding="utf-8") == "secret"


def test_symlinked_thread_file_is_replaced_without_touching_target(tmp_path: Path) -> None:
    """A thread-file symlink should be replaced locally rather than read or followed."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret", encoding="utf-8")
    thread_file = room_dir / "%24thread%3Alocalhost.yaml"
    thread_file.symlink_to(outside)

    assert write_thread_payload(output_dir, _room(), "$thread:localhost", {"version": 1}) is True

    assert outside.read_text(encoding="utf-8") == "secret"
    assert not thread_file.is_symlink()


def test_room_directory_swap_cannot_redirect_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a room directory after it is opened must not redirect the write."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    detached_room_dir = tmp_path / "detached-room"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_atomic_write = storage._atomic_write_at

    def swap_then_write(directory_fd: int, filename: str, text: str) -> None:
        room_dir.rename(detached_room_dir)
        room_dir.symlink_to(outside, target_is_directory=True)
        original_atomic_write(directory_fd, filename, text)

    monkeypatch.setattr(storage, "_atomic_write_at", swap_then_write)

    assert write_thread_payload(output_dir, _room(), "$thread:localhost", {"version": 1}) is True

    filename = "%24thread%3Alocalhost.yaml"
    assert (detached_room_dir / filename).is_file()
    assert not (outside / filename).exists()


def test_export_root_swap_cannot_redirect_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the export root after it is opened must not redirect deletion."""
    output_dir = tmp_path / "thread_exports"
    stale_room = output_dir / "stale"
    stale_room.mkdir(parents=True)
    detached_root = tmp_path / "detached-root"
    outside = tmp_path / "outside"
    outside_room = outside / "keep"
    outside_room.mkdir(parents=True)
    marker = outside_room / "marker"
    marker.write_text("secret", encoding="utf-8")
    original_listdir = storage.os.listdir
    swapped = False

    def swap_then_list(directory_fd: int) -> list[str]:
        nonlocal swapped
        if not swapped:
            output_dir.rename(detached_root)
            output_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_listdir(directory_fd)

    monkeypatch.setattr(storage.os, "listdir", swap_then_list)

    reconcile_room_directories(output_dir, set())

    assert not (detached_root / "stale").exists()
    assert marker.read_text(encoding="utf-8") == "secret"
