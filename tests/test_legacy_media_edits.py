"""Historical file edits must retain validated replacement content."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock

import nio
import pytest

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


def test_legacy_file_edit_is_readable_without_changing_source() -> None:
    """Only parser fallback fields change; the exact authored source survives."""
    source = _file_edit()
    original = deepcopy(source)
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.BadEvent)
    restored = _readable_event(Mock(spec=nio.AsyncClient), event)
    assert isinstance(restored, nio.RoomMessageFile)
    assert restored.source == original == source


@pytest.mark.parametrize("damage", ["target", "replacement", "url", "sender", "original"])
def test_unrelated_malformed_file_payload_remains_unreadable(damage: str) -> None:
    """Malformed replacement or envelope fields must still fail validation."""
    source = _file_edit()
    if damage == "target":
        source["content"]["m.relates_to"]["event_id"] = 1
    elif damage == "replacement":
        source["content"]["m.new_content"] = []
    elif damage == "url":
        source["content"]["m.new_content"]["url"] = 1
    elif damage == "sender":
        source.pop("sender")
    else:
        source["content"].pop("m.relates_to")
    assert _readable_event(Mock(spec=nio.AsyncClient), nio.Event.parse_event(source)) is None
