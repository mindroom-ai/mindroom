"""Tests for agent self-managed room membership.

With the new self-managing agent pattern, agents handle their own room
memberships. This test module verifies that behavior.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config, RouterConfig, TeamConfig
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from nio.responses import Response


@pytest.fixture
def mock_config() -> Config:
    """Create a mock config with agents and teams."""
    return Config(
        agents={
            "agent1": AgentConfig(
                display_name="Agent 1",
                role="Test agent",
                rooms=["room1", "room2"],
            ),
            "agent2": AgentConfig(
                display_name="Agent 2",
                role="Another test agent",
                rooms=["room1"],
            ),
        },
        teams={
            "team1": TeamConfig(
                display_name="Team 1",
                role="Test team",
                agents=["agent1", "agent2"],
                rooms=["room2"],
            ),
        },
    )


@pytest.mark.asyncio
async def test_agent_joins_configured_rooms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that agents join their configured rooms on startup."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Create the agent bot with configured rooms
    config = Config(router=RouterConfig(model="default"))

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!room1:localhost", "!room2:localhost"],
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track which rooms were joined
    joined_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Test that the bot joins its configured rooms
    await bot.join_configured_rooms()

    # Verify the bot joined both configured rooms
    assert len(joined_rooms) == 2
    assert "!room1:localhost" in joined_rooms
    assert "!room2:localhost" in joined_rooms


@pytest.mark.asyncio
async def test_agent_leaves_unconfigured_rooms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG001
    """Test that agents leave rooms they're no longer configured for."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Create the agent bot with only room1 configured
    config = Config(router=RouterConfig(model="default"))

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!room1:localhost"],  # Only configured for room1
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Mock joined_rooms to return both room1 and room2 (agent is in both)
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room2:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Track which rooms were left
    left_rooms = []

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    mock_client.room_leave = mock_room_leave

    # Test that the bot leaves unconfigured rooms
    await bot.leave_unconfigured_rooms()

    # Verify the bot left room2 (unconfigured) but not room1 (configured)
    assert len(left_rooms) == 1
    assert "!room2:localhost" in left_rooms


@pytest.mark.asyncio
async def test_agent_manages_rooms_on_config_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that agents update their room memberships when configuration changes."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Start with agent configured for room1 only
    config = Config(router=RouterConfig(model="default"))

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!room1:localhost"],
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track room operations
    joined_rooms = []
    left_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    mock_client.room_leave = mock_room_leave

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock joined_rooms to return room1 and room3 (agent is in both)
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room3:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Update configuration: now configured for room1 and room2 (not room3)
    bot.rooms = ["!room1:localhost", "!room2:localhost"]

    # Apply room updates
    await bot.join_configured_rooms()
    await bot.leave_unconfigured_rooms()

    # Verify:
    # - Joined room2 (newly configured)
    # - Left room3 (no longer configured)
    # - Stayed in room1 (still configured)
    assert "!room2:localhost" in joined_rooms
    assert "!room3:localhost" in left_rooms
    assert "!room1:localhost" not in left_rooms  # Should stay in room1
