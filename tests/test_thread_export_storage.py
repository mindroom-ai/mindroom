"""Filesystem-boundary tests for thread exports."""

from __future__ import annotations

import json
import os
import tracemalloc
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from mindroom import yaml_io
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.thread_export import clear_thread_export_root
from mindroom.thread_export import storage as thread_export_storage
from mindroom.thread_export.models import ThreadExportRoom
from mindroom.thread_export.storage import (
    _ROOT_MARKER_FILENAME,
    _ROOT_MARKER_TEXT,
    _safe_path_segment,
    _UnsafeThreadExportPathError,
    exported_content,
    prepare_export_root,
    reconcile_room_directories,
    remove_room_export,
    remove_stale_thread_exports,
    room_has_thread_exports,
    thread_payload,
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


def _write_thread_export(room_dir: Path, thread_id: str = "$thread:localhost") -> Path:
    """Write one structurally valid thread export, as ownership evidence requires."""
    path = room_dir / _thread_filename(thread_id)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "room": {"key": "lobby", "id": "!lobby:localhost", "name": "Lobby", "alias": "#lobby:localhost"},
                "thread": {"id": thread_id, "source": "matrix", "message_count": 0},
                "messages": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_clear_thread_export_root_removes_only_owned_content(tmp_path: Path) -> None:
    """Plugins can retract a target without owning path traversal or broad deletion."""
    output_dir = tmp_path / "agent" / "workspace" / "thread_exports"
    _mark_export_root(output_dir)
    room_dir = output_dir / "lobby"
    room_dir.mkdir()
    _write_thread_export(room_dir)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    note = output_dir / "operator-note.txt"
    note.write_text("keep", encoding="utf-8")

    clear_thread_export_root(output_dir, trusted_root=tmp_path)

    assert not room_dir.exists()
    assert (output_dir / _ROOT_MARKER_FILENAME).exists()
    assert note.read_text(encoding="utf-8") == "keep"

    prepare_export_root(output_dir, trusted_root=tmp_path)


def test_clear_thread_export_root_retains_empty_owned_directory(
    tmp_path: Path,
) -> None:
    """Cleanup retains the owned root so later exports can reuse it safely."""
    output_dir = tmp_path / "agent" / "workspace" / "thread_exports"
    _mark_export_root(output_dir)

    clear_thread_export_root(output_dir, trusted_root=tmp_path)

    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


def test_clear_thread_export_root_never_drops_ownership_before_cleanup_finishes(
    tmp_path: Path,
) -> None:
    """Cleanup must not expose a markerless root to concurrent writers."""
    output_dir = tmp_path / "agent" / "workspace" / "thread_exports"
    _mark_export_root(output_dir)

    with patch("mindroom.thread_export.storage.os.unlink") as unlink:
        clear_thread_export_root(output_dir, trusted_root=tmp_path)

    unlink.assert_not_called()
    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


def test_clear_thread_export_root_rejects_replaced_parent(tmp_path: Path) -> None:
    """Cleanup cannot follow an intermediate symlink installed after discovery."""
    instance_root = tmp_path / "private_instances" / "scope" / "agent"
    output_dir = instance_root / "workspace" / "thread_exports"
    _mark_export_root(output_dir)
    saved_instance_root = instance_root.with_name("agent-saved")
    instance_root.rename(saved_instance_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    instance_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="thread export root parent"):
        clear_thread_export_root(output_dir, trusted_root=tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (saved_instance_root / "workspace" / "thread_exports" / _ROOT_MARKER_FILENAME).exists()


def test_write_thread_payload_rejects_replaced_parent(tmp_path: Path) -> None:
    """A prepared target cannot be redirected before a later write."""
    instance_root = tmp_path / "private_instances" / "scope" / "agent"
    output_dir = instance_root / "workspace" / "thread_exports"
    prepare_export_root(output_dir, trusted_root=tmp_path)
    saved_instance_root = instance_root.with_name("agent-saved")
    instance_root.rename(saved_instance_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    instance_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="thread export root parent"):
        write_thread_payload(
            output_dir,
            _room(),
            "$thread:localhost",
            {"messages": []},
            trusted_root=tmp_path,
        )

    assert not (outside / "workspace" / "thread_exports").exists()
    assert (saved_instance_root / "workspace" / "thread_exports" / _ROOT_MARKER_FILENAME).exists()


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


def test_exporter_refuses_populated_unmarked_root_and_preserves_content(tmp_path: Path) -> None:
    """A populated markerless root must remain untouched until explicitly marked."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    existing_export = _write_thread_export(room_dir)
    before = existing_export.read_bytes()

    with pytest.raises(RuntimeError, match="unowned thread export root"):
        prepare_export_root(output_dir)

    assert existing_export.read_bytes() == before
    assert not (output_dir / _ROOT_MARKER_FILENAME).exists()


def test_a_stray_index_json_does_not_make_a_project_directory_adoptable(tmp_path: Path) -> None:
    """A build directory holding index.json must not authorize adopting its parent."""
    project = tmp_path / "my-project"
    build_dir = project / "dist"
    build_dir.mkdir(parents=True)
    (build_dir / "index.json").write_text('{"build":1}\n', encoding="utf-8")
    source = project / "README.md"
    source.write_text("my project", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unowned thread export root"):
        prepare_export_root(project)

    assert not (project / _ROOT_MARKER_FILENAME).exists()
    assert (build_dir / "index.json").read_text(encoding="utf-8") == '{"build":1}\n'
    assert source.read_text(encoding="utf-8") == "my project"


def test_a_thread_shaped_filename_alone_does_not_make_a_directory_adoptable(tmp_path: Path) -> None:
    """Ownership evidence must read the document, not trust a percent-encoded filename."""
    notes = tmp_path / "notes"
    archive = notes / "archive"
    archive.mkdir(parents=True)
    decoy = archive / _thread_filename("$notes")
    decoy.write_text("shopping:\n  - milk\n  - eggs\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unowned thread export root"):
        prepare_export_root(notes)

    assert not (notes / _ROOT_MARKER_FILENAME).exists()
    assert decoy.read_text(encoding="utf-8") == "shopping:\n  - milk\n  - eggs\n"


def test_unrecognized_root_is_not_marked(tmp_path: Path) -> None:
    """An ordinary directory should fail closed instead of gaining export ownership."""
    output_dir = tmp_path / "documents"
    output_dir.mkdir()
    keep = output_dir / "keep.txt"
    keep.write_text("private", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unowned thread export root") as error:
        prepare_export_root(output_dir)

    message = str(error.value)
    assert _ROOT_MARKER_TEXT.strip() in message
    assert "\\n" not in message, "the adoption instruction must be copy-pasteable, not a Python repr"
    assert keep.read_text(encoding="utf-8") == "private"
    assert not (output_dir / _ROOT_MARKER_FILENAME).exists()


def test_markerless_destructive_operations_fail_closed(
    tmp_path: Path,
) -> None:
    """Populated markerless contents still require the marker before deletion."""
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


def test_room_removal_preserves_a_present_unrecognized_directory(
    tmp_path: Path,
) -> None:
    """A room holding only foreign files should be preserved and reported, not fail forever."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    keep = room_dir / "keep.txt"
    keep.write_text("private", encoding="utf-8")

    with patch("mindroom.thread_export.storage.logger.warning") as warning:
        for _ in range(2):
            remove_room_export(output_dir, _room())

    assert keep.read_text(encoding="utf-8") == "private"
    assert warning.call_count == 2
    assert all(call.args == ("Leaving unrecognized thread export entry untouched",) for call in warning.call_args_list)


def test_room_retraction_is_idempotent_beside_a_foreign_file(tmp_path: Path) -> None:
    """Repeat retraction of a partially cleaned room must stay a quiet no-op."""
    output_dir = tmp_path / "thread_exports"
    room_dir = output_dir / "lobby"
    room_dir.mkdir(parents=True)
    _mark_export_root(output_dir)
    (room_dir / "index.json").write_text("{}\n", encoding="utf-8")
    (room_dir / _thread_filename("$thread:localhost")).write_text("version: 1\n", encoding="utf-8")
    keep = room_dir / "notes.txt"
    keep.write_text("operator note", encoding="utf-8")

    for _ in range(3):
        remove_room_export(output_dir, _room())

    assert sorted(entry.name for entry in room_dir.iterdir()) == ["notes.txt"]
    assert keep.read_text(encoding="utf-8") == "operator note"


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


def test_thread_export_yaml_with_aliases_is_refused(tmp_path: Path) -> None:
    """Thread exports sit in the worker-writable workspace, so an aliased file is left out of the index and replaced."""
    output_dir = tmp_path / "thread_exports"
    room = _room()
    payload: dict[str, object] = {
        "version": 1,
        "thread": {"id": "$aliased:localhost", "source": "matrix", "summary": "$aliased:localhost"},
        "messages": [],
    }
    write_thread_payload(output_dir, room, "$aliased:localhost", payload)
    planted = output_dir / "lobby" / _thread_filename("$aliased:localhost")
    planted.write_text(
        'version: 1\nthread:\n  id: &id "$aliased:localhost"\n  source: matrix\n  summary: *id\nmessages: []\n',
        encoding="utf-8",
    )

    write_room_index(output_dir, room)
    assert json.loads((output_dir / "lobby" / "index.json").read_text(encoding="utf-8"))["threads"] == []

    assert write_thread_payload(output_dir, room, "$aliased:localhost", payload) is True
    assert "*id" not in planted.read_text(encoding="utf-8")


def test_thread_export_yaml_with_too_many_nodes_is_refused(tmp_path: Path) -> None:
    """A planted file is refused before its node graph, hundreds of bytes per node, fills the primary's memory."""
    output_dir = tmp_path / "thread_exports"
    room = _room()
    payload: dict[str, object] = {
        "version": 1,
        "thread": {"id": "$planted:localhost", "source": "matrix"},
        "messages": [],
    }
    write_thread_payload(output_dir, room, "$planted:localhost", payload)
    planted = output_dir / "lobby" / _thread_filename("$planted:localhost")
    planted.write_text(
        'version: 1\nthread:\n  id: "$planted:localhost"\n  source: matrix\n'
        f"messages:\n- sender: '@planted:localhost'\n  padding: [{'a,' * 250_000}a]\n",
        encoding="utf-8",
    )

    write_room_index(output_dir, room)
    [entry] = json.loads((output_dir / "lobby" / "index.json").read_text(encoding="utf-8"))["threads"]
    assert entry["thread_id"] == "$planted:localhost"
    assert entry["participants"] == []

    assert write_thread_payload(output_dir, room, "$planted:localhost", payload) is True
    assert "padding" not in planted.read_text(encoding="utf-8")


def test_thread_too_long_to_parse_whole_stays_indexed_and_is_not_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary's own export of a thread above the YAML node limit is indexed and left alone while unchanged."""
    monkeypatch.setattr(yaml_io, "_MAX_UNTRUSTED_NODES", 500)
    output_dir = tmp_path / "thread_exports"
    room = _room()
    messages = [
        {"event_id": f"$event-{index}:localhost", "sender": "@user:localhost", "timestamp": index + 1, "body": "hi"}
        for index in range(100)
    ]

    def payload(exported_at: str) -> dict[str, object]:
        return {
            "version": 1,
            "room": {"key": "lobby", "id": "!lobby:localhost", "name": "Lobby", "alias": "#lobby:localhost"},
            "thread": {"id": "$long:localhost", "source": "matrix", "exported_at": exported_at, "message_count": 100},
            "messages": messages,
        }

    assert write_thread_payload(output_dir, room, "$long:localhost", payload("2026-10-01T00:00:00+00:00")) is True
    write_room_index(output_dir, room)
    [entry] = json.loads((output_dir / "lobby" / "index.json").read_text(encoding="utf-8"))["threads"]
    assert entry["thread_id"] == "$long:localhost"
    assert entry["message_count"] == 100

    with patch.object(thread_export_storage, "_atomic_write_at", side_effect=AssertionError("rewrote an export")):
        assert write_thread_payload(output_dir, room, "$long:localhost", payload("2026-10-02T00:00:00+00:00")) is False
        write_room_index(output_dir, room)


def test_thread_above_the_read_cap_stays_indexed_and_is_not_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread file above the read cap is still written, indexed from its header, and left alone while unchanged."""
    monkeypatch.setattr(thread_export_storage, "MAX_READ_BYTES", 4_096)
    output_dir = tmp_path / "thread_exports"
    room = _room()

    def payload(exported_at: str) -> dict[str, object]:
        return {
            "version": 1,
            "room": {"key": "lobby", "id": "!lobby:localhost", "name": "Lobby", "alias": "#lobby:localhost"},
            "thread": {"id": "$long:localhost", "source": "matrix", "exported_at": exported_at, "message_count": 1},
            "messages": [
                {"event_id": "$e:localhost", "sender": "@user:localhost", "timestamp": 1, "body": "x" * 5_000},
            ],
        }

    assert (
        write_thread_payload(output_dir, room, "$long:localhost", payload("2026-10-01T00:00:00.123456+00:00")) is True
    )
    write_room_index(output_dir, room)
    [entry] = json.loads((output_dir / "lobby" / "index.json").read_text(encoding="utf-8"))["threads"]
    assert entry == {
        "file": _thread_filename("$long:localhost"),
        "thread_id": "$long:localhost",
        "message_count": 1,
        "participants": [],
    }

    # The new export is shorter than the existing file by its timestamp alone, and the comparison still reads all of it.
    with patch.object(thread_export_storage, "_atomic_write_at", side_effect=AssertionError("rewrote an export")):
        assert write_thread_payload(output_dir, room, "$long:localhost", payload("2026-10-02T00:00:00+00:00")) is False
        write_room_index(output_dir, room)


def test_thread_whose_file_passes_the_cap_fails_and_keeps_its_previous_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """libyaml writes each emoji in ten bytes, so a thread the message guard admits can still be refused for its file size."""
    output_dir = tmp_path / "thread_exports"
    room = _room()

    def payload(body: str) -> dict[str, object]:
        return {
            "version": 1,
            "thread": {"id": "$emoji:localhost", "source": "matrix"},
            "messages": [{"event_id": "$e:localhost", "sender": "@user:localhost", "body": body}],
        }

    assert write_thread_payload(output_dir, room, "$emoji:localhost", payload("short")) is True
    path = output_dir / "lobby" / _thread_filename("$emoji:localhost")
    previous = path.read_bytes()
    monkeypatch.setattr(thread_export_storage, "_MAX_THREAD_FILE_BYTES", 4_096)

    # Four kilobytes of message JSON become ten kilobytes of YAML.
    with pytest.raises(RuntimeError, match="too large to export"):
        write_thread_payload(output_dir, room, "$emoji:localhost", payload("\U0001f600" * 1_000))
    assert path.read_bytes() == previous


def test_unchanged_thread_check_holds_one_copy_of_each_export(tmp_path: Path) -> None:
    """Deciding that a large thread is unchanged holds its new and existing exports, not stripped copies of both."""
    output_dir = tmp_path / "thread_exports"
    room = _room()

    def payload(exported_at: str) -> dict[str, object]:
        return {
            "version": 1,
            "thread": {"id": "$large:localhost", "source": "matrix", "exported_at": exported_at, "message_count": 1},
            "messages": [{"event_id": "$e:localhost", "sender": "@user:localhost", "body": "x" * (4 << 20)}],
        }

    assert write_thread_payload(output_dir, room, "$large:localhost", payload("2026-10-01T00:00:00.123456+00:00"))
    size = (output_dir / "lobby" / _thread_filename("$large:localhost")).stat().st_size
    unchanged = payload("2026-10-02T00:00:00+00:00")
    tracemalloc.start()
    try:
        assert write_thread_payload(output_dir, room, "$large:localhost", unchanged) is False
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The new export, the existing file, and the read's own buffers stay below four copies.
    assert peak < 4 * size


def test_exported_content_keeps_everything_a_thread_payload_writes() -> None:
    """A fetched thread drops the content its export never writes, and the written payload stays the same."""
    router = "@mindroom_router:localhost"
    contents: list[dict[str, object]] = [
        {
            "msgtype": "m.notice",
            "body": "Summary text",
            "formatted_body": "<p>Summary text</p>",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$root", "m.in_reply_to": {"event_id": "$root"}},
            "io.mindroom.thread_summary": {"version": 1, "summary": "Deploy fix"},
            "io.mindroom.stream_status": "completed",
            "padding": ["unused"] * 100,
        },
        {"msgtype": "m.text", "body": "later notice", "io.mindroom.thread_summary": {"version": 1}},
        {"body": "plain", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}},
    ]
    messages = [
        ResolvedVisibleMessage.from_message_data(
            {
                "sender": router,
                "body": str(content["body"]),
                "timestamp": index + 1,
                "event_id": f"$event-{index}",
                "content": content,
            },
            thread_id="$root",
            latest_event_id=f"$event-{index}",
        )
        for index, content in enumerate(contents)
    ]

    def payload() -> dict[str, object]:
        return thread_payload(
            room=_room(),
            thread_id="$root",
            messages=messages,
            exported_at=datetime(2026, 10, 1, tzinfo=UTC),
            trusted_sender_ids={router},
        )

    written = payload()
    for message in messages:
        message.content = exported_content(message)

    assert payload() == written
    assert [set(message.content) for message in messages] == [
        {"msgtype", "m.relates_to", "io.mindroom.thread_summary"},
        {"msgtype", "io.mindroom.thread_summary"},
        set(),
    ]


def test_room_index_rebuild_reads_its_newest_threads_within_a_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread files added to a room cannot make one rebuild read without end, and the newest threads stay indexed."""
    output_dir = tmp_path / "thread_exports"
    room = _room()

    def write(index: int) -> Path:
        thread_id = f"$thread-{index}:localhost"
        payload = {"version": 1, "thread": {"id": thread_id, "source": "matrix", "message_count": 0}, "messages": []}
        write_thread_payload(output_dir, room, thread_id, payload)
        return output_dir / "lobby" / _thread_filename(thread_id)

    paths = [write(index) for index in range(3)]
    for index, path in enumerate(paths):
        os.utime(path, ns=(index * 1_000_000_000, index * 1_000_000_000))
    monkeypatch.setattr(
        thread_export_storage,
        "_MAX_ROOM_INDEX_BYTES",
        paths[1].stat().st_size + paths[2].stat().st_size,
    )

    def indexed() -> tuple[list[str], list[str]]:
        index = json.loads((output_dir / "lobby" / "index.json").read_text(encoding="utf-8"))
        return sorted(entry["thread_id"] for entry in index["threads"]), index.get("unindexed_files", [])

    with patch("mindroom.thread_export.storage.logger.warning") as warning:
        write_room_index(output_dir, room)
        assert indexed() == (["$thread-1:localhost", "$thread-2:localhost"], [paths[0].name])
        assert paths[0].exists()

        # The files the budget left out are not drift, so an unchanged pass skips the rebuild.
        with patch.object(thread_export_storage, "_thread_index_entry", side_effect=AssertionError("reparsed")):
            write_room_index(output_dir, room, thread_files_changed=False)
        warning.assert_called_once()

        # An added or deleted file still rebuilds the index.
        added = write(3)
        write_room_index(output_dir, room, thread_files_changed=False)
        assert indexed() == (["$thread-2:localhost", "$thread-3:localhost"], sorted([paths[0].name, paths[1].name]))
        paths[0].unlink()
        added.unlink()
        write_room_index(output_dir, room, thread_files_changed=False)
        assert indexed() == (["$thread-1:localhost", "$thread-2:localhost"], [])


@pytest.mark.parametrize("filename", ["marker", "index"])
def test_export_reads_never_block_on_a_planted_fifo(tmp_path: Path, filename: str) -> None:
    """A FIFO agent code plants where an export file belongs is refused instead of blocking the primary."""
    os.mkfifo(tmp_path / ("marker" if filename == "marker" else "index.yaml"))
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if filename == "marker":
            (tmp_path / "marker").rename(tmp_path / thread_export_storage._ROOT_MARKER_FILENAME)
            assert thread_export_storage._has_valid_export_root_marker(root_fd) is False
        else:
            assert thread_export_storage._read_bytes_at(root_fd, "index.yaml", max_bytes=1_024) is None
    finally:
        os.close(root_fd)
