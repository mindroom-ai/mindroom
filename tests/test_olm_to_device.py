"""Tests for exact-device Matrix Olm transport."""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock

import nio
import pytest
from nio.crypto import Olm, OlmDevice
from nio.store import DefaultStore

from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    PinnedMatrixDevice,
    authenticated_sender_matches,
    send_encrypted_to_device,
)
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

SENDER = "@cloud:example.org"
RECIPIENT = "@desktop:example.org"


def _olm_pair(tmp: str) -> tuple[Olm, Olm, OlmDevice]:
    sender = Olm(SENDER, "CLOUD", DefaultStore(SENDER, "CLOUD", tmp))
    recipient = Olm(RECIPIENT, "DESKTOP", DefaultStore(RECIPIENT, "DESKTOP", tmp))
    sender_device = OlmDevice(sender.user_id, sender.device_id, sender.account.identity_keys)
    recipient_device = OlmDevice(recipient.user_id, recipient.device_id, recipient.account.identity_keys)
    sender.device_store.add(recipient_device)
    recipient.device_store.add(sender_device)
    sender.verify_device(recipient_device)
    recipient.verify_device(sender_device)
    recipient.account.generate_one_time_keys(1)
    one_time_key = next(iter(recipient.account.one_time_keys["curve25519"].values()))
    sender.create_session(one_time_key, recipient_device.curve25519)
    recipient.account.mark_keys_as_published()
    return sender, recipient, recipient_device


@pytest.mark.asyncio
async def test_send_targets_one_pinned_device_with_olm_ciphertext() -> None:
    """Custom commands are Olm encrypted and addressed to only the pinned device."""
    with tempfile.TemporaryDirectory() as tmp:
        sender, recipient, recipient_device = _olm_pair(tmp)
        try:
            client = AsyncMock(spec=nio.AsyncClient)
            client.olm = sender
            sent: list[nio.ToDeviceMessage] = []

            async def capture(message: nio.ToDeviceMessage) -> nio.ToDeviceResponse:
                sent.append(message)
                return nio.ToDeviceResponse(message)

            client.to_device = capture
            target = PinnedMatrixDevice(RECIPIENT, "DESKTOP", recipient_device.ed25519)

            await send_encrypted_to_device(
                client,
                target,
                event_type="io.mindroom.test",
                content={"secret": "value"},
            )

            assert len(sent) == 1
            assert sent[0].type == "m.room.encrypted"
            assert sent[0].recipient == RECIPIENT
            assert sent[0].recipient_device == "DESKTOP"
            assert sent[0].content["algorithm"] == "m.olm.v1.curve25519-aes-sha2"
            assert "secret" not in str(sent[0].content)
        finally:
            sender.store.database.close()
            recipient.store.database.close()


@pytest.mark.asyncio
async def test_send_fails_closed_on_fingerprint_mismatch() -> None:
    """A homeserver cannot silently substitute a different registered device key."""
    with tempfile.TemporaryDirectory() as tmp:
        sender, recipient, _recipient_device = _olm_pair(tmp)
        try:
            client = AsyncMock(spec=nio.AsyncClient)
            client.olm = sender

            with pytest.raises(OlmToDeviceError, match="fingerprint mismatch"):
                await send_encrypted_to_device(
                    client,
                    PinnedMatrixDevice(RECIPIENT, "DESKTOP", "wrong-fingerprint"),
                    event_type="io.mindroom.test",
                    content={},
                )

            client.to_device.assert_not_awaited()
        finally:
            sender.store.database.close()
            recipient.store.database.close()


def test_authenticated_sender_must_match_user_device_and_fingerprint() -> None:
    """Decrypted payload claims never replace the authenticated Olm device identity."""
    with tempfile.TemporaryDirectory() as tmp:
        sender, recipient, recipient_device = _olm_pair(tmp)
        try:
            client = AsyncMock(spec=nio.AsyncClient)
            client.olm = sender
            event = AuthenticatedToDeviceEvent(
                source={"content": {}},
                sender=RECIPIENT,
                type="io.mindroom.test",
                authenticated_device_id="DESKTOP",
            )

            assert authenticated_sender_matches(
                client,
                event,
                PinnedMatrixDevice(RECIPIENT, "DESKTOP", recipient_device.ed25519),
            )
            assert not authenticated_sender_matches(
                client,
                event,
                PinnedMatrixDevice(RECIPIENT, "OTHER", recipient_device.ed25519),
            )
        finally:
            sender.store.database.close()
            recipient.store.database.close()
