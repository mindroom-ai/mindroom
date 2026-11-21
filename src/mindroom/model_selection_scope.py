"""Authoritative Matrix membership and readable root scope for model selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.authorization import responder_candidate_entities_from_cached_room
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.conversation_hydration import readable_event
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.joined_members import read_joined_members

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


__all__ = ["ModelPickerScope", "validate_model_picker_scope"]


@dataclass(frozen=True, slots=True)
class ModelPickerScope:
    """Currently joined agents and requester-visible responding entities, each with its account's Matrix ID."""

    agent_user_ids: tuple[str, ...]
    entity_user_ids: tuple[tuple[str, str], ...]

    @property
    def entity_names(self) -> tuple[str, ...]:
        """The responding entities' names, in the order of their accounts."""
        return tuple(name for name, _ in self.entity_user_ids)


async def _joined_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Read current joined membership without modifying nio's owned room projection."""
    current = client.rooms.get(room_id)
    if current is None:
        return None
    response = await read_joined_members(client, room_id)
    if not isinstance(response, nio.JoinedMembersResponse) or response.room_id != room_id:
        return None
    room = nio.MatrixRoom(room_id, client.user_id)
    room.canonical_alias = current.canonical_alias
    for member in response.members:
        room.add_member(member.user_id, member.display_name, member.avatar_url)
    room.members_synced = True
    return room


async def _root_denial(client: nio.AsyncClient, room_id: str, thread_id: str) -> str | None:
    """Return why the thread root is not a readable root of this room, or None."""
    response = await client.room_get_event(room_id, thread_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return "thread_root_unavailable"
    event = readable_event(client, response.event)
    if not isinstance(event, nio.RoomMessage) or event.event_id != thread_id:
        return "thread_root_unreadable"
    source = event.source
    if source.get("room_id", room_id) != room_id or "redacted_because" in source.get("unsigned", {}):
        return "thread_root_unreadable"
    # A thread child, edit, or plain reply is not proof of a root. Inspect the
    # readable event only: encrypted outer relation hints are untrusted.
    return None if EventInfo.from_event(source).can_be_thread_root else "not_thread_root"


async def validate_model_picker_scope(  # noqa: PLR0911
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    room_id: str,
    requester_user_id: str,
    thread_id: str | None,
) -> ModelPickerScope | str:
    """Require the requester, the answering account and eligible agents actually joined, and an optional root.

    A denied scope returns a short stable reason for diagnostic logs.
    """
    registry = entity_identity_registry(config, runtime_paths)
    required = {requester_user_id, client.user_id}
    try:
        before = await _joined_room(client, room_id)
        if before is None:
            return "room_unavailable"
        if not required.issubset(before.users):
            return "not_joined"
        if thread_id is not None and (denial := await _root_denial(client, room_id, thread_id)) is not None:
            return denial
        after = await _joined_room(client, room_id)
    except (nio.EncryptionError, OSError, TimeoutError):
        return "scope_check_error"
    if after is None:
        return "room_unavailable"
    if not required.issubset(after.users):
        return "not_joined"
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
    entity_user_ids = tuple((name, user_id) for user_id, name in entities)
    return ModelPickerScope(agents, entity_user_ids) if agents else "no_addressable_agent"
