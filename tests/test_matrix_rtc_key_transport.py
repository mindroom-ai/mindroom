"""Tests for the encrypted to-device frame-key transport."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix_rtc.events import CallMember
from mindroom.matrix_rtc.key_transport import ToDeviceFrameKeyTransport

ROOM_ID = "!room:example.org"


def _member(user_id: str, device_id: str) -> CallMember:
    return CallMember(
        user_id=user_id,
        device_id=device_id,
        created_ts=0,
        expires_ms=10_000_000,
        membership_id=f"{user_id}:{device_id}",
    )


@pytest.mark.asyncio
async def test_failed_otk_claim_is_surfaced_and_does_not_abort_send() -> None:
    """A KeysClaimError logs a warning; remaining targets are still attempted."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@bot:example.org"
    client.device_id = "BOTDEV"
    olm = MagicMock()
    olm.get_missing_sessions.return_value = {"@alice:example.org": ["ALICEDEV"]}
    client.olm = olm
    claim_error = MagicMock(spec=nio.responses.KeysClaimError)
    claim_error.message = "one time keys exhausted"
    client.keys_claim.return_value = claim_error
    # No known devices: the per-target loop skips with its own warning.
    client.device_store = {"@alice:example.org": {}}

    transport = ToDeviceFrameKeyTransport(client)
    delivered = await transport.send_key(
        room_id=ROOM_ID,
        key_base64="a2V5a2V5a2V5a2V5a2V5a2U=",
        key_index=0,
        targets=[_member("@alice:example.org", "ALICEDEV")],
    )

    client.keys_claim.assert_awaited_once()
    client.to_device.assert_not_awaited()
    assert delivered == []
