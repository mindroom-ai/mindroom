"""Tests for MindRoom-specific Matrix client behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.matrix.client_session import _MindRoomAsyncClient

if TYPE_CHECKING:
    from pathlib import Path

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


def test_explicit_zero_one_time_key_count_requests_replenishment(tmp_path: Path) -> None:
    """A drained server OTK pool must make nio upload replacement keys."""
    user_id = "@agent:example.org"
    client = _MindRoomAsyncClient(
        "https://example.org",
        user_id,
        device_id="AGENTDEVICE",
        store_path=str(tmp_path),
    )
    client.restore_login(user_id, "AGENTDEVICE", "access-token")
    client.load_store()
    assert client.olm is not None
    client.olm.account.shared = True
    client.olm.uploaded_key_count = 50

    response = nio.SyncResponse(
        next_batch="next",
        rooms=nio.Rooms(invite={}, join={}, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=7, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        account_data_events=[],
    )
    client._handle_olm_events(response)

    assert client.olm.uploaded_key_count == 0
    assert client.should_upload_keys
