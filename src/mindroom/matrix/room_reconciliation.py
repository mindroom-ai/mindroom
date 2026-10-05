"""Pass-local authoritative room state for administration and invitations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiohttp
import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.room_power import create_content_room_version, has_privileged_creators, privileged_creators

logger = get_logger(__name__)


@dataclass(frozen=True)
class RoomStateSnapshot:
    """One complete remote state read; never persisted or inferred from sync defaults."""

    room_id: str
    events: dict[tuple[str, str], dict[str, Any]]
    # Users the room version gives unlimited power: room version 12 creators, which power levels never list.
    creators: frozenset[str] = field(default_factory=frozenset)

    def present_user_ids(self) -> set[str]:
        """Return joined or invited users, both of which already satisfy invitation."""
        return {
            state_key
            for (event_type, state_key), content in self.events.items()
            if event_type == "m.room.member" and content.get("membership") in {"join", "invite"}
        }


async def read_room_state(client: nio.AsyncClient, room_id: str) -> RoomStateSnapshot | None:
    """Fetch complete state; unreadable state is not an empty room."""
    try:
        response = await client.room_get_state(room_id)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        logger.warning("room_reconciliation_state_read_failed", room_id=room_id, exc_info=True)
        return None
    if not isinstance(response, nio.RoomGetStateResponse) or response.room_id != room_id:
        logger.warning("room_reconciliation_state_unavailable", room_id=room_id, error=str(response))
        return None
    # Nio validates the state envelope, but not the content field.
    if any(not isinstance(event.get("content"), dict) for event in response.events):
        logger.warning("room_reconciliation_state_content_invalid", room_id=room_id)
        return None
    create_event = next((event for event in response.events if event["type"] == "m.room.create"), {})
    return RoomStateSnapshot(
        room_id,
        {(event["type"], event["state_key"]): dict(event["content"]) for event in response.events},
        privileged_creators(create_event),
    )


async def read_room_creators(
    client: nio.AsyncClient,
    room_id: str,
    *,
    snapshot: RoomStateSnapshot | None = None,
) -> frozenset[str] | None:
    """Return the users the room's version gives unlimited power, or None when the room state is unreadable.

    Only the full create event names its sender, so a standalone read of a version 12 room reads full state.
    """
    if snapshot is not None:
        return snapshot.creators
    create = await client.room_get_state_event(room_id, "m.room.create")
    if not isinstance(create, nio.RoomGetStateEventResponse) or not isinstance(create.content, dict):
        logger.warning("room_creators_unreadable", room_id=room_id, error=str(create))
        return None
    if not has_privileged_creators(create_content_room_version(create.content)):
        return frozenset()
    full_state = await read_room_state(client, room_id)
    return None if full_state is None else full_state.creators


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
