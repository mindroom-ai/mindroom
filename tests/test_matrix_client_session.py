"""Tests for MindRoom-specific Matrix client behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.matrix.client_session import _MindRoomAsyncClient

if TYPE_CHECKING:
    import pytest


def test_encryption_exposes_only_mindroom_stream_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encrypted stream events expose status but no private message fields."""
    relation = {"event_id": "$original:example.org", "rel_type": "m.replace"}

    def fake_encrypt(
        _client: nio.AsyncClient,
        _room_id: str,
        _message_type: str,
        _content: dict[Any, Any],
    ) -> tuple[str, dict[str, Any]]:
        return "m.room.encrypted", {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "encrypted payload",
            "m.relates_to": relation,
        }

    monkeypatch.setattr(nio.AsyncClient, "encrypt", fake_encrypt)
    client = _MindRoomAsyncClient("https://example.org", "@mindroom_agent:example.org")

    message_type, encrypted_content = client.encrypt(
        "!room:example.org",
        "m.room.message",
        {
            "body": "private answer text",
            "m.mentions": {"user_ids": ["@private:example.org"]},
            "msgtype": "m.notice",
            STREAM_STATUS_KEY: "streaming",
        },
    )

    assert message_type == "m.room.encrypted"
    assert encrypted_content == {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "encrypted payload",
        "m.relates_to": relation,
        STREAM_STATUS_KEY: "streaming",
    }


def test_encryption_does_not_add_metadata_to_ordinary_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary encrypted messages retain nio's standard envelope."""

    def fake_encrypt(
        _client: nio.AsyncClient,
        _room_id: str,
        _message_type: str,
        _content: dict[Any, Any],
    ) -> tuple[str, dict[str, str]]:
        return "m.room.encrypted", {"ciphertext": "encrypted payload"}

    monkeypatch.setattr(nio.AsyncClient, "encrypt", fake_encrypt)
    client = _MindRoomAsyncClient("https://example.org", "@mindroom_agent:example.org")

    _, encrypted_content = client.encrypt(
        "!room:example.org",
        "m.room.message",
        {"body": "private answer text", "msgtype": "m.text"},
    )

    assert encrypted_content == {"ciphertext": "encrypted payload"}
