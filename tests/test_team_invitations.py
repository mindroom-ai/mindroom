"""Tests for team room membership functionality.

With the new self-managing agent pattern, teams handle their own room
memberships just like agents do.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.bot import TeamBot
from mindroom.config import AgentConfig, Config, RouterConfig, TeamConfig
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD


@pytest.fixture
def mock_config_with_teams() -> Config:
    """Create a mock config with agents and teams."""
    return Config(
        agents={
            "agent1": AgentConfig(
                display_name="Agent 1",
                role="Test agent",
                rooms=["test_room"],
            ),
        },
        teams={
            "team1": TeamConfig(
                display_name="Team 1",
                role="Test team",
                agents=["agent1"],
                rooms=["test_room"],
            ),
        },
    )


class TestTeamRoomMembership:
    """Test team room membership functionality."""

    @pytest.mark.asyncio
    async def test_team_joins_configured_rooms(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that teams join their configured rooms on startup."""
        # Create a mock team user
        team_user = AgentMatrixUser(
            agent_name="team1",
            user_id="@mindroom_team1:localhost",
            display_name="Team 1",
            password=TEST_PASSWORD,
        )

        # Create the team bot with configured rooms
        config = Config(router=RouterConfig(model="default"))
        # Convert agent names to MatrixID objects
        team_matrix_ids = [MatrixID.from_username("agent1", config.domain)]
        bot = TeamBot(
            agent_user=team_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test_room:localhost"],
            team_agents=team_matrix_ids,
            team_mode="round_robin",
            team_model=None,
            enable_streaming=False,
        )

        # Mock the client
        mock_client = AsyncMock()
        bot.client = mock_client

        # Track which rooms were joined
        joined_rooms = []

        async def mock_join_room(_client: object, room_id: str) -> bool:
            joined_rooms.append(room_id)
            return True

        monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)

        # Mock restore_scheduled_tasks
        async def mock_restore_scheduled_tasks(_client: object, _room_id: str, _config: Config) -> int:
            return 0

        monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

        # Test that the team joins its configured room
        await bot.join_configured_rooms()

        # Verify the team joined the configured room
        assert len(joined_rooms) == 1
        assert "!test_room:localhost" in joined_rooms

    @pytest.mark.asyncio
    async def test_team_leaves_unconfigured_rooms(self, tmp_path: Path) -> None:
        """Test that teams leave rooms they're no longer configured for."""
        # Create a mock team user
        team_user = AgentMatrixUser(
            agent_name="team1",
            user_id="@mindroom_team1:localhost",
            display_name="Team 1",
            password=TEST_PASSWORD,
        )

        # Create the team bot with no configured rooms
        config = Config(router=RouterConfig(model="default"))
        # Convert agent names to MatrixID objects
        team_matrix_ids = [MatrixID.from_username("agent1", config.domain)]
        bot = TeamBot(
            agent_user=team_user,
            storage_path=tmp_path,
            config=config,
            rooms=[],  # No configured rooms
            team_agents=team_matrix_ids,
            team_mode="round_robin",
            team_model=None,
            enable_streaming=False,
        )

        # Mock the client
        mock_client = AsyncMock()
        bot.client = mock_client

        # Mock joined_rooms to return a room the team is in
        joined_rooms_response = MagicMock()
        joined_rooms_response.__class__ = nio.JoinedRoomsResponse
        joined_rooms_response.rooms = ["!old_room:localhost"]
        mock_client.joined_rooms.return_value = joined_rooms_response

        # Track which rooms were left
        left_rooms = []

        async def mock_room_leave(room_id: str) -> MagicMock:
            left_rooms.append(room_id)
            response = MagicMock()
            response.__class__ = nio.RoomLeaveResponse
            return response

        mock_client.room_leave = mock_room_leave

        # Test that the team leaves unconfigured rooms
        await bot.leave_unconfigured_rooms()

        # Verify the team left the old room
        assert len(left_rooms) == 1
        assert "!old_room:localhost" in left_rooms
