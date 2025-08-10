"""Tests for room cleanup behavior with thread invitations.

These tests ensure that invited agents are not kicked from rooms
when they have active thread invitations, even if they're not
configured for the room.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

import mindroom.room_cleanup
from mindroom.config import AgentConfig, Config, RouterConfig
from mindroom.room_cleanup import _cleanup_orphaned_bots_in_room, cleanup_all_orphaned_bots
from mindroom.thread_invites import ThreadInviteManager


@pytest.mark.asyncio
async def test_cleanup_preserves_invited_agents() -> None:
    """Test that agents with thread invitations are not kicked from rooms."""
    # Create mock client
    mock_client = AsyncMock()

    # Create config with agents defined (but not necessarily configured for this room)
    config = Config(
        agents={
            "router": AgentConfig(
                display_name="Router",
                role="Route messages",
                rooms=["!test:localhost"],
            ),
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=[],  # Not configured for any rooms
            ),
            "general": AgentConfig(
                display_name="General",
                role="General assistance",
                rooms=[],  # Not configured for any rooms
            ),
        },
        router=RouterConfig(model="default"),
    )

    # Create mock thread invite manager
    thread_invite_manager = ThreadInviteManager(mock_client)

    # Mock get_agent_threads to return thread IDs for calculator
    async def mock_get_agent_threads(room_id: str, agent_name: str) -> list[str]:
        if agent_name == "calculator" and room_id == "!test:localhost":
            return ["$thread1", "$thread2"]  # Calculator has 2 thread invitations
        return []

    thread_invite_manager.get_agent_threads = AsyncMock(side_effect=mock_get_agent_threads)

    # Mock get_room_members to return various bots
    async def mock_get_room_members(_client: AsyncMock, _room_id: str) -> list[str]:
        return [
            "@mindroom_calculator:localhost",  # Not configured but has thread invitations
            "@mindroom_general:localhost",  # Not configured, no invitations (should be kicked)
            "@mindroom_router:localhost",  # Configured for room
        ]

    # Track kicked bots
    kicked_bots = []

    async def mock_room_kick(_room_id: str, user_id: str, reason: str = "") -> MagicMock:  # noqa: ARG001
        kicked_bots.append(user_id)
        response = MagicMock()
        response.__class__ = nio.RoomKickResponse
        return response

    mock_client.room_kick = mock_room_kick

    # Patch the imports
    original_get_members = mindroom.room_cleanup.get_room_members
    original_get_bot_usernames = mindroom.room_cleanup._get_all_known_bot_usernames

    mindroom.room_cleanup.get_room_members = mock_get_room_members
    mindroom.room_cleanup._get_all_known_bot_usernames = lambda: {
        "mindroom_calculator",
        "mindroom_general",
        "mindroom_router",
    }

    try:
        # Run cleanup
        result = await _cleanup_orphaned_bots_in_room(mock_client, "!test:localhost", config, thread_invite_manager)

        # Verify only general was kicked (no thread invitations)
        assert len(kicked_bots) == 1
        assert "@mindroom_general:localhost" in kicked_bots
        assert "@mindroom_calculator:localhost" not in kicked_bots  # Has thread invitations
        assert "@mindroom_router:localhost" not in kicked_bots  # Router is always kept

        # Verify return value
        assert "mindroom_general" in result
        assert "mindroom_calculator" not in result

    finally:
        # Restore original functions
        mindroom.room_cleanup.get_room_members = original_get_members
        mindroom.room_cleanup._get_all_known_bot_usernames = original_get_bot_usernames


@pytest.mark.asyncio
async def test_cleanup_all_preserves_invited_agents() -> None:
    """Test that cleanup_all_orphaned_bots preserves agents with thread invitations."""
    # Create mock client
    mock_client = AsyncMock()

    # Mock joined_rooms
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room2:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Create config with agents defined
    config = Config(
        agents={
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=[],  # Not configured for any rooms
            ),
            "general": AgentConfig(
                display_name="General",
                role="General assistance",
                rooms=[],  # Not configured for any rooms
            ),
            "code": AgentConfig(
                display_name="Code",
                role="Code assistant",
                rooms=[],  # Not configured for any rooms
            ),
        },
        router=RouterConfig(model="default"),
    )

    # Create mock thread invite manager
    thread_invite_manager = ThreadInviteManager(mock_client)

    # Mock get_agent_threads - calculator has invitations in room1 only
    async def mock_get_agent_threads(room_id: str, agent_name: str) -> list[str]:
        if agent_name == "calculator" and room_id == "!room1:localhost":
            return ["$thread1"]
        return []

    thread_invite_manager.get_agent_threads = AsyncMock(side_effect=mock_get_agent_threads)

    # Mock get_room_members for each room
    async def mock_get_room_members(_client: AsyncMock, room_id: str) -> list[str]:
        if room_id == "!room1:localhost":
            return [
                "@mindroom_calculator:localhost",  # Has thread invitation here
                "@mindroom_general:localhost",  # No invitations
            ]
        # room2
        return [
            "@mindroom_calculator:localhost",  # No thread invitation in room2
            "@mindroom_code:localhost",  # No invitations
        ]

    # Track kicked bots per room
    kicked_bots_by_room: dict[str, list[str]] = {}

    async def mock_room_kick(room_id: str, user_id: str, reason: str = "") -> MagicMock:  # noqa: ARG001
        if room_id not in kicked_bots_by_room:
            kicked_bots_by_room[room_id] = []
        kicked_bots_by_room[room_id].append(user_id)
        response = MagicMock()
        response.__class__ = nio.RoomKickResponse
        return response

    mock_client.room_kick = mock_room_kick

    # Patch the imports
    original_get_members = mindroom.room_cleanup.get_room_members
    original_get_bot_usernames = mindroom.room_cleanup._get_all_known_bot_usernames
    original_get_joined = mindroom.room_cleanup.get_joined_rooms

    mindroom.room_cleanup.get_room_members = mock_get_room_members
    mindroom.room_cleanup._get_all_known_bot_usernames = lambda: {
        "mindroom_calculator",
        "mindroom_general",
        "mindroom_code",
        "mindroom_router",
    }
    mindroom.room_cleanup.get_joined_rooms = AsyncMock(return_value=["!room1:localhost", "!room2:localhost"])

    try:
        # Run cleanup
        result = await cleanup_all_orphaned_bots(mock_client, config, thread_invite_manager)

        # Verify room1: calculator kept (has invitation), general kicked
        assert "@mindroom_general:localhost" in kicked_bots_by_room.get("!room1:localhost", [])
        assert "@mindroom_calculator:localhost" not in kicked_bots_by_room.get("!room1:localhost", [])

        # Verify room2: both calculator and code kicked (no invitations)
        assert "@mindroom_calculator:localhost" in kicked_bots_by_room.get("!room2:localhost", [])
        assert "@mindroom_code:localhost" in kicked_bots_by_room.get("!room2:localhost", [])

        # Verify return value structure
        assert "!room1:localhost" in result
        assert "mindroom_general" in result["!room1:localhost"]
        assert "!room2:localhost" in result
        assert "mindroom_calculator" in result["!room2:localhost"]
        assert "mindroom_code" in result["!room2:localhost"]

    finally:
        # Restore original functions
        mindroom.room_cleanup.get_room_members = original_get_members
        mindroom.room_cleanup._get_all_known_bot_usernames = original_get_bot_usernames
        mindroom.room_cleanup.get_joined_rooms = original_get_joined
