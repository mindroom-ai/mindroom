"""Who may invite people by email, and which rooms refuse it."""

from __future__ import annotations

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


async def may_invite(
    client: nio.AsyncClient,
    room_id: str,
    user_id: str,
    approvers: Sequence[str] = (),
) -> bool | None:
    """Return whether a user may invite to the room from live state, or None when the state is unreadable.

    A listed approver may invite anywhere; anyone else must be joined at or above the room's `invite` level.
    """
    if user_id in approvers:
        return True
    try:
        membership = await get_room_membership(client, room_id, user_id)
        if membership is None:
            return None
        if membership != "join":
            return False
        power_levels = await client.room_get_state_event(room_id, "m.room.power_levels")
        if not isinstance(power_levels, nio.RoomGetStateEventResponse) or not isinstance(power_levels.content, dict):
            logger.warning("email_invite_power_levels_unreadable", room_id=room_id, error=str(power_levels))
            return None
        creators = await read_room_creators(client, room_id)
    except (aiohttp.ClientError, TimeoutError):
        logger.warning("email_invite_room_state_unreadable", room_id=room_id, exc_info=True)
        return None
    if creators is None:
        return None
    return user_power_level(power_levels.content, user_id, creators) >= invite_power_level(power_levels.content)


def refused_room_kind(config: Config, paths: RuntimePaths, room_id: str) -> Literal["personal", "onboarding"] | None:
    """Return why a room refuses email invites: it is a personal room or an onboarding room."""
    if is_personal_room(paths, room_id):
        return "personal"
    if config.personal_rooms is not None and room_id in resolve_room_aliases(
        config.personal_rooms.onboarding_rooms,
        paths,
    ):
        return "onboarding"
    return None
