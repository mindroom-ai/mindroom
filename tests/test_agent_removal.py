"""Test that agents leave all rooms when removed from configuration."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import MultiAgentOrchestrator
from mindroom.models import AgentConfig, Config, TeamConfig


class TestAgentRemoval:
    """Test suite for agent removal and room cleanup."""

    @pytest.fixture
    def mock_ensure_all_agent_users(self):
        """Mock ensure_all_agent_users to return fake agent users."""
        with patch("mindroom.bot.ensure_all_agent_users") as mock:
            mock.return_value = {
                "router": MagicMock(user_id="@router:example.com"),
                "agent1": MagicMock(user_id="@agent1:example.com"),
                "team1": MagicMock(user_id="@team1:example.com"),
            }
            yield mock

    @pytest.fixture
    def mock_create_agent_user(self):
        """Mock create_agent_user to return fake agent users."""
        with patch("mindroom.matrix.users.create_agent_user") as mock:

            async def side_effect(homeserver, agent_name, display_name):
                return MagicMock(user_id=f"@{agent_name}:example.com")

            mock.side_effect = side_effect
            yield mock

    @pytest.fixture
    def initial_config(self):
        """Create initial config with an agent and a team."""
        return Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent1",
                    role="Test agent",
                    tools=[],
                    instructions=[],
                    rooms=["room1", "room2"],
                ),
            },
            teams={
                "team1": TeamConfig(
                    display_name="Team1",
                    role="Test team",
                    agents=["agent1"],
                    rooms=["room2", "room3"],
                    model="default",
                    mode="collaborate",
                ),
            },
        )

    @pytest.fixture
    def config_without_agent(self):
        """Create config with agent1 removed."""
        return Config(
            agents={},
            teams={
                "team1": TeamConfig(
                    display_name="Team1",
                    role="Test team",
                    agents=[],  # Agent removed from team too
                    rooms=["room2", "room3"],
                    model="default",
                    mode="collaborate",
                ),
            },
        )

    @pytest.fixture
    def config_without_team(self):
        """Create config with team1 removed."""
        return Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent1",
                    role="Test agent",
                    tools=[],
                    instructions=[],
                    rooms=["room1", "room2"],
                ),
            },
            teams={},
        )

    @pytest.mark.asyncio
    async def test_agent_removal_leaves_rooms(
        self,
        mock_ensure_all_agent_users,
        mock_create_agent_user,
        initial_config,
        config_without_agent,
    ):
        """Test that removing an agent from config causes it to leave all rooms."""
        # Create orchestrator with initial config
        orchestrator = MultiAgentOrchestrator(storage_path=Path("/tmp"))
        orchestrator.current_config = initial_config

        # Create mock bots
        mock_agent_bot = MagicMock()
        mock_agent_bot.stop = AsyncMock()
        mock_team_bot = MagicMock()
        mock_team_bot.stop = AsyncMock()
        mock_router_bot = MagicMock()
        mock_router_bot.stop = AsyncMock()

        orchestrator.agent_bots = {
            "agent1": mock_agent_bot,
            "team1": mock_team_bot,
            "router": mock_router_bot,
        }

        # Patch load_config and other dependencies
        with (
            patch("mindroom.bot.load_config", return_value=config_without_agent),
            patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
        ):
            # Mock create_bot_for_entity to return None for removed agents
            def create_bot_side_effect(entity_name, agent_user, config, storage_path):
                if entity_name == "agent1":
                    return None  # Agent removed
                elif entity_name == "team1" or entity_name == "router":
                    new_bot = MagicMock()
                    new_bot.start = AsyncMock()
                    new_bot.sync_forever = AsyncMock()
                    return new_bot
                return None

            mock_create_bot.side_effect = create_bot_side_effect

            updated = await orchestrator.update_config()

        # Verify update happened
        assert updated is True

        # Verify agent1 was stopped with leave_rooms=True
        mock_agent_bot.stop.assert_called_once_with(leave_rooms=True)

        # Verify team1 was restarted (config changed) but not asked to leave rooms
        mock_team_bot.stop.assert_called_once_with(leave_rooms=False)

        # Verify router was restarted (rooms changed) but not asked to leave rooms
        mock_router_bot.stop.assert_called_once_with(leave_rooms=False)

    @pytest.mark.asyncio
    async def test_team_removal_leaves_rooms(
        self,
        mock_ensure_all_agent_users,
        mock_create_agent_user,
        initial_config,
        config_without_team,
    ):
        """Test that removing a team from config causes it to leave all rooms."""
        # Create orchestrator with initial config
        orchestrator = MultiAgentOrchestrator(storage_path=Path("/tmp"))
        orchestrator.current_config = initial_config

        # Create mock bots
        mock_agent_bot = MagicMock()
        mock_agent_bot.stop = AsyncMock()
        mock_team_bot = MagicMock()
        mock_team_bot.stop = AsyncMock()
        mock_router_bot = MagicMock()
        mock_router_bot.stop = AsyncMock()

        orchestrator.agent_bots = {
            "agent1": mock_agent_bot,
            "team1": mock_team_bot,
            "router": mock_router_bot,
        }

        # Patch load_config and other dependencies
        with (
            patch("mindroom.bot.load_config", return_value=config_without_team),
            patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
        ):
            # Mock create_bot_for_entity to return None for removed teams
            def create_bot_side_effect(entity_name, agent_user, config, storage_path):
                if entity_name == "team1":
                    return None  # Team removed
                elif entity_name == "router":
                    new_bot = MagicMock()
                    new_bot.start = AsyncMock()
                    new_bot.sync_forever = AsyncMock()
                    return new_bot
                return None

            mock_create_bot.side_effect = create_bot_side_effect

            updated = await orchestrator.update_config()

        # Verify update happened
        assert updated is True

        # Verify team1 was stopped with leave_rooms=True
        mock_team_bot.stop.assert_called_once_with(leave_rooms=True)

        # Verify agent1 was not restarted (no config change)
        mock_agent_bot.stop.assert_not_called()

        # Verify router was restarted (rooms changed) but not asked to leave rooms
        mock_router_bot.stop.assert_called_once_with(leave_rooms=False)

    @pytest.mark.asyncio
    async def test_agent_modification_no_leave_rooms(
        self,
        mock_ensure_all_agent_users,
        mock_create_agent_user,
        initial_config,
    ):
        """Test that modifying an agent (not removing) doesn't trigger leave_rooms."""
        # Create orchestrator with initial config
        orchestrator = MultiAgentOrchestrator(storage_path=Path("/tmp"))
        orchestrator.current_config = initial_config

        # Create modified config with agent1 rooms changed
        modified_config = Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent1",
                    role="Test agent modified",  # Role changed
                    tools=[],
                    instructions=[],
                    rooms=["room1", "room3"],  # Rooms changed
                ),
            },
            teams=initial_config.teams,
        )

        # Create mock bots
        mock_agent_bot = MagicMock()
        mock_agent_bot.stop = AsyncMock()
        mock_team_bot = MagicMock()
        mock_team_bot.stop = AsyncMock()
        mock_router_bot = MagicMock()
        mock_router_bot.stop = AsyncMock()

        orchestrator.agent_bots = {
            "agent1": mock_agent_bot,
            "team1": mock_team_bot,
            "router": mock_router_bot,
        }

        # Patch load_config and other dependencies
        with (
            patch("mindroom.bot.load_config", return_value=modified_config),
            patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
        ):
            # Mock create_bot_for_entity to return bots for all entities
            def create_bot_side_effect(entity_name, agent_user, config, storage_path):
                new_bot = MagicMock()
                new_bot.start = AsyncMock()
                new_bot.sync_forever = AsyncMock()
                return new_bot

            mock_create_bot.side_effect = create_bot_side_effect

            updated = await orchestrator.update_config()

        # Verify update happened
        assert updated is True

        # Verify agent1 was stopped but NOT with leave_rooms=True
        mock_agent_bot.stop.assert_called_once_with(leave_rooms=False)

        # Verify team1 was not restarted (no config change)
        mock_team_bot.stop.assert_not_called()

        # Verify router was not restarted (total room set unchanged: room1, room2, room3 in both)
        mock_router_bot.stop.assert_not_called()
