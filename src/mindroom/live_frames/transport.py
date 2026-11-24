"""Sending one live frame: Megolm-encrypted in an encrypted room, over the homeserver's MSC2477 ephemeral route."""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import TYPE_CHECKING

import nio
from nio.api import Api

from mindroom.live_frames.frames import FRAME_VERSION, LIVE_EVENT_TYPE
from mindroom.logging_config import get_logger
from mindroom.matrix.server_features import homeserver_unstable_features

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

# MindRoom's Tuwunel fork lists this while it relays custom ephemeral events only to syncs that opt into their type.
_LIVE_FRAMES_FEATURE = "io.mindroom.ephemeral_opt_in"
# The fork refuses bodies above 64 KiB; this leaves room for the encryption envelope's JSON.
_MAX_FRAME_BYTES = 60_000
# Megolm's bytes around the AES-CBC ciphertext at most: version, message index, length, MAC, and signature.
_MEGOLM_OVERHEAD_BYTES = 83
# The encrypted content's JSON without its ciphertext: algorithm, both keys, and a device ID of up to 95 characters.
_ENVELOPE_BYTES = 300
_SEND_TIMEOUT_SECONDS = 5.0
# How long a publisher waits after a send failed at once, as through a proxy while the homeserver restarts.
RETRY_BACKOFF_SECONDS = 1.0
_FEATURE_CHECK_SECONDS = 600.0
_FEATURE_CHECK_FAILED = "Could not check whether the homeserver relays live reply frames"
_feature_checks: dict[str, tuple[float, bool]] = {}


async def homeserver_relays_live_frames(homeserver_url: str) -> bool:
    """Return whether the homeserver relays live frames, rechecked at most every ten minutes."""
    checked = _feature_checks.get(homeserver_url)
    now = time.monotonic()
    if checked is not None and now - checked[0] < _FEATURE_CHECK_SECONDS:
        return checked[1]
    features = await homeserver_unstable_features(homeserver_url, failure_message=_FEATURE_CHECK_FAILED)
    if features is None:
        # A failed check, as through a proxy while the homeserver restarts, is asked again by the next reply.
        return False
    supported = features.get(_LIVE_FRAMES_FEATURE) is True
    _feature_checks[homeserver_url] = (now, supported)
    return supported


class LiveSendOutcome(Enum):
    """What one frame send means for the frames after it."""

    SENT = "sent"
    # Not sent this time; the next frame is a snapshot.
    RETRY = "retry"
    # Too big to send as a frame, or refused by the homeserver or the room; frames stop and durable edits take over.
    STOP = "stop"


# What a refusal means for later frames; any other error is retried with a snapshot. A rate limit never gets here:
# nio retries it with the same transaction ID within the publisher's send timeout.
_REFUSAL_ERRCODES = frozenset({"M_TOO_LARGE", "M_UNRECOGNIZED", "M_FORBIDDEN", "M_UNKNOWN", "M_NOT_FOUND"})


def _serialized_size(content: Mapping[str, object]) -> int:
    """Return the size of the body nio sends for ``content``.

    nio sends compact JSON with ASCII escapes, so a character outside ASCII takes 6 bytes and one outside the BMP 12.
    """
    return len(Api.to_json(dict(content)))


def frame_fits(client: nio.AsyncClient, room_id: str, frame: dict[str, object], *, event_type: str) -> bool:
    """Return whether the frame is within the size limit as it goes out to the room, encrypted in an encrypted room."""
    room = client.rooms.get(room_id)
    if room is None or not room.encrypted:
        return _serialized_size(frame) <= _MAX_FRAME_BYTES
    return _encrypted_size(room_id, frame, event_type=event_type) <= _MAX_FRAME_BYTES


def _encrypted_size(room_id: str, frame: dict[str, object], *, event_type: str) -> int:
    """Return at most how big the encrypted content of the frame is.

    nio encrypts the event as JSON with ASCII escapes, padded to whole 16-byte blocks, and sends it base64-encoded, so a
    character outside ASCII can take 8 bytes and one outside the BMP 16.
    """
    plaintext = _serialized_size({"content": frame, "type": event_type, "room_id": room_id})
    ciphertext = (plaintext // 16 + 1) * 16 + _MEGOLM_OVERHEAD_BYTES
    return (ciphertext * 4 + 2) // 3 + _ENVELOPE_BYTES


async def send_live_frame(
    client: nio.AsyncClient,
    room_id: str,
    frame: dict[str, object],
    *,
    transaction_id: str,
    event_type: str = LIVE_EVENT_TYPE,
) -> LiveSendOutcome:
    """Send one frame of ``event_type`` to the room, giving up on a send that hangs past the send timeout.

    Frames are best effort: a send that raises stops them instead of failing what they show.
    """
    try:
        return await asyncio.wait_for(
            _send(client, room_id, frame, transaction_id=transaction_id, event_type=event_type),
            timeout=_SEND_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return LiveSendOutcome.RETRY
    except Exception:
        logger.warning("live_frame_send_failed", room_id=room_id, event_type=event_type, exc_info=True)
        return LiveSendOutcome.STOP


async def _send(
    client: nio.AsyncClient,
    room_id: str,
    frame: dict[str, object],
    *,
    transaction_id: str,
    event_type: str,
) -> LiveSendOutcome:
    """Send one frame, encrypted with the room's current group session when the room is encrypted."""
    room = client.rooms.get(room_id)
    if room is None:
        return LiveSendOutcome.STOP
    content: dict[str, object] = frame
    if room.encrypted:
        if _serialized_size(frame) > _MAX_FRAME_BYTES:
            logger.info("live_frames_too_large", room_id=room_id)
            return LiveSendOutcome.STOP
        # Frames do not count toward count-based rotation: at ten frames a second they would rotate the session every
        # ten seconds, each rotation sharing a new key with every device. Age and membership rotation still apply.
        encrypted = await client.room_encrypt(
            room_id,
            event_type,
            frame,
            ignore_unverified_devices=True,
            count_toward_rotation=False,
        )
        content = {"v": FRAME_VERSION, "encrypted": encrypted}
    if _serialized_size(content) > _MAX_FRAME_BYTES:
        logger.info("live_frames_too_large", room_id=room_id)
        return LiveSendOutcome.STOP
    response = await client.room_send_ephemeral(room_id, event_type, content, tx_id=transaction_id)
    if isinstance(response, nio.RoomSendEphemeralResponse):
        return LiveSendOutcome.SENT
    status = response.transport_response.status if response.transport_response is not None else None
    # A refusal without a Matrix error, as from a proxy, would refuse every later frame too.
    bare_refusal = response.status_code is None and status is not None and 400 <= status < 500
    if response.status_code not in _REFUSAL_ERRCODES and not bare_refusal:
        return LiveSendOutcome.RETRY
    logger.info("live_frames_refused", room_id=room_id, errcode=response.status_code, status=status)
    return LiveSendOutcome.STOP
