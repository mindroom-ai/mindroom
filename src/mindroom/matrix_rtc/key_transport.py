"""Encrypted to-device transport for MatrixRTC media frame keys.

Element Call distributes call frame keys as olm-encrypted
``io.element.call.encryption_keys`` to-device events. nio has no public API
for encrypting arbitrary to-device payloads, so this module drives the olm
machine directly the same way nio's own room-key sharing does: claim one-time
keys for devices without sessions, then olm-encrypt per target device.

Receiving requires a mindroom-nio release that surfaces unknown decrypted olm
events as ``UnknownToDeviceEvent`` (stock nio drops them); until then the bot
can send its own key (participants hear it) but cannot decrypt inbound media.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix_rtc.events import (
    CALL_ENCRYPTION_KEYS_EVENT_TYPE,
    build_key_to_device_content,
    parse_key_to_device_content,
)

if TYPE_CHECKING:
    from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
    from mindroom.matrix_rtc.events import CallMember, ReceivedFrameKey

logger = get_logger(__name__)


class ToDeviceFrameKeyTransport:
    """Sends and parses ``io.element.call.encryption_keys`` to-device events."""

    def __init__(self, client: nio.AsyncClient) -> None:
        self._client = client

    async def send_key(
        self,
        *,
        room_id: str,
        key_base64: str,
        key_index: int,
        targets: list[CallMember],
    ) -> list[CallMember]:
        """Olm-encrypt our frame key and return each target that received it."""
        client = self._client
        olm = client.olm
        if olm is None:
            logger.warning("call_key_send_skipped_no_olm", room_id=room_id)
            return []
        own_user = client.user_id
        own_device = client.device_id
        if own_device is None:
            logger.warning("call_key_send_skipped_no_device_id", room_id=room_id)
            return []
        recipients = [t for t in targets if not (t.user_id == own_user and t.device_id == own_device)]
        if not recipients:
            return []

        missing = olm.get_missing_sessions(sorted({t.user_id for t in recipients}))
        if missing:
            claim_response = await client.keys_claim(missing)
            if isinstance(claim_response, nio.KeysClaimError):
                # Targets without sessions are skipped individually below with
                # their own warning; surface the claim failure itself too.
                logger.warning(
                    "call_key_otk_claim_failed",
                    room_id=room_id,
                    error=str(claim_response.message),
                )

        content = build_key_to_device_content(
            key_base64=key_base64,
            key_index=key_index,
            room_id=room_id,
            member_id=f"{own_user}:{own_device}",
            device_id=own_device,
            sent_ts=int(time.time() * 1000),
        )
        delivered: list[CallMember] = []
        for target in recipients:
            device = client.device_store[target.user_id].get(target.device_id)
            if device is None:
                logger.warning(
                    "call_key_target_device_unknown",
                    room_id=room_id,
                    user_id=target.user_id,
                    device_id=target.device_id,
                )
                continue
            session = olm.session_store.get(device.curve25519)
            if session is None:
                logger.warning(
                    "call_key_target_session_missing",
                    room_id=room_id,
                    user_id=target.user_id,
                    device_id=target.device_id,
                )
                continue
            encrypted_content = olm._olm_encrypt(
                session,
                device,
                CALL_ENCRYPTION_KEYS_EVENT_TYPE,
                content,
            )
            message = nio.ToDeviceMessage(
                type="m.room.encrypted",
                recipient=target.user_id,
                recipient_device=target.device_id,
                content=encrypted_content,
            )
            response = await client.to_device(message)
            if isinstance(response, nio.ToDeviceError):
                logger.warning(
                    "call_key_send_failed",
                    room_id=room_id,
                    user_id=target.user_id,
                    device_id=target.device_id,
                    error=str(response.message),
                )
                continue
            delivered.append(target)
        return delivered

    def parse_incoming(
        self,
        event: AuthenticatedToDeviceEvent,
        *,
        received_at_ms: int,
    ) -> tuple[str, ReceivedFrameKey] | None:
        """Parse a decrypted call-key event together with its target room."""
        if event.type != CALL_ENCRYPTION_KEYS_EVENT_TYPE:
            return None
        content = event.source.get("content")
        if not isinstance(content, dict):
            return None
        room_id = content.get("room_id")
        if not isinstance(room_id, str) or not room_id:
            return None
        received = parse_key_to_device_content(
            event.sender,
            content,
            room_id=room_id,
            received_at_ms=received_at_ms,
        )
        if received is None or received.claimed_device_id != event.authenticated_device_id:
            return None
        return room_id, received
