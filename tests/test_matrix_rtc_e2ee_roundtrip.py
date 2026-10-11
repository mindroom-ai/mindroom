"""Real Olm custom events use NIO identity evidence without app crypto overrides."""
# ruff: noqa: D103

from __future__ import annotations

import json

import nio
import pytest
from unpaddedbase64 import encode_base64

from mindroom.matrix.olm_to_device import PinnedMatrixDevice, send_encrypted_to_device
from tests.test_olm_to_device import RECIPIENT, SENDER, olm_transport


@pytest.mark.asyncio
async def test_cold_custom_sender_queues_query_without_authenticated_callback() -> None:
    async with olm_transport() as (client, peer, requests, _):
        assert peer.olm is not None
        await send_encrypted_to_device(
            client,
            PinnedMatrixDevice(RECIPIENT, "DESKTOP", peer.olm.account.identity_keys["ed25519"]),
            event_type="org.example.custom",
            content={"value": "hello"},
        )
        encrypted = requests[-1]["body"]["messages"][RECIPIENT]["DESKTOP"]
        received = []
        peer.add_to_device_callback(received.append, nio.ToDeviceEvent)
        await peer.receive_response(
            nio.SyncResponse.from_dict(
                {
                    "next_batch": "next",
                    "to_device": {
                        "events": [
                            {"type": "m.room.encrypted", "sender": SENDER, "content": encrypted},
                        ],
                    },
                },
            ),
        )
        assert len(received) == 1
        assert isinstance(received[0], nio.UnknownToDeviceEvent)
        assert not isinstance(received[0], nio.AuthenticatedToDeviceEvent)
        assert SENDER in peer.olm.users_for_key_query


@pytest.mark.asyncio
async def test_custom_envelope_may_omit_sender_device_but_keeps_signing_identity() -> None:
    async with olm_transport() as (client, peer, _, _):
        assert client.olm is not None
        assert peer.olm is not None
        await send_encrypted_to_device(
            client,
            PinnedMatrixDevice(RECIPIENT, "DESKTOP", peer.olm.account.identity_keys["ed25519"]),
            event_type="org.example.setup",
            content={},
        )
        await peer.receive_response(
            nio.KeysQueryResponse(
                {SENDER: {"CLOUD": client.olm.share_keys()["device_keys"]}},
                {},
            ),
        )
        session = client.olm.session_store.get(peer.olm.account.identity_keys["curve25519"])
        assert session is not None
        payload = {
            "sender": SENDER,
            "keys": {"ed25519": client.olm.account.identity_keys["ed25519"]},
            "recipient": RECIPIENT,
            "recipient_keys": {"ed25519": peer.olm.account.identity_keys["ed25519"]},
            "type": "org.example.custom",
            "content": {"value": "hello"},
        }
        encrypted = session.encrypt(json.dumps(payload))
        message_type, ciphertext = encrypted.to_parts()
        source = {
            "type": "m.room.encrypted",
            "sender": SENDER,
            "content": {
                "algorithm": "m.olm.v1.curve25519-aes-sha2",
                "sender_key": client.olm.account.identity_keys["curve25519"],
                "ciphertext": {
                    peer.olm.account.identity_keys["curve25519"]: {
                        "type": message_type,
                        "body": encode_base64(ciphertext),
                    },
                },
            },
        }
        received = []
        peer.add_to_device_callback(received.append, nio.AuthenticatedToDeviceEvent)
        await peer.receive_response(
            nio.SyncResponse.from_dict(
                {
                    "next_batch": "next",
                    "to_device": {"events": [source]},
                },
            ),
        )
        assert len(received) == 1
        assert received[0].authenticated_sender.device_id == "CLOUD"
        assert received[0].source["content"] == {"value": "hello"}
