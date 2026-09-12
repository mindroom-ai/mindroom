"""Historical file edits must retain validated replacement content."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock

import nio
import pytest
from nio.crypto.attachments import encrypt_attachment

from mindroom.matrix.conversation_hydration import _readable_event


def _file_edit() -> dict:
    return {
        "type": "m.room.message",
        "event_id": "$edit",
        "sender": "@alice:example.org",
        "origin_server_ts": 1,
        "content": {
            "msgtype": "m.file",
            "body": "* preview",
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            "m.new_content": {
                "msgtype": "m.file",
                "body": "preview",
                "url": "mxc://example.org/full-content",
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            },
        },
    }


@pytest.mark.parametrize("encrypted", [False, True])
def test_legacy_file_edit_is_readable_without_changing_source(encrypted: bool) -> None:
    """Only parser fallback fields change; the exact authored source survives."""
    source = _file_edit()
    if encrypted:
        replacement = source["content"]["m.new_content"]
        _, descriptor = encrypt_attachment(b"complete sidecar")
        replacement["file"] = {**descriptor, "url": replacement.pop("url")}
    original = deepcopy(source)
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.BadEvent)
    restored = _readable_event(Mock(spec=nio.AsyncClient), event)
    assert isinstance(restored, nio.RoomEncryptedFile if encrypted else nio.RoomMessageFile)
    assert restored.source == original == source


@pytest.mark.parametrize("damage", ["target", "replacement", "url", "file", "sender", "original"])
def test_unrelated_malformed_file_payload_remains_unreadable(damage: str) -> None:
    """Malformed replacement or envelope fields must still fail validation."""
    source = _file_edit()
    if damage == "target":
        source["content"]["m.relates_to"]["event_id"] = 1
    elif damage == "replacement":
        source["content"]["m.new_content"] = []
    elif damage == "url":
        source["content"]["m.new_content"]["url"] = 1
    elif damage == "file":
        replacement = source["content"]["m.new_content"]
        replacement["file"] = {"url": replacement.pop("url")}
    elif damage == "sender":
        source.pop("sender")
    else:
        source["content"].pop("m.relates_to")
    assert _readable_event(Mock(spec=nio.AsyncClient), nio.Event.parse_event(source)) is None
