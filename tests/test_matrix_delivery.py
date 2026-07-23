"""Tests for Matrix delivery trust behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.client_delivery import build_edit_event_content, send_message_result


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


@pytest.mark.asyncio
async def test_send_message_result_ignores_unverified_devices() -> None:
    """Bots cannot interactively verify devices, so delivery always ignores device trust."""
    client = _mock_client()

    await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True


@pytest.mark.asyncio
async def test_send_message_result_ignores_unverified_devices_in_encrypted_room() -> None:
    """Encrypted-room sends must not be blocked by nio's device-trust checks."""
    client = _mock_client(encrypted=True)

    await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True


def test_edit_fallback_preserves_replacement_message_type() -> None:
    """A notice replacement must also be a notice to suppress edit mention pushes."""
    content = build_edit_event_content(
        event_id="$original:localhost",
        new_content={
            "body": "Streaming answer",
            "msgtype": "m.notice",
            "m.mentions": {"user_ids": ["@user:localhost"]},
        },
        new_text="Streaming answer",
    )

    assert content["msgtype"] == "m.notice"
    assert content["m.new_content"]["msgtype"] == "m.notice"
    assert content["m.mentions"] == {"user_ids": ["@user:localhost"]}
