"""Matrix room power levels across room versions.

Room version 12 (MSC4289, also `org.matrix.hydra.11`) gives a room's creators, the sender of `m.room.create`
plus its `additional_creators`, unlimited power, and servers reject power levels that list them in `users`.
Earlier versions list the creator in `users` like any other member.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_PRIVILEGED_CREATOR_PREVIEW_VERSION = "org.matrix.hydra.11"
_FIRST_PRIVILEGED_CREATOR_VERSION = 12
_DEFAULT_USER_POWER_LEVEL = 0
# The level Matrix clients show as room admin, and the one MindRoom grants the users it makes room admins.
ROOM_ADMIN_POWER_LEVEL = 100


def has_privileged_creators(room_version: str) -> bool:
    """Return whether rooms of this version give their creators unlimited power."""
    if room_version == _PRIVILEGED_CREATOR_PREVIEW_VERSION:
        return True
    return room_version.isdigit() and int(room_version) >= _FIRST_PRIVILEGED_CREATOR_VERSION


def create_content_room_version(create_content: Mapping[str, object]) -> str:
    """Return the room version a create event's content declares; the spec defaults it to "1"."""
    room_version = create_content.get("room_version", "1")
    return room_version if isinstance(room_version, str) else "1"


def privileged_creators(create_event: Mapping[str, object]) -> frozenset[str]:
    """Return the users a full `m.room.create` event gives unlimited power, empty before room version 12."""
    content = create_event.get("content")
    if not isinstance(content, dict):
        return frozenset()
    typed_content = cast("Mapping[str, object]", content)
    if not has_privileged_creators(create_content_room_version(typed_content)):
        return frozenset()
    sender = create_event.get("sender")
    additional = typed_content.get("additional_creators")
    creators = {sender} if isinstance(sender, str) else set()
    if isinstance(additional, list):
        creators.update(user_id for user_id in additional if isinstance(user_id, str))
    return frozenset(creators)


def user_power_level(
    power_levels_content: Mapping[str, object],
    user_id: str,
    creators: frozenset[str] = frozenset(),
) -> float:
    """Return one user's effective power level: unlimited for a privileged creator, else the power-level state."""
    if user_id in creators:
        return math.inf
    users = power_levels_content.get("users")
    if isinstance(users, dict) and (level := _level(cast("Mapping[str, object]", users).get(user_id))) is not None:
        return level
    users_default = _level(power_levels_content.get("users_default"))
    return users_default if users_default is not None else _DEFAULT_USER_POWER_LEVEL


def send_power_level(power_levels_content: Mapping[str, object], event_type: str) -> int:
    """Return the power level sending message events of ``event_type`` needs: its own entry, else ``events_default``, else 0."""
    events = power_levels_content.get("events")
    level = cast("Mapping[str, object]", events).get(event_type) if isinstance(events, dict) else None
    if type(level) is int:
        return level
    default = power_levels_content.get("events_default")
    return default if type(default) is int else 0


def _level(value: object) -> int | None:
    """Return a power level: an integer, or the integer string room versions before 10 allow; else None."""
    if type(value) is int:
        return value
    return int(value) if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value) else None


def invite_power_level(power_levels_content: Mapping[str, object]) -> int | None:
    """Return the power level inviting users needs: 0 when absent, None when the value is not a power level."""
    return _level(power_levels_content["invite"]) if "invite" in power_levels_content else 0
