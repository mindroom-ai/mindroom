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


def _fresh_olm_pair(tmp: str) -> tuple[Olm, Olm, OlmDevice]:
    sender = Olm(SENDER, "CLOUD", DefaultStore(SENDER, "CLOUD", tmp))
    recipient = Olm(RECIPIENT, "DESKTOP", DefaultStore(RECIPIENT, "DESKTOP", tmp))
    recipient_device = OlmDevice(recipient.user_id, recipient.device_id, recipient.account.identity_keys)
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
async def test_first_contact_queries_exact_device_and_claims_olm_session() -> None:
    """An unknown pinned device is queried and receives a newly established encrypted session."""
    with tempfile.TemporaryDirectory() as tmp:
        sender, recipient, recipient_device = _fresh_olm_pair(tmp)
        try:
            client = AsyncMock(spec=nio.AsyncClient)
            client.olm = sender
            client.access_token = "access-token"  # noqa: S105 - Test-only Matrix client fixture.
            recipient.account.generate_one_time_keys(1)
            one_time_key = next(iter(recipient.account.one_time_keys["curve25519"].values()))

            device_payload = {
                "algorithms": ["m.olm.v1.curve25519-aes-sha2"],
                "device_id": recipient.device_id,
                "keys": {
                    f"curve25519:{recipient.device_id}": recipient_device.curve25519,
                    f"ed25519:{recipient.device_id}": recipient_device.ed25519,
                },
                "user_id": recipient.user_id,
            }
            device_payload["signatures"] = {
                recipient.user_id: {
                    f"ed25519:{recipient.device_id}": recipient.sign_json(device_payload),
                },
            }
            query_response = nio.KeysQueryResponse(
                {recipient.user_id: {recipient.device_id: device_payload}},
                {},
            )

            async def query_keys(*_args: object, **_kwargs: object) -> nio.KeysQueryResponse:
                sender.handle_response(query_response)
                return query_response

            key_payload = {"key": one_time_key}
            key_payload["signatures"] = {
                recipient.user_id: {
                    f"ed25519:{recipient.device_id}": recipient.sign_json(key_payload),
                },
            }
            claim_response = nio.KeysClaimResponse(
                {
                    recipient.user_id: {
                        recipient.device_id: {"signed_curve25519:AAAA": key_payload},
                    },
                },
                {},
            )

            async def claim_keys(*_args: object, **_kwargs: object) -> nio.KeysClaimResponse:
                sender.handle_response(claim_response)
                return claim_response

            client._send = AsyncMock(side_effect=query_keys)
            client.keys_claim = AsyncMock(side_effect=claim_keys)
            client.to_device = AsyncMock(side_effect=lambda message: nio.ToDeviceResponse(message))
            target = PinnedMatrixDevice(RECIPIENT, "DESKTOP", recipient_device.ed25519)

            await send_encrypted_to_device(
                client,
                target,
                event_type="io.mindroom.test",
                content={"first": "contact"},
            )

            client._send.assert_awaited_once()
            client.keys_claim.assert_awaited_once_with({RECIPIENT: ["DESKTOP"]})
            client.to_device.assert_awaited_once()
            assert sender.session_store.get(recipient_device.curve25519) is not None
            assert RECIPIENT not in sender.users_for_key_query
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


@pytest.mark.asyncio
async def test_first_contact_fails_closed_when_claim_creates_no_session() -> None:
    """A successful-looking empty key claim cannot produce plaintext or misaddressed delivery."""
    with tempfile.TemporaryDirectory() as tmp:
        sender, recipient, recipient_device = _fresh_olm_pair(tmp)
        try:
            sender.device_store.add(recipient_device)
            sender.verify_device(recipient_device)
            client = AsyncMock(spec=nio.AsyncClient)
            client.olm = sender
            client.keys_claim.return_value = nio.KeysClaimResponse({}, {})

            with pytest.raises(OlmToDeviceError, match="Could not establish an Olm session"):
                await send_encrypted_to_device(
                    client,
                    PinnedMatrixDevice(RECIPIENT, "DESKTOP", recipient_device.ed25519),
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
