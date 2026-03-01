"""Tests for attachment persistence helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mindroom.attachment_media import attachment_records_to_media, resolve_attachment_media
from mindroom.attachments import (
    attachment_id_for_event,
    filter_attachments_for_context,
    load_attachment,
    merge_attachment_ids,
    parse_attachment_ids_from_event_source,
    register_local_attachment,
    resolve_attachments,
)


def test_attachment_id_for_event_is_stable() -> None:
    """Event IDs should map to deterministic attachment IDs."""
    assert attachment_id_for_event("$file_event") == "att_fileevent"
    assert attachment_id_for_event("$file_event") == "att_fileevent"


def test_parse_attachment_ids_from_event_source_dedupes() -> None:
    """Parser should normalize attachment IDs and drop duplicates."""
    event_source = {
        "content": {
            "com.mindroom.attachment_ids": ["att_1", "att_1", "  att_2  ", 123, ""],
        },
    }
    attachment_ids = parse_attachment_ids_from_event_source(event_source)
    assert attachment_ids == ["att_1", "att_2"]


def test_register_resolve_and_convert_attachment(tmp_path: Path) -> None:
    """Registered attachments should resolve and convert to Agno media objects."""
    file_path = tmp_path / "payload.zip"
    file_path.write_bytes(b"PK\x03\x04")

    registered = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_payload",
        filename="payload.zip",
        mime_type="application/zip",
        room_id="!room:localhost",
        thread_id="$thread",
        source_event_id="$evt",
        sender="@user:localhost",
    )
    assert registered is not None

    loaded = load_attachment(tmp_path, "att_payload")
    assert loaded is not None
    assert loaded.attachment_id == "att_payload"
    assert loaded.local_path == file_path.resolve()

    resolved = resolve_attachments(tmp_path, ["att_payload", "att_missing"])
    assert [record.attachment_id for record in resolved] == ["att_payload"]

    _, files, videos = attachment_records_to_media(resolved)
    assert len(files) == 1
    assert files[0].filename == "payload.zip"
    assert str(files[0].filepath) == str(file_path.resolve())
    assert videos == []


def test_register_local_attachment_uses_unique_temp_metadata_paths(tmp_path: Path) -> None:
    """Repeated writes for the same attachment ID should not reuse temp metadata paths."""
    file_path = tmp_path / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    replace_sources: list[str] = []
    original_replace = Path.replace

    def tracked_replace(self: Path, target: Path) -> Path:
        replace_sources.append(str(self))
        return original_replace(self, target)

    with patch.object(Path, "replace", new=tracked_replace):
        first = register_local_attachment(
            tmp_path,
            file_path,
            kind="file",
            attachment_id="att_same",
            room_id="!room:localhost",
        )
        second = register_local_attachment(
            tmp_path,
            file_path,
            kind="file",
            attachment_id="att_same",
            room_id="!room:localhost",
        )

    assert first is not None
    assert second is not None
    assert len(replace_sources) == 2
    assert replace_sources[0] != replace_sources[1]
    assert replace_sources[0].endswith(".tmp")
    assert replace_sources[1].endswith(".tmp")


def test_merge_attachment_ids_preserves_order() -> None:
    """Merge should preserve first-seen ordering across sources."""
    merged = merge_attachment_ids(["att_1", "att_2"], ["att_2", "att_3"], ["att_1"])
    assert merged == ["att_1", "att_2", "att_3"]


def test_filter_attachments_for_context_enforces_room_and_thread(tmp_path: Path) -> None:
    """Thread mode should keep only exact room/thread matches."""
    file_path = tmp_path / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    matching = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_matching",
        room_id="!room:localhost",
        thread_id="$thread_a",
    )
    wrong_thread = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_wrong_thread",
        room_id="!room:localhost",
        thread_id="$thread_b",
    )
    wrong_room = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_wrong_room",
        room_id="!other:localhost",
        thread_id="$thread_a",
    )
    legacy_unscoped = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_legacy",
        room_id=None,
        thread_id="$thread_a",
    )
    assert matching is not None
    assert wrong_thread is not None
    assert wrong_room is not None
    assert legacy_unscoped is not None

    records = resolve_attachments(
        tmp_path,
        ["att_matching", "att_wrong_thread", "att_wrong_room", "att_legacy"],
    )
    allowed, rejected = filter_attachments_for_context(
        records,
        room_id="!room:localhost",
        thread_id="$thread_a",
    )

    assert [record.attachment_id for record in allowed] == ["att_matching"]
    assert rejected == ["att_wrong_thread", "att_wrong_room", "att_legacy"]


def test_filter_attachments_for_context_room_mode_rejects_threaded_ids(tmp_path: Path) -> None:
    """Room mode should reject attachments scoped to any specific thread."""
    file_path = tmp_path / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    room_scoped = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_room_scoped",
        room_id="!room:localhost",
        thread_id=None,
    )
    thread_scoped = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_thread_scoped",
        room_id="!room:localhost",
        thread_id="$thread_a",
    )
    assert room_scoped is not None
    assert thread_scoped is not None

    records = resolve_attachments(tmp_path, ["att_room_scoped", "att_thread_scoped"])
    allowed, rejected = filter_attachments_for_context(records, room_id="!room:localhost", thread_id=None)

    assert [record.attachment_id for record in allowed] == ["att_room_scoped"]
    assert rejected == ["att_thread_scoped"]


def test_resolve_attachment_media_drops_cross_thread_ids(tmp_path: Path) -> None:
    """Media resolution should enforce room/thread provenance on attachment IDs."""
    file_path = tmp_path / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    allowed = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_ok",
        room_id="!room:localhost",
        thread_id="$thread_a",
    )
    rejected = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_wrong_thread",
        room_id="!room:localhost",
        thread_id="$thread_b",
    )
    assert allowed is not None
    assert rejected is not None

    resolved_ids, _, files, _ = resolve_attachment_media(
        tmp_path,
        ["att_ok", "att_wrong_thread"],
        room_id="!room:localhost",
        thread_id="$thread_a",
    )

    assert resolved_ids == ["att_ok"]
    assert len(files) == 1
    assert str(files[0].filepath) == str(file_path.resolve())
