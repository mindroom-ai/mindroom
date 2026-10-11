"""Agents can discover authorized Matrix targets without a thread registry."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import nio
import pytest

from mindroom.authorization import ensure_room_membership_synced
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.custom_tools.matrix_room import MatrixRoomTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    test_runtime_paths,
)
from tests.identity_helpers import actual_entity_usernames, entity_ids

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import AsyncMock

    from mindroom.runtime_protocols import OrchestratorRuntime


pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.fixture
def context(tmp_path: Path) -> ToolRuntimeContext:
    """Build an ad-hoc room with allowed, forbidden, and absent configured agents."""
    paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            name: AgentConfig(
                display_name=name.title(),
                role=f"Work as {name}",
                thread_mode="room" if name == "code" else "thread",
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                    users=[] if name == "blocked" else ["@alice:localhost"],
                ),
            )
            for name in ("general", "code", "blocked", "absent")
        },
    )
    ids = entity_ids(config, paths, usernames=actual_entity_usernames(config))
    room = nio.MatrixRoom("!room:localhost", ids["general"].full_id)
    for name in ("general", "code", "blocked"):
        room.add_member(ids[name].full_id, config.agents[name].display_name, None)
    room.add_member("@alice:localhost", "Alice", None)
    room.members_synced = True
    client = make_matrix_client_mock()
    client.rooms = {room.room_id: room}
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id=room.room_id, thread_id="$current", reply_to_event_id=None),
        requester_id="@alice:localhost",
        client=client,
        config=config,
        runtime_paths=paths,
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
        room=room,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["normal", "revoked", "not_running", "configured_room", "failed_snapshot", "cached_room"],
)
async def test_agent_discovery_reports_only_available_authorized_targets(
    context: ToolRuntimeContext,
    scenario: str,
) -> None:
    """Discovery shares ingress eligibility and preserves exact mention IDs and thread mode."""
    MatrixRoomTools._recent_actions.clear()
    expected = ["code", "general"]
    if scenario == "revoked":
        current = context.config.model_copy(deep=True)
        access = current.agents["code"].access
        assert access is not None
        access.users = []
        context = replace(context, config_provider=lambda: current)
        expected = ["general"]
    elif scenario == "not_running":
        context = replace(
            context,
            orchestrator=cast(
                "OrchestratorRuntime",
                SimpleNamespace(
                    agent_bots={"general": SimpleNamespace(running=True), "code": SimpleNamespace(running=False)},
                ),
            ),
        )
        expected = ["general"]
    elif scenario == "configured_room":
        context.config.agents["general"].rooms = [context.room_id]
        expected = ["general"]
    elif scenario == "cached_room":
        context = replace(context, room=None)
    elif scenario == "failed_snapshot":
        assert context.room is not None
        context.room.members_synced = False
        cast("AsyncMock", context.client.joined_members).side_effect = TimeoutError("membership unavailable")
        context = replace(context, membership_turn_id="$turn")
        assert not await ensure_room_membership_synced(context.client, context.room, sender_id=context.requester_id)

    with tool_runtime_context(context):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="agents"))

    assert payload["status"] == "ok"
    assert [row["name"] for row in payload["agents"]] == expected
    by_name = {row["name"]: row for row in payload["agents"]}
    assert by_name["general"]["matrix_user_id"] == "@actual_general:localhost"
    assert by_name["general"]["thread_mode"] == "thread"
    assert "Work as general" in by_name["general"]["description"]
    if "code" in by_name:
        assert by_name["code"]["thread_mode"] == "room"
    if scenario == "failed_snapshot":
        assert cast("AsyncMock", context.client.joined_members).await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize("cached", [True, False])
async def test_agent_discovery_in_another_room(context: ToolRuntimeContext, authorized: bool, cached: bool) -> None:
    """The room access gate runs before discovery and target membership controls the result."""
    MatrixRoomTools._recent_actions.clear()
    other = nio.MatrixRoom("!other:localhost", "@actual_general:localhost")
    other.add_member("@actual_code:localhost", "Code", None)
    other.add_member("@alice:localhost", "Alice", None)
    other.members_synced = True
    # Cross-room access also requires the requester's own membership, which is
    # always read from the homeserver rather than the cached room projection.
    cast("AsyncMock", context.client.joined_members).return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(user_id="@actual_code:localhost", display_name="Code", avatar_url=None),
            nio.RoomMember(user_id="@alice:localhost", display_name="Alice", avatar_url=None),
        ],
        room_id=other.room_id,
    )
    if cached:
        context.client.rooms[other.room_id] = other
    if not authorized:
        current = context.config.model_copy(deep=True)
        access = current.agents["general"].access
        assert access is not None
        access.users = []
        context = replace(context, config_provider=lambda: current)

    with tool_runtime_context(context):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="agents", room_id=other.room_id))

    if authorized:
        assert payload["status"] == "ok"
        assert [row["name"] for row in payload["agents"]] == ["code"]
    else:
        assert payload["status"] == "error"
        assert "Not authorized" in payload["message"]
    assert cast("AsyncMock", context.client.joined_members).await_count == (
        0 if not authorized else 1 + int(not cached)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_agent_discovery_denies_room_the_requester_has_not_joined(
    context: ToolRuntimeContext,
    cached: bool,
) -> None:
    """A user grant must not expose the agents of rooms the requester is not part of."""
    MatrixRoomTools._recent_actions.clear()
    other = nio.MatrixRoom("!other:localhost", "@actual_general:localhost")
    other.add_member("@actual_code:localhost", "Code", None)
    other.members_synced = True
    cast("AsyncMock", context.client.joined_members).return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id="@actual_code:localhost", display_name="Code", avatar_url=None)],
        room_id=other.room_id,
    )
    if cached:
        context.client.rooms[other.room_id] = other

    with tool_runtime_context(context):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="agents", room_id=other.room_id))

    assert payload["status"] == "error"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_agent_discovery_includes_authorized_teams(context: ToolRuntimeContext) -> None:
    """Matrix conversations can address a team using its exact registered Matrix ID."""
    MatrixRoomTools._recent_actions.clear()
    context.config.teams["helpers"] = TeamConfig(
        display_name="Helpers",
        role="Coordinate helpers",
        agents=["general", "code"],
        access=ResponderAccessConfig(current_room_members=False, users=["@alice:localhost"]),
    )
    ids = entity_ids(context.config, context.runtime_paths, usernames=actual_entity_usernames(context.config))
    assert context.room is not None
    context.room.add_member(ids["helpers"].full_id, "Helpers", None)
    with tool_runtime_context(context):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="agents"))
    team = next(row for row in payload["agents"] if row["name"] == "helpers")
    assert team["matrix_user_id"] == ids["helpers"].full_id
    assert team["thread_mode"] == "thread"
    assert "Team of agents: general, code" in team["description"]
