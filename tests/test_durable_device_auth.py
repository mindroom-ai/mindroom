"""Captured replay identity remains historical evidence, subject to current authority."""
# ruff: noqa: D103

from __future__ import annotations

from types import SimpleNamespace

import nio
import pytest
from nio.crypto import DeviceStore, OlmDevice, TrustState
from nio.durable import RecordKind, SyncRecord
from nio.durable.codec import restore_event
from nio.durable.model import CryptoEvidence

from mindroom.matrix.olm_to_device import PinnedMatrixDevice, authenticated_sender_matches


@pytest.mark.parametrize("change", [None, "removed", "curve", "signing", "device", "sender", "blacklisted"])
def test_replayed_identity_requires_current_device_and_pin(change: str | None) -> None:
    store = DeviceStore()
    device = OlmDevice("@alice:example.org", "ALICE", {"curve25519": "curve", "ed25519": "signing"})
    store.add(device)
    identity = nio.AuthenticatedDevice(device.user_id, device.device_id, device.curve25519, device.ed25519)
    record = SyncRecord(
        RecordKind.TO_DEVICE,
        None,
        {
            "type": "m.room.encrypted",
            "sender": device.user_id,
            "content": {"algorithm": "m.olm.v1.curve25519-aes-sha2", "sender_key": "curve"},
        },
        clear={
            "type": "org.example.call",
            "sender": device.user_id,
            "sender_device": device.device_id,
            "keys": {"ed25519": device.ed25519},
            "content": {"value": "hello"},
        },
        crypto=CryptoEvidence(None, "curve", authenticated_sender=identity),
        route="to_device",
    )
    event = restore_event(record)
    assert isinstance(event, nio.AuthenticatedToDeviceEvent)
    expected = PinnedMatrixDevice(device.user_id, device.device_id, device.ed25519)
    if change == "removed":
        device.deleted = True
    elif change == "curve":
        device.curve25519 = "other"
    elif change == "signing":
        device.ed25519 = "other"
    elif change == "device":
        expected = PinnedMatrixDevice(device.user_id, "OTHER", device.ed25519)
    elif change == "sender":
        expected = PinnedMatrixDevice("@other:example.org", device.device_id, device.ed25519)
    elif change == "blacklisted":
        device.trust_state = TrustState.blacklisted
    replayed = restore_event(record)
    assert isinstance(replayed, nio.AuthenticatedToDeviceEvent)
    assert replayed.authenticated_sender == identity
    client = SimpleNamespace(olm=SimpleNamespace(device_store=store))
    assert authenticated_sender_matches(client, replayed, expected) is (change is None)
