"""Real filesystem checks for the locally authorized desktop file provider."""

import os
from pathlib import Path

import pytest

from mindroom.desktop.filesystem import DesktopFilesystem, DesktopFilesystemError


def test_lists_pinned_root_and_reads_bounded_utf8(tmp_path: Path) -> None:
    """A split codepoint resumes at its byte offset without data loss."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "text.txt").write_text("a" * 16383 + "é" + "tail")
    files = DesktopFilesystem((root,))
    try:
        folders = files.list_folders()["folders"]
        assert len(folders) == 1
        root_id = folders[0]["id"]
        assert folders[0]["path"] == str(root)
        assert {entry["name"] for entry in files.list_directory(root_id)["entries"]} == {"nested"}
        assert files.list_directory(root_id, "nested")["entries"] == [{"name": "text.txt", "type": "file"}]
        first = files.read_file(root_id, "nested/text.txt")
        assert first["text"] == "a" * 16383
        assert first["offset"] == 0
        assert first["next_offset"] == 16383
        assert first["truncated"] is True
        second = files.read_file(root_id, "nested/text.txt", first["next_offset"])
        assert second["text"] == "étail"
        assert second["eof"] is True
    finally:
        files.close()


def test_rejects_traversal_links_binary_and_fifo(tmp_path: Path) -> None:
    """Unsafe paths and non-text files cannot escape or block a read."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "linked").symlink_to(outside)
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    (root / "binary").write_bytes(b"a\x00b")
    (root / "controls").write_bytes(b"a\x01b")
    os.mkfifo(root / "pipe")
    files = DesktopFilesystem((root,))
    try:
        root_id = files.list_folders()["folders"][0]["id"]
        for path in ("../outside.txt", str(outside), "linked", "parent/outside.txt", "pipe", "binary", "controls"):
            with pytest.raises(DesktopFilesystemError):
                files.read_file(root_id, path)
        with pytest.raises(DesktopFilesystemError):
            files.list_directory(root_id, "parent")
    finally:
        files.close()


def test_limits_directory_entries_and_closes_descriptors(tmp_path: Path) -> None:
    """Listings have a finite size and close disables later access."""
    root = tmp_path / "root"
    root.mkdir()
    for number in range(201):
        (root / f"{number:03}").write_text("x")
    files = DesktopFilesystem((root,))
    root_id = files.list_folders()["folders"][0]["id"]
    reply = files.list_directory(root_id)
    assert len(reply["entries"]) == 200
    assert reply["truncated"] is True
    files.close()
    with pytest.raises(DesktopFilesystemError):
        files.list_directory(root_id)


def test_oversized_offset_is_rejected_as_provider_error(tmp_path: Path) -> None:
    """An integer beyond OS offset range yields a controlled provider error."""
    (tmp_path / "file").write_text("text")
    files = DesktopFilesystem((tmp_path,))
    try:
        root_id = files.list_folders()["folders"][0]["id"]
        with pytest.raises(DesktopFilesystemError):
            files.read_file(root_id, "file", 2**128)
    finally:
        files.close()


def test_vanished_entry_is_skipped_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry that disappears between the scan and its stat is skipped, not fatal to the listing."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "gone").write_text("x")
    (root / "kept").write_text("x")
    original_stat = os.DirEntry.stat

    def flaky_stat(entry: os.DirEntry[str], *, follow_symlinks: bool = True) -> os.stat_result:
        if entry.name == "gone":
            message = "vanished between scan and stat"
            raise FileNotFoundError(message)
        return original_stat(entry, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os.DirEntry, "stat", flaky_stat)
    files = DesktopFilesystem((root,))
    try:
        root_id = files.list_folders()["folders"][0]["id"]
        assert files.list_directory(root_id)["entries"] == [{"name": "kept", "type": "file"}]
    finally:
        files.close()


def test_symlink_loop_runtime_error_still_closes_earlier_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink loop reported as RuntimeError (Python 3.12) closes descriptors already opened."""
    good = tmp_path / "good"
    good.mkdir()
    looping = tmp_path / "looping"
    looping.mkdir()
    original_resolve = Path.resolve
    original_close = os.close
    closed_descriptors: list[int] = []

    def close_and_record(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)

    def flaky_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == looping:
            message = "Symlink loop"
            raise RuntimeError(message)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)
    monkeypatch.setattr(os, "close", close_and_record)
    with pytest.raises(DesktopFilesystemError) as exc_info:
        DesktopFilesystem((good, looping))
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert len(closed_descriptors) == 1
