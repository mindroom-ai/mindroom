"""Sending one live frame: Megolm-encrypted in an encrypted room, over the homeserver's MSC2477 ephemeral route."""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import TYPE_CHECKING

import nio

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
    # Too big to send as a frame; frames stop and durable edits take over.
    TOO_LARGE = "too_large"
    # The homeserver or the room refuses frames; frames stop for this reply.
    STOP = "stop"


# What a refusal means for later frames; any other error is retried with a snapshot. A rate limit never gets here:
# nio retries it with the same transaction ID within the publisher's send timeout.
_REFUSAL_OUTCOMES = {
    "M_TOO_LARGE": LiveSendOutcome.TOO_LARGE,
    "M_UNRECOGNIZED": LiveSendOutcome.STOP,
    "M_FORBIDDEN": LiveSendOutcome.STOP,
    "M_UNKNOWN": LiveSendOutcome.STOP,
    "M_NOT_FOUND": LiveSendOutcome.STOP,
}


def _serialized_size(content: Mapping[str, object]) -> int:
    return len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode())


async def send_live_frame(
    client: nio.AsyncClient,
    room_id: str,
    frame: dict[str, object],
    *,
    transaction_id: str,
) -> LiveSendOutcome:
    """Send one frame to the room, encrypting it with the room's current group session when the room is encrypted."""
    room = client.rooms.get(room_id)
    if room is None:
        return LiveSendOutcome.STOP
    content: dict[str, object] = frame
    if room.encrypted:
        if _serialized_size(frame) > _MAX_FRAME_BYTES:
            return LiveSendOutcome.TOO_LARGE
        # Frames do not count toward count-based rotation: at ten frames a second they would rotate the session every
        # ten seconds, each rotation sharing a new key with every device. Age and membership rotation still apply.
        encrypted = await client.room_encrypt(
            room_id,
            LIVE_EVENT_TYPE,
            frame,
            ignore_unverified_devices=True,
            count_toward_rotation=False,
        )
        content = {"v": FRAME_VERSION, "encrypted": encrypted}
    if _serialized_size(content) > _MAX_FRAME_BYTES:
        return LiveSendOutcome.TOO_LARGE
    response = await client.room_send_ephemeral(room_id, LIVE_EVENT_TYPE, content, tx_id=transaction_id)
    if isinstance(response, nio.RoomSendEphemeralResponse):
        return LiveSendOutcome.SENT
    outcome = _REFUSAL_OUTCOMES.get(response.status_code or "", LiveSendOutcome.RETRY)
    status = response.transport_response.status if response.transport_response is not None else None
    if response.status_code is None and status is not None and 400 <= status < 500:
        # A refusal without a Matrix error, as from a proxy, would refuse every later frame too.
        outcome = LiveSendOutcome.STOP
    if outcome is LiveSendOutcome.STOP:
        logger.info("live_frames_refused", room_id=room_id, errcode=response.status_code)
    return outcome
