"""MatrixRTC keys use public NIO transport and current sender authorization."""
# ruff: noqa: D103

from __future__ import annotations

import nio
import pytest

from mindroom.matrix_rtc.events import CALL_ENCRYPTION_KEYS_EVENT_TYPE, CallMember, build_key_to_device_content
from mindroom.matrix_rtc.key_transport import ToDeviceFrameKeyTransport
from tests.test_olm_to_device import RECIPIENT, SENDER, olm_transport

ROOM_ID = "!room:example.org"


def _member(user_id: str, device_id: str) -> CallMember:
    return CallMember(user_id=user_id, device_id=device_id, created_ts=0, expires_ms=10_000_000)


@pytest.mark.asyncio
async def test_frame_key_uses_public_pinned_send_and_delivers_clear_payload() -> None:
    async with olm_transport() as (client, peer, requests, _):
        assert client.olm is not None
        assert peer.olm is not None
        await client.keys_query({RECIPIENT})
        await peer.receive_response(
            nio.KeysQueryResponse({SENDER: {"CLOUD": client.olm.share_keys()["device_keys"]}}, {}),
        )
        target = _member(RECIPIENT, "DESKTOP")
        delivered = await ToDeviceFrameKeyTransport(client).send_key(
            room_id=ROOM_ID,
            key_base64="QUJDREVGR0hJSktMTU5PUA==",
            key_index=5,
            targets=[target],
        )
        assert delivered == [target]
        encrypted = requests[-1]["body"]["messages"][RECIPIENT]["DESKTOP"]
        event = nio.ToDeviceEvent.parse_event({"type": "m.room.encrypted", "sender": SENDER, "content": encrypted})
        assert isinstance(event, nio.OlmEvent)
        decrypted = peer.olm.decrypt_event(event)
        assert isinstance(decrypted, nio.AuthenticatedToDeviceEvent)
        parsed = ToDeviceFrameKeyTransport(peer).parse_incoming(decrypted, received_at_ms=1000)
        assert parsed is not None
        room_id, received = parsed
        assert room_id == ROOM_ID
        assert received.key_index == 5
        assert received.key_base64 == "QUJDREVGR0hJSktMTU5PUA=="
        assert received.claimed_device_id == "CLOUD"


@pytest.mark.asyncio
async def test_blocked_frame_key_target_is_not_sent() -> None:
    async with olm_transport() as (client, _peer, requests, _):
        await client.keys_query({RECIPIENT})
        assert client.olm is not None
        client.blacklist_device(client.olm.device_store[RECIPIENT]["DESKTOP"])
        delivered = await ToDeviceFrameKeyTransport(client).send_key(
            room_id=ROOM_ID,
            key_base64="QUJDREVGR0hJSktMTU5PUA==",
            key_index=5,
            targets=[_member(RECIPIENT, "DESKTOP")],
        )
        assert delivered == []
        assert not any("/sendToDevice/" in item["path"] for item in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "deleted", "blacklisted", "signing", "curve"])
async def test_incoming_frame_key_rechecks_current_device(change: str | None) -> None:
    async with olm_transport() as (client, peer, _, _):
        assert client.olm is not None
        assert peer.olm is not None
        await client.keys_query({RECIPIENT})
        device = client.olm.device_store[RECIPIENT]["DESKTOP"]
        identity = nio.AuthenticatedDevice(RECIPIENT, "DESKTOP", device.curve25519, device.ed25519)
        source = {
            "type": CALL_ENCRYPTION_KEYS_EVENT_TYPE,
            "sender": RECIPIENT,
            "content": build_key_to_device_content(
                key_base64="QUJDREVGR0hJSktMTU5PUA==",
                key_index=5,
                room_id=ROOM_ID,
                member_id=f"{RECIPIENT}:DESKTOP",
                device_id="DESKTOP",
                sent_ts=1000,
            ),
        }
        event = nio.AuthenticatedToDeviceEvent(source, RECIPIENT, CALL_ENCRYPTION_KEYS_EVENT_TYPE, identity)
        if change == "deleted":
            device.deleted = True
        elif change == "blacklisted":
            client.blacklist_device(device)
        elif change == "signing":
            device.ed25519 = "rotated"
        elif change == "curve":
            device.curve25519 = "rotated"
        assert (ToDeviceFrameKeyTransport(client).parse_incoming(event, received_at_ms=1000) is not None) is (
            change is None
        )
