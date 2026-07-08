"""Visibility and recovery for undecryptable encrypted Matrix events.

nio surfaces encrypted timeline events it cannot decrypt as ``MegolmEvent``.
Without a registered callback those events vanish silently: the agent never
answers and the logs show nothing, which makes wedged encryption sessions
impossible to diagnose.
This module logs each failure and sends a Matrix room-key request so the
session can self-heal when the sender's client honors key requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nio.exceptions import LocalProtocolError

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio

logger = get_logger(__name__)


async def handle_decrypt_failure(client: nio.AsyncClient, room: nio.MatrixRoom, event: nio.MegolmEvent) -> None:
    """Log one undecryptable Megolm event and request its room key once per session."""
    already_requested = event.session_id in client.outgoing_key_requests
    logger.warning(
        "matrix_event_decryption_failed",
        room_id=room.room_id,
        event_id=event.event_id,
        sender=event.sender,
        sender_device_id=event.device_id,
        session_id=event.session_id,
        key_request_already_sent=already_requested,
        hint=(
            "The sending client did not share this Megolm session with the bot's device. "
            "A room-key request is sent so the session can recover; if the sender's client "
            "does not honor it, ask the sender to send a new message."
        ),
    )
    if already_requested:
        return
    try:
        await client.request_room_key(event)
    except LocalProtocolError:
        # A concurrent callback for the same session already requested the key.
        logger.debug(
            "matrix_room_key_request_already_pending",
            room_id=room.room_id,
            session_id=event.session_id,
        )


__all__ = ["handle_decrypt_failure"]
