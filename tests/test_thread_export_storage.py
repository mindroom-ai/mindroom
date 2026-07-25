"""Filesystem-boundary tests for thread exports."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.thread_export.models import ThreadExportRoom
from mindroom.thread_export.storage import (
    _ROOT_MARKER_FILENAME,
    _ROOT_MARKER_TEXT,
    _safe_path_segment,
    _UnsafeThreadExportPathError,
    prepare_export_root,
    reconcile_room_directories,
    remove_room_export,
    remove_stale_thread_exports,
    room_has_thread_exports,
    write_room_index,
    write_thread_payload,
)

if TYPE_CHECKING:
    from pathlib import Path


def _room(key: str = "lobby") -> ThreadExportRoom:
    return ThreadExportRoom(
        key=key,
        room_id=f"!{key}:localhost",
        alias=f"#{key}:localhost",
        name=key.title(),
    )


def _mark_export_root(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / _ROOT_MARKER_FILENAME).write_text(_ROOT_MARKER_TEXT, encoding="utf-8")


def _thread_filename(thread_id: str) -> str:
    return f"{_safe_path_segment(thread_id)}.yaml"


def test_safe_path_segment_blocks_dot_directory_segments() -> None:
    """Path segments should not allow current or parent directory traversal."""
    assert _safe_path_segment(".") == "%2E"
    assert _safe_path_segment("..") == "%2E%2E"
    assert _safe_path_segment("%2E") == "%252E"


def test_exporter_marks_a_new_empty_root_automatically(tmp_path: Path) -> None:
    """Preparing a new root should install ownership without operator ceremony."""
    output_dir = tmp_path / "missing" / "thread_exports"

    prepare_export_root(output_dir)

    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


def test_exporter_marks_a_recognizable_legacy_root_automatically(tmp_path: Path) -> None:
    """A markerless tree containing only known export shapes should be claimed."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    (room_dir / _thread_filename("$thread:localhost")).write_text("version: 1\n", encoding="utf-8")

    prepare_export_root(output_dir)

    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


def test_exporter_replaces_an_invalid_marker_on_a_recognizable_legacy_root(tmp_path: Path) -> None:
    """An invalid reserved marker should not prevent adoption of an otherwise recognizable tree."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    (output_dir / _ROOT_MARKER_FILENAME).write_text("invalid\n", encoding="utf-8")

    prepare_export_root(output_dir)

    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


def test_exporter_ignores_its_atomic_write_residue_when_claiming_a_legacy_root(tmp_path: Path) -> None:
    """Exact exporter temp files should not strand an otherwise recognizable legacy tree."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    room_temp = room_dir / f".index.json.{'a' * 32}.tmp"
    root_temp = output_dir / f".{_ROOT_MARKER_FILENAME}.{'b' * 32}.tmp"
    room_temp.write_text('{"version":', encoding="utf-8")
    root_temp.write_text('{"format":', encoding="utf-8")

    prepare_export_root(output_dir)

    assert room_temp.read_text(encoding="utf-8") == '{"version":'
    assert root_temp.read_text(encoding="utf-8") == '{"format":'
    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


def test_unrecognized_root_is_not_marked(tmp_path: Path) -> None:
    """An ordinary directory should fail closed instead of gaining export ownership."""
    output_dir = tmp_path / "documents"
    output_dir.mkdir()
    keep = output_dir / "keep.txt"
    keep.write_text("private", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unowned thread export root") as error:
        prepare_export_root(output_dir)

    assert repr(_ROOT_MARKER_TEXT) in str(error.value)
    assert keep.read_text(encoding="utf-8") == "private"
    assert not (output_dir / _ROOT_MARKER_FILENAME).exists()


def test_markerless_destructive_operations_fail_closed(
    tmp_path: Path,
) -> None:
    """Recognizable legacy contents still require the marker before deletion."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")

    with (
        patch("mindroom.thread_export.storage.logger.warning") as warning,
        pytest.raises(RuntimeError, match="unowned thread export root"),
    ):
        reconcile_room_directories(output_dir, set())

    assert room_dir.is_dir()
    warning.assert_called_once_with(
        "Refusing destructive operation on markerless thread export root",
        output_dir=str(output_dir),
    )


def test_reconcile_deletes_only_recognizable_room_directories(
    tmp_path: Path,
) -> None:
    """Root reconciliation should preserve unrelated files and directories."""
    output_dir = tmp_path / "thread_exports"
    _mark_export_root(output_dir)
    stale_room = output_dir / "stale"
    stale_room.mkdir()
    (stale_room / "index.json").write_text("{}\n", encoding="utf-8")
    unrelated_dir = output_dir / "private"
    unrelated_dir.mkdir()
    (unrelated_dir / "keep.txt").write_text("private", encoding="utf-8")
    unrelated_file = output_dir / "notes.txt"
    unrelated_file.write_text("keep", encoding="utf-8")

    with patch("mindroom.thread_export.storage.logger.warning") as warning:
        reconcile_room_directories(output_dir, set())

    assert not stale_room.exists()
    assert unrelated_dir.is_dir()
    assert unrelated_file.read_text(encoding="utf-8") == "keep"
    assert warning.call_count == 2
    assert all(call.args == ("Leaving unrecognized thread export entry untouched",) for call in warning.call_args_list)


def test_stale_pruning_deletes_only_thread_id_shaped_yaml(
    tmp_path: Path,
) -> None:
    """Thread pruning should leave unrelated YAML and non-regular entries untouched."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    kept = room_dir / _thread_filename("$kept:localhost")
    stale = room_dir / _thread_filename("$stale:localhost")
    unrelated = room_dir / "notes.yaml"
    kept.write_text("kept", encoding="utf-8")
    stale.write_text("stale", encoding="utf-8")
    unrelated.write_text("private", encoding="utf-8")

    with patch("mindroom.thread_export.storage.logger.warning") as warning:
        assert remove_stale_thread_exports(output_dir, _room(), ["$kept:localhost"]) is True

    assert kept.is_file()
    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "private"
    warning.assert_called_once()
    assert warning.call_args.args == ("Leaving unrecognized thread export entry untouched",)


def test_room_removal_reports_a_present_unrecognized_directory(
    tmp_path: Path,
) -> None:
    """A present room with no exporter-owned files should fail instead of looking absent."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    keep = room_dir / "keep.txt"
    keep.write_text("private", encoding="utf-8")

    with (
        patch("mindroom.thread_export.storage.logger.warning") as warning,
        pytest.raises(RuntimeError, match="unrecognized thread export room"),
    ):
        remove_room_export(output_dir, _room())

    assert keep.read_text(encoding="utf-8") == "private"
    warning.assert_called_once()
    assert warning.call_args.args == ("Leaving unrecognized thread export entry untouched",)


def test_recognizable_room_removal_still_retracts_data(tmp_path: Path) -> None:
    """A marked room directory with an index remains retractable."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")

    remove_room_export(output_dir, _room())
    assert not room_dir.exists()


@pytest.mark.parametrize("full_reconciliation", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
def test_room_retraction_removes_only_exporter_data_when_unknown_entries_remain(
    tmp_path: Path,
    *,
    full_reconciliation: bool,
    indexed: bool,
) -> None:
    """Exact and full retraction must preserve unknown entries even beside an index."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    thread_file = room_dir / _thread_filename("$thread:localhost")
    index_file = room_dir / "index.json"
    temp_file = room_dir / f".index.json.{'a' * 32}.tmp"
    unknown_file = room_dir / "keep.txt"
    thread_file.write_text("version: 1\n", encoding="utf-8")
    temp_file.write_text('{"version":', encoding="utf-8")
    if indexed:
        index_file.write_text("{}\n", encoding="utf-8")
    unknown_file.write_text("private", encoding="utf-8")

    if full_reconciliation:
        reconcile_room_directories(output_dir, set())
    else:
        remove_room_export(output_dir, _room())

    assert not thread_file.exists()
    assert not index_file.exists()
    assert not temp_file.exists()
    assert unknown_file.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("full_reconciliation", [False, True])
@pytest.mark.parametrize("has_partial_thread", [False, True])
def test_room_retraction_removes_empty_exporter_residue_idempotently(
    tmp_path: Path,
    *,
    full_reconciliation: bool,
    has_partial_thread: bool,
) -> None:
    """An empty or newly emptied canonical room directory should retract cleanly twice."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    if has_partial_thread:
        (room_dir / _thread_filename("$thread:localhost")).write_text("version: 1\n", encoding="utf-8")

    with patch("mindroom.thread_export.storage.logger.warning") as warning:
        for _ in range(2):
            if full_reconciliation:
                reconcile_room_directories(output_dir, set())
            else:
                remove_room_export(output_dir, _room())

    assert not room_dir.exists()
    warning.assert_not_called()


def test_symlinked_export_root_cannot_write_or_reconcile_outside_workspace(tmp_path: Path) -> None:
    """A final output-directory symlink must not redirect writes or deletion."""
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("secret", encoding="utf-8")
    output_dir = tmp_path / "thread_exports"
    output_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export root"):
        write_thread_payload(output_dir, _room(), "$thread:localhost", {"version": 1})
    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export root"):
        reconcile_room_directories(output_dir, set())

    assert keep.read_text(encoding="utf-8") == "secret"
    assert output_dir.is_symlink()


def test_symlinked_room_directory_is_never_followed_or_removed(tmp_path: Path) -> None:
    """Room writes, pruning, and exact retraction should reject a room symlink."""
    output_dir = tmp_path / "thread_exports"
    _mark_export_root(output_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("secret", encoding="utf-8")
    (outside / "index.json").write_text("{}\n", encoding="utf-8")
    room_dir = output_dir / "lobby"
    room_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export room directory"):
        write_thread_payload(output_dir, _room(), "$thread:localhost", {"version": 1})
    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export room directory"):
        remove_stale_thread_exports(output_dir, _room(), [])
    with pytest.raises(_UnsafeThreadExportPathError, match="symlinked thread export room directory"):
        remove_room_export(output_dir, _room())

    assert room_dir.is_symlink()
    assert keep.read_text(encoding="utf-8") == "secret"


def test_room_key_cannot_collide_with_export_root_marker(tmp_path: Path) -> None:
    """A valid room key equal to the marker should use a separate directory."""
    output_dir = tmp_path / "thread_exports"
    room = _room(_ROOT_MARKER_FILENAME)

    write_thread_payload(output_dir, room, "$thread:localhost", {"version": 1})
    write_room_index(output_dir, room)

    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT
    assert (output_dir / "%2Emindroom-thread-exports" / "index.json").is_file()


def test_room_export_query_ignores_unrecognized_yaml(tmp_path: Path) -> None:
    """Only Matrix-thread-shaped regular YAML files count as existing exports."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    (room_dir / "notes.yaml").write_text("private", encoding="utf-8")

    assert room_has_thread_exports(output_dir, _room()) is False

    (room_dir / _thread_filename("$thread:localhost")).write_text("version: 1\n", encoding="utf-8")
    assert room_has_thread_exports(output_dir, _room()) is True
