"""Real-olm end-to-end round trip for MatrixRTC call frame keys.

Two nio Olm machines establish an olm session in-process (no network), then
``ToDeviceFrameKeyTransport.send_key`` olm-encrypts a call frame key and the
recipient decrypts it and ``parse_incoming`` reads it back. This exercises
the real crypto path for encrypted-room calls.

Receiving requires the mindroom-nio change that surfaces unknown decrypted
olm to-device events as ``UnknownToDeviceEvent`` (mindroom-ai/mindroom-nio#5).
Until that lands in the pinned nio, the test skips.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import nio
import pytest
from nio.crypto import Olm, OlmDevice
from nio.crypto.olm_machine import DecryptedOlmT
from nio.store import DefaultStore
from structlog.testing import capture_logs

from mindroom.desktop.protocol import DESKTOP_PAIRING_CLAIM_EVENT_TYPE, DesktopPairingClaim
from mindroom.matrix.client_session import matrix_client
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.matrix_rtc.events import CALL_ENCRYPTION_KEYS_EVENT_TYPE, CallMember
from mindroom.matrix_rtc.key_transport import ToDeviceFrameKeyTransport
from tests.conftest import test_runtime_paths

_NIO_SURFACES_UNKNOWN_OLM = "UnknownToDeviceEvent" in str(DecryptedOlmT)
pytestmark = pytest.mark.skipif(
    not _NIO_SURFACES_UNKNOWN_OLM,
    reason="needs mindroom-nio#5 (unknown decrypted olm to-device passthrough)",
)

BOT = "@bot:example.org"
REC = "@rec:example.org"
ROOM = "!call:example.org"
KEY_B64 = "QUJDREVGR0hJSktMTU5PUA=="  # 16 bytes


def _olm_pair(tmp: str) -> tuple[Olm, Olm, OlmDevice]:
    bot = Olm(BOT, "BOTDEV", DefaultStore(BOT, "BOTDEV", tmp))
    rec = Olm(REC, "RECDEV", DefaultStore(REC, "RECDEV", tmp))
    bot_dev = OlmDevice(bot.user_id, bot.device_id, bot.account.identity_keys)
    rec_dev = OlmDevice(rec.user_id, rec.device_id, rec.account.identity_keys)
    bot.device_store.add(rec_dev)
    rec.device_store.add(bot_dev)
    bot.verify_device(rec_dev)
    rec.verify_device(bot_dev)
    rec.account.generate_one_time_keys(1)
    one_time = next(iter(rec.account.one_time_keys["curve25519"].values()))
    bot.create_session(one_time, rec_dev.curve25519)
    rec.account.mark_keys_as_published()
    return bot, rec, rec_dev


def _authenticate_rust_style_event(
    monkeypatch: pytest.MonkeyPatch,
    client: nio.AsyncClient,
    olm_event: nio.OlmEvent,
    decrypted: AuthenticatedToDeviceEvent,
) -> nio.ToDeviceEvent | None:
    """Model matrix-rust-sdk omitting nio's redundant identity fields."""
    rust_style_source = dict(decrypted.source)
    rust_style_source.pop("sender_device", None)
    rust_style_source.pop("keys", None)
    rust_style_event = nio.UnknownToDeviceEvent.from_dict(rust_style_source)
    with monkeypatch.context() as context:
        context.setattr(
            nio.AsyncClient,
            "_handle_decrypt_to_device",
            lambda _self, _event: rust_style_event,
        )
        return client._handle_decrypt_to_device(olm_event)


async def _decrypt_with_ambiguous_sender(
    transport: ToDeviceFrameKeyTransport,
    recipient: nio.AsyncClient,
    sender_olm: Olm,
    recipient_olm: Olm,
    sent: list[nio.ToDeviceMessage],
    target: CallMember,
) -> nio.ToDeviceEvent | None:
    """Add a duplicate curve identity and decrypt a newly sent frame key."""
    recipient_olm.device_store.add(OlmDevice(BOT, "BOTDUP", sender_olm.account.identity_keys))
    await transport.send_key(room_id=ROOM, key_base64=KEY_B64, key_index=6, targets=[target])
    encrypted = nio.OlmEvent.from_dict(
        {"type": "m.room.encrypted", "sender": BOT, "content": sent[1].content},
    )
    return recipient._handle_decrypt_to_device(encrypted)


@pytest.mark.asyncio
async def test_frame_key_round_trips_through_real_olm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame key survives olm encryption, decryption, and parsing."""
    with tempfile.TemporaryDirectory() as tmp:
        bot_olm, rec_olm, _rec_dev = _olm_pair(tmp)
        try:
            client = AsyncMock(spec=nio.AsyncClient)
            client.user_id = BOT
            client.device_id = "BOTDEV"
            client.olm = bot_olm
            client.device_store = bot_olm.device_store
            sent: list[nio.ToDeviceMessage] = []

            async def _capture(message: nio.ToDeviceMessage) -> nio.ToDeviceResponse:
                sent.append(message)
                return nio.ToDeviceResponse(message)

            client.to_device = _capture
            client.keys_claim = AsyncMock()

            transport = ToDeviceFrameKeyTransport(client)
            target = CallMember(
                user_id=REC,
                device_id="RECDEV",
                created_ts=0,
                expires_ms=10_000_000,
            )

            await transport.send_key(room_id=ROOM, key_base64=KEY_B64, key_index=5, targets=[target])

            assert len(sent) == 1
            assert sent[0].type == "m.room.encrypted"
            assert sent[0].recipient == REC

            olm_event = nio.OlmEvent.from_dict(
                {"type": "m.room.encrypted", "sender": BOT, "content": sent[0].content},
            )
            runtime_paths = test_runtime_paths(Path(tmp) / "runtime")
            async with matrix_client("https://example.org", runtime_paths, user_id=REC) as rec_client:
                rec_client.device_id = "RECDEV"
                rec_client.olm = rec_olm
                decrypted = rec_client._handle_decrypt_to_device(olm_event)
                assert isinstance(decrypted, AuthenticatedToDeviceEvent)
                assert decrypted.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE
                assert decrypted.authenticated_device_id == "BOTDEV"

                parsed = transport.parse_incoming(decrypted, received_at_ms=1_000)
                assert parsed is not None
                room_id, received = parsed
                assert room_id == ROOM
                assert received.key_base64 == KEY_B64
                assert received.key_index == 5
                assert received.claimed_device_id == "BOTDEV"

                # matrix-rust-sdk (used by Cinny) does not include nio's
                # redundant sender_device/keys fields for custom Olm events.
                # The authenticated Olm envelope sender key must still map to
                # the unique signed device in the recipient's device store.
                rust_style = _authenticate_rust_style_event(monkeypatch, rec_client, olm_event, decrypted)
                assert isinstance(rust_style, AuthenticatedToDeviceEvent)
                assert rust_style.authenticated_device_id == "BOTDEV"

                spoofed_device = AuthenticatedToDeviceEvent(
                    source=decrypted.source,
                    sender=decrypted.sender,
                    type=decrypted.type,
                    authenticated_device_id="OTHER",
                )
                assert transport.parse_incoming(spoofed_device, received_at_ms=1_001) is None

                ambiguous = await _decrypt_with_ambiguous_sender(
                    transport,
                    rec_client,
                    bot_olm,
                    rec_olm,
                    sent,
                    target,
                )
                assert isinstance(ambiguous, nio.UnknownToDeviceEvent)
                assert not isinstance(ambiguous, AuthenticatedToDeviceEvent)
        finally:
            bot_olm.store.database.close()
            rec_olm.store.database.close()


@pytest.mark.asyncio
async def test_cold_pairing_claim_queues_sender_key_query_and_logs_rejection() -> None:
    """Unknown dedicated Desktop device triggers discovery before CLI retry."""
    with tempfile.TemporaryDirectory() as tmp:
        bot_olm, rec_olm, rec_dev = _olm_pair(tmp)
        try:
            rec_olm.device_store[BOT].pop("BOTDEV")
            session = bot_olm.session_store.get(rec_dev.curve25519)
            assert session is not None
            encrypted_content = bot_olm._olm_encrypt(
                session,
                rec_dev,
                DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
                DesktopPairingClaim("pair-code").to_content(),
            )
            olm_event = nio.OlmEvent.from_dict(
                {"type": "m.room.encrypted", "sender": BOT, "content": encrypted_content},
            )
            runtime_paths = test_runtime_paths(Path(tmp) / "runtime")
            async with matrix_client("https://example.org", runtime_paths, user_id=REC) as rec_client:
                rec_client.device_id = "RECDEV"
                rec_client.olm = rec_olm
                with capture_logs() as logs:
                    decrypted = rec_client._handle_decrypt_to_device(olm_event)

                assert isinstance(decrypted, nio.UnknownToDeviceEvent)
                assert BOT in rec_olm.users_for_key_query
                assert any(
                    log.get("event") == "custom_olm_rejected"
                    and log.get("event_type") == DESKTOP_PAIRING_CLAIM_EVENT_TYPE
                    for log in logs
                )
        finally:
            bot_olm.store.database.close()
            rec_olm.store.database.close()
