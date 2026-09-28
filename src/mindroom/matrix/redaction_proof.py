"""Ask the homeserver whether it applied one redaction.

A redaction event names its target by ID alone, and a homeserver passes one
along even when its sender had no right to redact the target, without applying
it. Before MindRoom destroys persisted history for a redaction, it asks the
homeserver whether the target is now redacted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

if TYPE_CHECKING:
    from mindroom.runtime_protocols import SupportsClientConfig

# Answers about the event itself that no retry changes for this account.
_HIDDEN_EVENT_ERRCODES = frozenset({"M_NOT_FOUND", "M_FORBIDDEN"})


async def homeserver_applied_redaction(runtime: SupportsClientConfig, room_id: str, event_id: str) -> bool:
    """Return whether the homeserver now serves this event redacted.

    False when it serves the event unredacted or will not show it to this
    account. Raises when it cannot answer now, so the caller can retry.
    """
    client = runtime.client
    if client is None:
        msg = "Matrix client is not ready to confirm a redaction"
        raise RuntimeError(msg)
    response = await client.room_get_event(room_id, event_id)
    if isinstance(response, nio.RoomGetEventResponse):
        source = response.event.source
        if source.get("event_id") != event_id:
            return False
        unsigned = source.get("unsigned")
        return (isinstance(unsigned, dict) and "redacted_because" in unsigned) or source.get("content") == {}
    if isinstance(response, nio.RoomGetEventError) and response.status_code in _HIDDEN_EVENT_ERRCODES:
        return False
    msg = f"Cannot read event {event_id!r} in {room_id!r} to confirm its redaction: {response}"
    raise RuntimeError(msg)
