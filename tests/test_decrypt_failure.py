"""Tests for undecryptable Megolm event handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.exceptions import LocalProtocolError

from mindroom.matrix.decrypt_failure import handle_decrypt_failure


def _megolm_event(session_id: str = "session123") -> nio.MegolmEvent:
    event = nio.MegolmEvent.from_dict(
        {
            "event_id": "$undecryptable:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1700000000000,
            "type": "m.room.encrypted",
            "room_id": "!room:localhost",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "cipher",
                "sender_key": "sender_key",
                "session_id": session_id,
                "device_id": "DEVICE1",
            },
        },
    )
    assert isinstance(event, nio.MegolmEvent)
    return event


def _mock_client(outgoing_key_requests: dict | None = None) -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.outgoing_key_requests = outgoing_key_requests or {}
    return client


def _mock_room() -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    return room


@pytest.mark.asyncio
async def test_handle_decrypt_failure_requests_room_key() -> None:
    """An undecryptable event should trigger one room-key request."""
    client = _mock_client()
    event = _megolm_event()

    await handle_decrypt_failure(client, _mock_room(), event)

    client.request_room_key.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_handle_decrypt_failure_skips_already_requested_session() -> None:
    """A session with an outgoing key request should not be requested again."""
    client = _mock_client(outgoing_key_requests={"session123": MagicMock()})
    event = _megolm_event()

    await handle_decrypt_failure(client, _mock_room(), event)

    client.request_room_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_decrypt_failure_tolerates_concurrent_key_request() -> None:
    """A racing duplicate key request should not raise out of the handler."""
    client = _mock_client()
    client.request_room_key.side_effect = LocalProtocolError("already requested")

    await handle_decrypt_failure(client, _mock_room(), _megolm_event())

    client.request_room_key.assert_awaited_once()
