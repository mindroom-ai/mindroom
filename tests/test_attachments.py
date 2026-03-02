"""Tests for attachment persistence helpers."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mindroom.attachment_media import resolve_attachment_media
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
    event_id = "$file_event"
    attachment_id = attachment_id_for_event(event_id)

    assert attachment_id == attachment_id_for_event(event_id)
    assert attachment_id.startswith("att_")
    assert len(attachment_id) == 28


def test_attachment_id_for_event_distinguishes_similar_strings() -> None:
    """Attachment IDs should differ for event IDs that only vary by punctuation."""
    assert attachment_id_for_event("$abc-123:localhost") != attachment_id_for_event("$abc_123:localhost")


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

    resolved_ids, _, files, videos = resolve_attachment_media(tmp_path, ["att_payload"])
    assert resolved_ids == ["att_payload"]
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


def test_register_local_attachment_returns_none_on_metadata_write_failure(tmp_path: Path) -> None:
    """Metadata write failures should return None instead of raising."""
    file_path = tmp_path / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = register_local_attachment(
            tmp_path,
            file_path,
            kind="file",
            attachment_id="att_write_fail",
            room_id="!room:localhost",
        )

    assert result is None
    # Metadata file should not exist
    metadata_path = tmp_path / "attachments" / "att_write_fail.json"
    assert not metadata_path.exists()


def test_register_local_attachment_throttles_cleanup_runs(tmp_path: Path) -> None:
    """Cleanup should run once per throttle window even with repeated registrations."""
    file_path = tmp_path / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    with (
        patch("mindroom.attachments._last_cleanup_time_by_storage_path", {}),
        patch("mindroom.attachments._cleanup_attachment_storage") as mock_cleanup,
    ):
        first = register_local_attachment(
            tmp_path,
            file_path,
            kind="file",
            attachment_id="att_first",
            room_id="!room:localhost",
        )
        second = register_local_attachment(
            tmp_path,
            file_path,
            kind="file",
            attachment_id="att_second",
            room_id="!room:localhost",
        )

    assert first is not None
    assert second is not None
    mock_cleanup.assert_called_once_with(tmp_path.resolve())


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


def test_register_local_attachment_prunes_expired_managed_media(tmp_path: Path) -> None:
    """Registering a new attachment should prune expired managed media records/files."""
    old_media_path = tmp_path / "incoming_media" / "old.bin"
    old_media_path.parent.mkdir(parents=True, exist_ok=True)
    old_media_path.write_bytes(b"old")

    old_record = register_local_attachment(
        tmp_path,
        old_media_path,
        kind="file",
        attachment_id="att_old",
        room_id="!room:localhost",
    )
    assert old_record is not None

    old_record_path = tmp_path / "attachments" / "att_old.json"
    old_payload = json.loads(old_record_path.read_text(encoding="utf-8"))
    old_payload["created_at"] = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    old_record_path.write_text(json.dumps(old_payload), encoding="utf-8")

    orphan_media_path = tmp_path / "incoming_media" / "orphan.bin"
    orphan_media_path.write_bytes(b"orphan")
    stale_timestamp = (datetime.now(UTC) - timedelta(days=45)).timestamp()
    orphan_media_path.touch()
    old_media_path.touch()

    os.utime(orphan_media_path, (stale_timestamp, stale_timestamp))
    os.utime(old_media_path, (stale_timestamp, stale_timestamp))

    fresh_media_path = tmp_path / "incoming_media" / "fresh.bin"
    fresh_media_path.write_bytes(b"fresh")
    # Reset the cleanup throttle so the second registration triggers cleanup.
    with patch("mindroom.attachments._last_cleanup_time_by_storage_path", {}):
        fresh_record = register_local_attachment(
            tmp_path,
            fresh_media_path,
            kind="file",
            attachment_id="att_fresh",
            room_id="!room:localhost",
        )
    assert fresh_record is not None

    assert load_attachment(tmp_path, "att_old") is None
    assert load_attachment(tmp_path, "att_fresh") is not None
    assert not old_media_path.exists()
    assert not orphan_media_path.exists()
    assert fresh_media_path.exists()


def test_register_local_attachment_prunes_expired_metadata_without_deleting_unmanaged_files(tmp_path: Path) -> None:
    """Cleanup should not delete files outside managed incoming_media storage."""
    external_file_path = tmp_path / "external.txt"
    external_file_path.write_text("external", encoding="utf-8")

    old_record = register_local_attachment(
        tmp_path,
        external_file_path,
        kind="file",
        attachment_id="att_external",
        room_id="!room:localhost",
    )
    assert old_record is not None

    old_record_path = tmp_path / "attachments" / "att_external.json"
    old_payload = json.loads(old_record_path.read_text(encoding="utf-8"))
    old_payload["created_at"] = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    old_record_path.write_text(json.dumps(old_payload), encoding="utf-8")

    fresh_media_path = tmp_path / "incoming_media" / "fresh.bin"
    fresh_media_path.parent.mkdir(parents=True, exist_ok=True)
    fresh_media_path.write_bytes(b"fresh")
    # Reset the cleanup throttle so the second registration triggers cleanup.
    with patch("mindroom.attachments._last_cleanup_time_by_storage_path", {}):
        fresh_record = register_local_attachment(
            tmp_path,
            fresh_media_path,
            kind="file",
            attachment_id="att_fresh2",
            room_id="!room:localhost",
        )
    assert fresh_record is not None

    assert load_attachment(tmp_path, "att_external") is None
    assert external_file_path.exists()
