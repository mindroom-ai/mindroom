"""Tests for attachment persistence helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.attachments import (
    attachment_id_for_event,
    attachment_records_to_media,
    load_attachment,
    merge_attachment_ids,
    parse_attachment_ids_from_event_source,
    register_local_attachment,
    resolve_attachments,
)

if TYPE_CHECKING:
    from pathlib import Path


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


def test_merge_attachment_ids_preserves_order() -> None:
    """Merge should preserve first-seen ordering across sources."""
    merged = merge_attachment_ids(["att_1", "att_2"], ["att_2", "att_3"], ["att_1"])
    assert merged == ["att_1", "att_2", "att_3"]
