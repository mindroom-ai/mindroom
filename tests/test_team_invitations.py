"""Tests for team invitation functionality."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.cli import _invite_agents_from_config
from mindroom.models import AgentConfig, Config, TeamConfig


@pytest.fixture
def mock_config_with_teams():
    """Create a mock config with agents and teams."""
    config = Config(
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
    return config


class TestTeamInvitations:
    """Test team invitation functionality."""

    @pytest.mark.asyncio
    async def test_invite_agents_from_config_includes_teams(self, mock_config_with_teams) -> None:
        """Test that _invite_agents_from_config invites both agents and teams."""
        mock_client = AsyncMock()

        # Mock successful room invite responses
        mock_invite_response = MagicMock()
        mock_invite_response.__class__ = nio.RoomInviteResponse
        mock_client.room_invite.return_value = mock_invite_response

        # Call the function
        invited_count = await _invite_agents_from_config(
            client=mock_client,
            room_id="!test_room:localhost",
            room_key="test_room",
            config=mock_config_with_teams,
            include_router=True,
        )

        # Should have invited router (1) + agent1 (1) + team1 (1) = 3
        assert invited_count == 3

        # Check that room_invite was called for all three
        assert mock_client.room_invite.call_count == 3

        # Check the specific calls
        calls = mock_client.room_invite.call_args_list
        invited_ids = [call[0][1] for call in calls]  # Get the user_id argument from each call

        assert "@mindroom_router:localhost" in invited_ids
        assert "@mindroom_agent1:localhost" in invited_ids
        assert "@mindroom_team1:localhost" in invited_ids

    @pytest.mark.asyncio
    async def test_invite_agents_from_config_only_invites_assigned_teams(self, mock_config_with_teams) -> None:
        """Test that teams are only invited to their assigned rooms."""
        # Add another team not assigned to test_room
        mock_config_with_teams.teams["team2"] = TeamConfig(
            display_name="Team 2",
            role="Another test team",
            agents=["agent1"],
            rooms=["other_room"],  # Different room
        )

        mock_client = AsyncMock()

        # Mock successful room invite responses
        mock_invite_response = MagicMock()
        mock_invite_response.__class__ = nio.RoomInviteResponse
        mock_client.room_invite.return_value = mock_invite_response

        # Call the function for test_room
        invited_count = await _invite_agents_from_config(
            client=mock_client,
            room_id="!test_room:localhost",
            room_key="test_room",
            config=mock_config_with_teams,
            include_router=True,
        )

        # Should have invited router (1) + agent1 (1) + team1 (1) = 3
        # team2 should NOT be invited as it's not assigned to test_room
        assert invited_count == 3

        # Check that room_invite was called for all three
        assert mock_client.room_invite.call_count == 3

        # Check the specific calls
        calls = mock_client.room_invite.call_args_list
        invited_ids = [call[0][1] for call in calls]  # Get the user_id argument from each call

        assert "@mindroom_router:localhost" in invited_ids
        assert "@mindroom_agent1:localhost" in invited_ids
        assert "@mindroom_team1:localhost" in invited_ids
        assert "@mindroom_team2:localhost" not in invited_ids  # Should NOT be invited
