"""Recovered to-device input rechecks the current signed-device store."""
# ruff: noqa: D103

from __future__ import annotations

import nio
import pytest
from nio.crypto import DeviceStore, OlmDevice

from mindroom.matrix import client_session
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent


@pytest.mark.parametrize("change", [None, "removed", "curve", "signing", "device", "sender"])
def test_restored_device_authentication_fails_closed(change: str | None) -> None:
    store = DeviceStore()
    device = OlmDevice("@alice:example.org", "ALICE", {"curve25519": "curve", "ed25519": "signing"})
    store.add(device)
    source = {
        "type": "m.room.encrypted",
        "sender": device.user_id,
        "content": {"algorithm": "m.olm.v1.curve25519-aes-sha2", "sender_key": "curve"},
    }
    clear = {
        "type": "org.example.call",
        "sender": device.user_id,
        "sender_device": "ALICE",
        "keys": {"ed25519": "signing"},
        "content": {},
    }
    if change == "removed":
        device.deleted = True
    elif change == "curve":
        device.keys["curve25519"] = "other"
    elif change == "signing":
        device.keys["ed25519"] = "other"
    elif change == "device":
        clear["sender_device"] = "OTHER"
    elif change == "sender":
        source["sender"] = "@other:example.org"
    event = nio.UnknownToDeviceEvent.from_dict(clear)
    result = client_session.authenticate_to_device_event(source, event, device_store=store)
    assert isinstance(result, AuthenticatedToDeviceEvent) is (change is None)
