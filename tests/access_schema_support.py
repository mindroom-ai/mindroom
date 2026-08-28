"""Shared builders for membership-access behavior tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.matrix.state import MatrixState
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def membership_config(
    *,
    administrators: Sequence[str] = (),
    room_defaults: Mapping[str, object] | None = None,
    rooms: Mapping[str, Mapping[str, object]] | None = None,
    agent_rooms: Sequence[str] = (),
    access: Mapping[str, object] | None = None,
    credential_managers: Sequence[str] = (),
) -> Config:
    """Build one runtime-bound membership-mode config with a ``talent`` agent."""
    agent: dict[str, object] = {
        "display_name": "Talent",
        "role": "Talent assistant",
        "rooms": list(agent_rooms),
        "credential_managers": list(credential_managers),
    }
    if access is not None:
        agent["access"] = dict(access)
    data: dict[str, object] = {
        "access_model": "room_membership",
        "administrators": list(administrators),
        "agents": {"talent": agent},
    }
    if room_defaults is not None:
        data["room_defaults"] = dict(room_defaults)
    if rooms is not None:
        data["rooms"] = {room_key: dict(room) for room_key, room in rooms.items()}
    config = Config.model_validate(data)
    runtime_root = Path(tempfile.mkdtemp(prefix="mindroom-membership-access-test-"))
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


def _joined_members(room_id: str, user_ids: Sequence[str]) -> nio.JoinedMembersResponse:
    return nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in user_ids],
        room_id=room_id,
    )


async def membership_index(
    config: Config,
    memberships_by_room_key: Mapping[str, set[str]],
) -> AgentReplyMembershipIndex:
    """Build an authoritative ready membership index for configured grant rooms."""
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    room_ids: dict[str, str] = {}
    for room_key in sorted(memberships_by_room_key):
        room_id = f"!{room_key}:example.com"
        room_ids[room_key] = room_id
        state.add_room(room_key, room_id, f"#{room_key}:example.com", room_key.title())
    state.save(runtime_paths=runtime_paths)

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=list(room_ids.values()))
    client.joined_members.side_effect = [
        _joined_members(room_ids[room_key], sorted(memberships_by_room_key[room_key]))
        for room_key in sorted(memberships_by_room_key)
    ]
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)
    return index


def unresolved_membership_index(config: Config, room_key: str) -> AgentReplyMembershipIndex:
    """Build an index whose configured grant room has no authoritative identity."""
    del room_key
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="test_unresolved_room")
    return index
