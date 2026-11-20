"""Who may invite people by email, and which rooms refuse it."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import aiohttp
import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_membership
from mindroom.matrix.personal_room_store import is_personal_room
from mindroom.matrix.room_power import invite_power_level, user_power_level
from mindroom.matrix.room_reconciliation import read_room_creators
from mindroom.matrix.state import resolve_room_aliases

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_Kind = Literal["personal", "onboarding", "space", "unknown"]


async def may_invite(client: nio.AsyncClient, room_id: str, user_id: str, approvers: Sequence[str] = ()) -> bool | None:
    """Return whether a user may invite to the room from live state, or None when the state is unreadable.

    A listed approver may invite anywhere; anyone else must be joined at or above the room's `invite` level.
    """
    if user_id in approvers:
        return True
    try:
        membership = await get_room_membership(client, room_id, user_id)
        if membership != "join":
            return None if membership is None else False
        power_levels = await client.room_get_state_event(room_id, "m.room.power_levels")
        creators = await read_room_creators(client, room_id)
    except (aiohttp.ClientError, TimeoutError):
        logger.warning("email_invite_room_state_unreadable", room_id=room_id, exc_info=True)
        return None
    if creators is None or not isinstance(power_levels, nio.RoomGetStateEventResponse):
        logger.warning("email_invite_room_state_unreadable", room_id=room_id, error=str(power_levels))
        return None
    needed = invite_power_level(power_levels.content)
    return None if needed is None else user_power_level(power_levels.content, user_id, creators) >= needed


async def refused_room_kind(client: nio.AsyncClient, config: Config, paths: RuntimePaths, room_id: str) -> _Kind | None:
    """Return why a room refuses email invites, or "unknown" when its create event is unreadable."""
    if await asyncio.to_thread(is_personal_room, paths, room_id):
        return "personal"
    onboarding = config.personal_rooms.onboarding_rooms if config.personal_rooms is not None else []
    if room_id in resolve_room_aliases(onboarding, paths):
        return "onboarding"
    create = await client.room_get_state_event(room_id, "m.room.create")
    if not isinstance(create, nio.RoomGetStateEventResponse):
        return "unknown"
    return "space" if create.content.get("type") == "m.space" else None
