"""User-facing note for agents that a managed room's configuration does not list."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.authorization import configured_responder_entities_for_room, is_sender_allowed_for_responder
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID

_ROOM_RULE = "This room is managed in the MindRoom configuration"
_MAX_NAMED_AGENTS = 3


def managed_room_notice(*, available: Sequence[str], unavailable: Sequence[str] = ()) -> str:
    """Explain why agents cannot answer in a managed room and where to talk to them instead.

    Leave ``unavailable`` empty when the agent speaks about itself.
    ``available`` holds the display names of the agents the reader can ask in this room.
    """
    if not unavailable:
        text = (
            f"{_ROOM_RULE}, and I'm not one of its agents, so I can't answer here. "
            "To talk to me, create a new room and invite me."
        )
    elif len(unavailable) == 1:
        name = unavailable[0]
        text = (
            f"{_ROOM_RULE}, and {name} isn't one of its agents, so it can't answer here. "
            f"To talk to {name}, create a new room and invite it."
        )
    else:
        text = (
            f"{_ROOM_RULE}, and {_join_names(unavailable, 'and')} aren't among its agents, so they can't answer here. "
            "To talk to them, create a new room and invite them."
        )
    if available:
        text += f" In this room, you can ask {_join_names(available, 'or')}."
    return text


def responder_display_names(responders: Sequence[MatrixID], config: Config, runtime_paths: RuntimePaths) -> list[str]:
    """Return the configured display names of responder Matrix IDs."""
    registry = entity_identity_registry(config, runtime_paths)
    names = (registry.current_entity_name_for_user_id(responder.full_id) for responder in responders)
    return [config.entity_display_name(name) for name in names if name is not None]


def managed_room_join_notice(
    room: nio.MatrixRoom,
    *,
    agent_name: str,
    agent_user_id: str,
    inviter: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> str | None:
    """Return the note an invited entity posts when the managed room it joined does not list it."""
    if agent_name == ROUTER_AGENT_NAME:
        return None
    configured_responders = configured_responder_entities_for_room(room, config, runtime_paths)
    if configured_responders is None or agent_user_id in {responder.full_id for responder in configured_responders}:
        return None

    def inviter_may_address(entity_name: str) -> bool:
        return is_sender_allowed_for_responder(
            inviter,
            entity_name,
            room.room_id,
            config,
            runtime_paths,
            membership_index,
        )

    if not inviter_may_address(agent_name):
        return None
    registry = entity_identity_registry(config, runtime_paths)
    addressable = [
        responder
        for responder in configured_responders
        if (name := registry.current_entity_name_for_user_id(responder.full_id)) is not None
        and inviter_may_address(name)
    ]
    return managed_room_notice(available=responder_display_names(addressable, config, runtime_paths))


def _join_names(names: Sequence[str], conjunction: str) -> str:
    """Join display names into one phrase, naming at most a few agents."""
    shown = list(names[:_MAX_NAMED_AGENTS])
    if len(names) > _MAX_NAMED_AGENTS:
        shown.append(f"{len(names) - _MAX_NAMED_AGENTS} others")
    if len(shown) == 1:
        return shown[0]
    if len(shown) == 2:
        return f"{shown[0]} {conjunction} {shown[1]}"
    return f"{', '.join(shown[:-1])}, {conjunction} {shown[-1]}"
