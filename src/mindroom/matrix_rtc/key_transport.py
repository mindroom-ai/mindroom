"""Encrypted MatrixRTC frame keys through public NIO transport and identity evidence."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    PinnedMatrixDevice,
    authenticated_sender_is_current,
    send_encrypted_to_device,
)
from mindroom.matrix_rtc.events import (
    CALL_ENCRYPTION_KEYS_EVENT_TYPE,
    build_key_to_device_content,
    parse_key_to_device_content,
)

if TYPE_CHECKING:
    import nio
    from nio import AuthenticatedToDeviceEvent

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
            try:
                await send_encrypted_to_device(
                    client,
                    PinnedMatrixDevice(target.user_id, target.device_id, device.ed25519),
                    event_type=CALL_ENCRYPTION_KEYS_EVENT_TYPE,
                    content=content,
                )
            except OlmToDeviceError as exc:
                logger.warning(
                    "call_key_send_failed",
                    room_id=room_id,
                    user_id=target.user_id,
                    device_id=target.device_id,
                    error=str(exc),
                )
                continue
            delivered.append(target)
            logger.info(
                "call_key_sent",
                room_id=room_id,
                user_id=target.user_id,
                device_id=target.device_id,
                key_index=key_index,
            )
        return delivered

    def parse_incoming(
        self,
        event: AuthenticatedToDeviceEvent,
        *,
        received_at_ms: int,
    ) -> tuple[str, ReceivedFrameKey] | None:
        """Parse a decrypted call-key event together with its target room."""
        if event.type != CALL_ENCRYPTION_KEYS_EVENT_TYPE or not authenticated_sender_is_current(self._client, event):
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
        if received is None or received.claimed_device_id != event.authenticated_sender.device_id:
            return None
        return room_id, received
