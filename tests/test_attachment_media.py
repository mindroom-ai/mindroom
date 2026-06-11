"""Regression tests for attachment media conversion."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.media import Image
from agno.models.message import Message
from agno.run.messages import RunMessages

from mindroom.attachment_media import (
    _INLINE_MEDIA_RECORDS_BY_ID,
    _INLINE_MEDIA_RECORDS_BY_PATH,
    _MAX_INLINE_MEDIA_RECORDS,
    _remember_attachment_record,
    resolve_attachment_media,
)
from mindroom.attachments import AttachmentRecord, register_local_attachment
from mindroom.history.agno_team_patch import _dedupe_run_messages_inline_media

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_inline_media_caches() -> Iterator[None]:
    _INLINE_MEDIA_RECORDS_BY_ID.clear()
    _INLINE_MEDIA_RECORDS_BY_PATH.clear()
    yield
    _INLINE_MEDIA_RECORDS_BY_ID.clear()
    _INLINE_MEDIA_RECORDS_BY_PATH.clear()


def test_inline_media_record_caches_evict_oldest_entries(tmp_path: Path) -> None:
    for index in range(_MAX_INLINE_MEDIA_RECORDS + 50):
        _remember_attachment_record(
            AttachmentRecord(
                attachment_id=f"att_{index}",
                local_path=tmp_path / f"{index}.png",
                kind="image",
            ),
        )

    assert len(_INLINE_MEDIA_RECORDS_BY_ID) == _MAX_INLINE_MEDIA_RECORDS
    assert len(_INLINE_MEDIA_RECORDS_BY_PATH) == _MAX_INLINE_MEDIA_RECORDS
    assert list(_INLINE_MEDIA_RECORDS_BY_ID) == [f"att_{index}" for index in range(50, _MAX_INLINE_MEDIA_RECORDS + 50)]
    assert set(_INLINE_MEDIA_RECORDS_BY_ID).isdisjoint({f"att_{index}" for index in range(50)})
    assert all(str((tmp_path / f"{index}.png").resolve()) not in _INLINE_MEDIA_RECORDS_BY_PATH for index in range(50))


def test_resolve_attachment_media_caches_records_before_partition(tmp_path: Path) -> None:
    old_path = tmp_path / "old.png"
    new_path = tmp_path / "new.png"
    image_bytes = b"\x89PNG\r\n\x1a\nsame"
    old_path.write_bytes(image_bytes)
    new_path.write_bytes(image_bytes)
    old_record = register_local_attachment(
        tmp_path,
        old_path,
        kind="image",
        attachment_id="att_old_duplicate",
        mime_type="image/png",
    )
    new_record = register_local_attachment(
        tmp_path,
        new_path,
        kind="image",
        attachment_id="att_new_duplicate",
        mime_type="image/png",
    )
    assert old_record is not None
    assert new_record is not None

    resolved_records, _, images, _, _ = resolve_attachment_media(
        tmp_path,
        [old_record.attachment_id, new_record.attachment_id],
        current_attachment_ids={new_record.attachment_id},
    )

    assert [record.attachment_id for record in resolved_records] == [
        old_record.attachment_id,
        new_record.attachment_id,
    ]
    assert [image.id for image in images] == [new_record.attachment_id]
    assert _INLINE_MEDIA_RECORDS_BY_ID[old_record.attachment_id].local_path == old_record.local_path
    assert _INLINE_MEDIA_RECORDS_BY_ID[new_record.attachment_id].local_path == new_record.local_path
    assert _INLINE_MEDIA_RECORDS_BY_PATH[str(old_record.local_path.resolve())].attachment_id == old_record.attachment_id
    assert _INLINE_MEDIA_RECORDS_BY_PATH[str(new_record.local_path.resolve())].attachment_id == new_record.attachment_id

    old_message = Message(
        role="user",
        images=[
            Image(
                id=old_record.attachment_id,
                filepath=old_record.local_path,
                mime_type=old_record.mime_type,
            ),
        ],
        from_history=True,
    )
    new_message = Message(
        role="user",
        images=[
            Image(
                id=new_record.attachment_id,
                filepath=new_record.local_path,
                mime_type=new_record.mime_type,
            ),
        ],
    )
    run_messages = RunMessages(
        messages=[
            old_message,
            new_message,
        ],
        user_message=new_message,
    )

    _dedupe_run_messages_inline_media(run_messages)

    assert [image.id for message in run_messages.messages for image in (message.images or [])] == [
        new_record.attachment_id,
    ]
    assert old_message.images == []
    assert [image.id for image in (new_message.images or [])] == [new_record.attachment_id]
