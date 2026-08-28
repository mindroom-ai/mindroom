"""Resolve membership access configuration into immutable runtime policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from mindroom.config.access import ResponderAccessConfig, RoomJoinPolicy
    from mindroom.config.main import Config


@dataclass(frozen=True, slots=True)
class EffectiveRoomPolicy:
    """Complete desired Matrix state for one managed room."""

    join_policy: RoomJoinPolicy
    listed: bool
    encrypted: bool
    invite_users: tuple[str, ...]
    admins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EffectiveResponderAccess:
    """Complete conversation-access policy for one responder."""

    current_room_members: bool
    members_of_rooms: tuple[str, ...]
    users: tuple[str, ...]


def resolve_room_policy(config: Config, room_key: str) -> EffectiveRoomPolicy:
    """Resolve replacement-based defaults and overrides for one managed room."""
    managed_room_keys = {key for key in config.get_all_configured_rooms() if not key.startswith(("!", "#"))}
    if room_key not in managed_room_keys:
        msg = f"Unknown managed room: {room_key}"
        raise ValueError(msg)

    defaults = config.room_defaults
    override = config.rooms.get(room_key)
    return EffectiveRoomPolicy(
        join_policy=(override.join_policy if override is not None and override.join_policy is not None else defaults.join_policy),
        listed=override.listed if override is not None and override.listed is not None else defaults.listed,
        encrypted=(override.encrypted if override is not None and override.encrypted is not None else defaults.encrypted),
        invite_users=tuple(
            override.invite_users
            if override is not None and override.invite_users is not None
            else defaults.invite_users,
        ),
        admins=tuple(override.admins if override is not None and override.admins is not None else defaults.admins),
    )


def _resolved_access(
    authored: ResponderAccessConfig | None,
    *,
    default_current_room_members: bool,
    default_room_grants: tuple[str, ...],
) -> EffectiveResponderAccess:
    if authored is None:
        return EffectiveResponderAccess(
            current_room_members=default_current_room_members,
            members_of_rooms=default_room_grants,
            users=(),
        )
    return EffectiveResponderAccess(
        current_room_members=authored.current_room_members,
        members_of_rooms=(
            default_room_grants if authored.members_of_rooms is None else tuple(authored.members_of_rooms)
        ),
        users=tuple(authored.users),
    )


def resolve_responder_access(config: Config, entity_name: str) -> EffectiveResponderAccess:
    """Resolve authored access and entity-specific defaults exactly once."""
    if entity_name in config.agents:
        entity = config.agents[entity_name]
        return _resolved_access(
            entity.access,
            default_current_room_members=False,
            default_room_grants=tuple(entity.rooms),
        )
    if entity_name in config.teams:
        entity = config.teams[entity_name]
        return _resolved_access(
            entity.access,
            default_current_room_members=False,
            default_room_grants=tuple(entity.rooms),
        )
    if entity_name == ROUTER_AGENT_NAME:
        return _resolved_access(
            config.router.access,
            default_current_room_members=True,
            default_room_grants=(),
        )
    msg = f"Unknown responder: {entity_name}"
    raise ValueError(msg)
