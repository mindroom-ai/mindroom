"""Authoritative Matrix membership and readable root scope for model selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.authorization import responder_candidate_entities_from_cached_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.conversation_hydration import readable_event
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


__all__ = ["ModelPickerScope", "validate_model_picker_scope"]


@dataclass(frozen=True, slots=True)
class ModelPickerScope:
    """Currently joined agents and requester-visible responding entities."""

    agent_user_ids: tuple[str, ...]
    entity_names: tuple[str, ...]


async def _joined_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Read current joined membership without modifying nio's owned room projection."""
    current = client.rooms.get(room_id)
    if current is None:
        return None
    response = await client.joined_members(room_id)
    if not isinstance(response, nio.JoinedMembersResponse) or response.room_id != room_id:
        return None
    room = nio.MatrixRoom(room_id, client.user_id)
    room.canonical_alias = current.canonical_alias
    for member in response.members:
        room.add_member(member.user_id, member.display_name, member.avatar_url)
    room.members_synced = True
    return room


async def _readable_root(client: nio.AsyncClient, room_id: str, thread_id: str) -> bool:
    response = await client.room_get_event(room_id, thread_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return False
    event = readable_event(client, response.event)
    if not isinstance(event, nio.RoomMessage) or event.event_id != thread_id:
        return False
    source = event.source
    if source.get("room_id", room_id) != room_id or "redacted_because" in source.get("unsigned", {}):
        return False
    # A thread child, edit, or plain reply is not proof of a root. Inspect the
    # readable event only: encrypted outer relation hints are untrusted.
    return EventInfo.from_event(source).can_be_thread_root


async def validate_model_picker_scope(
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    room_id: str,
    requester_user_id: str,
    thread_id: str | None,
) -> ModelPickerScope | None:
    """Require actual joined requester/router/eligible agents and an optional root."""
    registry = entity_identity_registry(config, runtime_paths)
    router_id = registry.current_id(ROUTER_AGENT_NAME).full_id
    if client.user_id != router_id:
        return None
    try:
        before = await _joined_room(client, room_id)
        if before is None or not {requester_user_id, router_id}.issubset(before.users):
            return None
        if thread_id is not None and not await _readable_root(client, room_id, thread_id):
            return None
        after = await _joined_room(client, room_id)
    except (nio.EncryptionError, OSError, TimeoutError):
        return None
    if after is None or not {requester_user_id, router_id}.issubset(after.users):
        return None
    candidates = responder_candidate_entities_from_cached_room(
        after,
        requester_user_id,
        config,
        runtime_paths,
        membership_index,
    )
    entities = tuple(
        (candidate.full_id, name)
        for candidate in candidates
        if candidate.full_id in after.users
        if (name := registry.current_entity_name_for_user_id(candidate.full_id, include_router=False)) is not None
    )
    agents = tuple(user_id for user_id, name in entities if name in config.agents)
    return ModelPickerScope(agents, tuple(name for _, name in entities)) if agents else None
