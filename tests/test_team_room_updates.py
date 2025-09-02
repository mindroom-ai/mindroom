"""Tests for team room update functionality."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import MultiAgentOrchestrator
from mindroom.config import Config


class TestTeamRoomUpdates:
    """Test team room configuration updates."""

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for team room management
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_team_room_change_triggers_restart(self, tmp_path: Path) -> None:
        """Test that changing a team's room configuration triggers a restart."""
        # Create initial config
        initial_config_data: dict[str, Any] = {
            "agents": {
                "agent1": {
                    "display_name": "Agent1",
                    "role": "Test",
                    "tools": [],
                    "instructions": [],
                    "rooms": ["room1"],
                    "model": "default",
                },
            },
            "teams": {
                "team1": {
                    "display_name": "Team1",
                    "role": "Test team",
                    "agents": ["agent1"],
                    "rooms": ["room1", "room2"],
                    "model": "default",
                    "mode": "coordinate",
                },
            },
            "defaults": {"num_history_runs": 5, "markdown": True, "add_history_to_messages": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        with patch("mindroom.config.Config.from_yaml") as mock_load_config:
            config1 = Config.model_validate(initial_config_data)
            mock_load_config.return_value = config1

            with patch("mindroom.matrix.users.ensure_all_agent_users") as mock_ensure_users:
                mock_agent1_user = MagicMock(user_id="@agent1:localhost", agent_name="agent1")
                mock_team_user = MagicMock(user_id="@team1:localhost", agent_name="team1")
                mock_router_user = MagicMock(user_id="@router:localhost", agent_name="router")
                mock_ensure_users.return_value = {
                    "agent1": mock_agent1_user,
                    "team1": mock_team_user,
                    "router": mock_router_user,
                }

                # Mock topic generation to avoid calling AI
                async def mock_generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str:  # noqa: ARG001
                    return f"Test topic for {room_name}"

                # Also need to patch it in the rooms module where it's imported
                with (
                    patch("mindroom.topic_generator.generate_room_topic_ai", mock_generate_room_topic_ai),
                    patch("mindroom.matrix.rooms.generate_room_topic_ai", mock_generate_room_topic_ai),
                ):
                    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

                    with patch("mindroom.bot.create_bot_for_entity") as mock_create_bot:
                        mock_bot = AsyncMock()
                        mock_bot.start = AsyncMock()
                        mock_bot.stop = AsyncMock()
                        mock_bot.sync_forever = AsyncMock()
                        mock_create_bot.return_value = mock_bot

                        await orchestrator.initialize()
                        orchestrator.running = True

                        # Update config with different rooms for the team
                        updated_config_data = initial_config_data.copy()
                        updated_config_data["teams"]["team1"]["rooms"] = ["room2", "room3", "room4"]
                        config2 = Config.model_validate(updated_config_data)
                        mock_load_config.return_value = config2

                        # Update config
                        updated = await orchestrator.update_config()

                        # Verify the team was restarted
                        assert updated is True
                        assert mock_bot.stop.called

                        # Should create: agent1 + team1 + router on init, team1 + router on update
                        # (router gets recreated when teams change)
                        assert mock_create_bot.call_count == 5

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for team creation
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_new_team_gets_created(self, tmp_path: Path) -> None:
        """Test that a new team in config gets created."""
        # Start with no teams
        initial_config_data: dict[str, Any] = {
            "agents": {},
            "teams": {},
            "defaults": {"num_history_runs": 5, "markdown": True, "add_history_to_messages": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        with patch("mindroom.config.Config.from_yaml") as mock_load_config:
            config1 = Config.model_validate(initial_config_data)
            mock_load_config.return_value = config1

            with patch("mindroom.matrix.users.ensure_all_agent_users") as mock_ensure_users:
                mock_router_user = MagicMock(user_id="@router:localhost", agent_name="router")
                mock_ensure_users.return_value = {"router": mock_router_user}

                # Mock topic generation to avoid calling AI
                async def mock_generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str:  # noqa: ARG001
                    return f"Test topic for {room_name}"

                # Also need to patch it in the rooms module where it's imported
                with (
                    patch("mindroom.topic_generator.generate_room_topic_ai", mock_generate_room_topic_ai),
                    patch("mindroom.matrix.rooms.generate_room_topic_ai", mock_generate_room_topic_ai),
                ):
                    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

                    with patch("mindroom.bot.create_bot_for_entity") as mock_create_bot:
                        mock_bot = AsyncMock()
                        mock_bot.start = AsyncMock()
                        mock_bot.stop = AsyncMock()
                        mock_bot.sync_forever = AsyncMock()
                        mock_create_bot.return_value = mock_bot

                        await orchestrator.initialize()
                        orchestrator.running = True

                        # Add a new team
                        updated_config_data = initial_config_data.copy()
                        updated_config_data["teams"]["new_team"] = {
                            "display_name": "NewTeam",
                            "role": "New test team",
                            "agents": [],
                            "rooms": ["room1"],
                            "model": "default",
                            "mode": "coordinate",
                        }
                        config2 = Config.model_validate(updated_config_data)
                        mock_load_config.return_value = config2

                        # Mock ensure_users to include the new team
                        mock_team_user = MagicMock(user_id="@new_team:localhost", agent_name="new_team")
                        mock_ensure_users.return_value = {
                            "router": mock_router_user,
                            "new_team": mock_team_user,
                        }

                        # Update config
                        updated = await orchestrator.update_config()

                        # Verify the new team was created
                        assert updated is True
                        # The new team should be in the bots now
                        assert "new_team" in orchestrator.agent_bots

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for team configuration
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_no_change_no_restart(self, tmp_path: Path) -> None:
        """Test that no changes in team config doesn't trigger restart."""
        config_data: dict[str, Any] = {
            "agents": {},
            "teams": {
                "team1": {
                    "display_name": "Team1",
                    "role": "Test team",
                    "agents": [],
                    "rooms": ["room1"],
                    "model": "default",
                    "mode": "coordinate",
                },
            },
            "defaults": {"num_history_runs": 5, "markdown": True, "add_history_to_messages": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        with patch("mindroom.config.Config.from_yaml") as mock_load_config:
            config = Config.model_validate(config_data)
            mock_load_config.return_value = config

            with patch("mindroom.matrix.users.ensure_all_agent_users") as mock_ensure_users:
                mock_team_user = MagicMock(user_id="@team1:localhost", agent_name="team1")
                mock_router_user = MagicMock(user_id="@router:localhost", agent_name="router")
                mock_ensure_users.return_value = {"team1": mock_team_user, "router": mock_router_user}

                # Mock topic generation to avoid calling AI
                async def mock_generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str:  # noqa: ARG001
                    return f"Test topic for {room_name}"

                # Also need to patch it in the rooms module where it's imported
                with (
                    patch("mindroom.topic_generator.generate_room_topic_ai", mock_generate_room_topic_ai),
                    patch("mindroom.matrix.rooms.generate_room_topic_ai", mock_generate_room_topic_ai),
                ):
                    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

                    with patch("mindroom.bot.create_bot_for_entity") as mock_create_bot:
                        mock_bot = AsyncMock()
                        mock_bot.start = AsyncMock()
                        mock_bot.stop = AsyncMock()
                        mock_bot.sync_forever = AsyncMock()
                        mock_create_bot.return_value = mock_bot

                        await orchestrator.initialize()
                        orchestrator.running = True

                        # Reset mocks
                        mock_bot.stop.reset_mock()
                        mock_create_bot.reset_mock()

                        # Update with same config
                        updated = await orchestrator.update_config()

                        # Verify nothing was restarted
                        assert updated is False
                        assert not mock_bot.stop.called
                        assert mock_create_bot.call_count == 0
