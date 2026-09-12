"""Pass-local authoritative room state for administration and invitations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import nio

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RoomStateSnapshot:
    """One complete remote state read; never persisted or inferred from sync defaults."""

    room_id: str
    events: dict[tuple[str, str], dict[str, Any]]

    def present_user_ids(self) -> set[str]:
        """Return joined or invited users, both of which already satisfy invitation."""
        return {
            state_key
            for (event_type, state_key), content in self.events.items()
            if event_type == "m.room.member" and content.get("membership") in {"join", "invite"}
        }


async def read_room_state(client: nio.AsyncClient, room_id: str) -> RoomStateSnapshot | None:
    """Fetch complete state; unreadable state is not an empty room."""
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse) or response.room_id != room_id:
        logger.warning("room_reconciliation_state_unavailable", room_id=room_id, error=str(response))
        return None
    return RoomStateSnapshot(
        room_id,
        {(event["type"], event["state_key"]): dict(event["content"]) for event in response.events},
    )


async def read_state_event(
    client: nio.AsyncClient,
    room_id: str,
    event_type: str,
    state_key: str = "",
    *,
    snapshot: RoomStateSnapshot | None = None,
) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
    """Read from an explicit complete snapshot or fetch for a standalone caller."""
    if snapshot is None:
        return await client.room_get_state_event(room_id, event_type, state_key)
    if snapshot.room_id != room_id:
        message = "Room state snapshot belongs to another room"
        raise ValueError(message)
    content = snapshot.events.get((event_type, state_key))
    if content is None:
        return nio.RoomGetStateEventError("State event absent", "M_NOT_FOUND", room_id=room_id)
    return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)
