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
from unittest.mock import AsyncMock

import nio
import pytest
from nio.crypto import Olm, OlmDevice
from nio.crypto.olm_machine import DecryptedOlmT
from nio.store import DefaultStore

from mindroom.matrix_rtc.events import CALL_ENCRYPTION_KEYS_EVENT_TYPE, CallMember
from mindroom.matrix_rtc.key_transport import ToDeviceFrameKeyTransport

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


@pytest.mark.asyncio
async def test_frame_key_round_trips_through_real_olm() -> None:
    """A frame key survives olm encryption, decryption, and parsing."""
    with tempfile.TemporaryDirectory() as tmp:
        bot_olm, rec_olm, _rec_dev = _olm_pair(tmp)

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
            membership_id=f"{REC}:RECDEV",
        )

        await transport.send_key(room_id=ROOM, key_base64=KEY_B64, key_index=5, targets=[target])

        assert len(sent) == 1
        assert sent[0].type == "m.room.encrypted"
        assert sent[0].recipient == REC

        olm_event = nio.OlmEvent.from_dict({"type": "m.room.encrypted", "sender": BOT, "content": sent[0].content})
        decrypted = rec_olm.decrypt_event(olm_event)
        assert isinstance(decrypted, nio.UnknownToDeviceEvent)
        assert decrypted.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE

        received = transport.parse_incoming(decrypted, room_id=ROOM)
        assert received is not None
        assert received.key_base64 == KEY_B64
        assert received.key_index == 5
        assert received.claimed_device_id == "BOTDEV"

        # A key addressed to a different room must not be accepted for this session.
        assert transport.parse_incoming(decrypted, room_id="!other:example.org") is None
