"""Tests for team room update functionality."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import orchestrator_runtime_paths


def _mock_running_bot(entity_name: str, agent_user: object, config: Config) -> MagicMock:
    """Return one distinct running bot double with its configured rooms."""
    bot = MagicMock()
    bot.agent_name = entity_name
    bot.agent_user = agent_user
    bot.config = config
    bot.rooms = get_rooms_for_entity(entity_name, config)
    bot.running = True
    bot.start = AsyncMock()
    bot.stop = AsyncMock()
    bot.sync_forever = AsyncMock()
    bot.try_start = AsyncMock(return_value=True)
    bot.prepare_for_sync_shutdown = AsyncMock()
    bot._set_presence_with_model_info = AsyncMock()
    bot.mark_sync_loop_started = MagicMock()
    bot.reset_watchdog_clock = MagicMock()
    return bot


class TestTeamRoomUpdates:
    """Test team room configuration updates."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_team_room_change_reconciles_live_bot(self, tmp_path: Path) -> None:
        """Changing a team's rooms should preserve bots and reconcile memberships live."""
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
            "defaults": {"markdown": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        with (
            patch("mindroom.orchestrator.load_config") as mock_load_config,
            patch("mindroom.orchestration.config_lifecycle.load_config", new=mock_load_config),
        ):
            config1 = Config.model_validate(initial_config_data)
            mock_load_config.return_value = config1

            with patch("mindroom.orchestrator.create_agent_user", new_callable=AsyncMock) as mock_ensure_users:
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
                    patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()),
                    patch("mindroom.orchestrator._MultiAgentOrchestrator._validate_entity_accounts"),
                    patch(
                        "mindroom.orchestrator._MultiAgentOrchestrator._setup_rooms_and_memberships",
                        new=AsyncMock(),
                    ) as setup_rooms,
                ):
                    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

                    with patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot:
                        created_bots: dict[str, MagicMock] = {}

                        def create_mock_bot(
                            entity_name: str,
                            agent_user: object,
                            config: Config,
                            *_args: object,
                            **_kwargs: object,
                        ) -> MagicMock:
                            bot = _mock_running_bot(entity_name, agent_user, config)
                            created_bots[entity_name] = bot
                            return bot

                        mock_create_bot.side_effect = create_mock_bot

                        try:
                            await orchestrator.initialize()
                            orchestrator.running = True
                            original_bots = dict(orchestrator.agent_bots)

                            async def reconcile_rooms(bots: list[MagicMock]) -> None:
                                active_config = orchestrator._require_config()
                                for bot in bots:
                                    bot.rooms = get_rooms_for_entity(bot.agent_name, active_config)

                            setup_rooms.side_effect = reconcile_rooms
                            setup_rooms.reset_mock()
                            for bot in created_bots.values():
                                bot.stop.reset_mock()

                            # Update config with different rooms for the team
                            updated_config_data = initial_config_data.copy()
                            updated_config_data["teams"]["team1"]["rooms"] = ["room2", "room3", "room4"]
                            config2 = Config.model_validate(updated_config_data)
                            mock_load_config.return_value = config2

                            # Update config
                            updated = await orchestrator.config_reload._update_config()

                            # Verify the existing team and router bots were reconciled in place.
                            assert updated is True
                            assert orchestrator.agent_bots == original_bots
                            assert orchestrator.agent_bots["agent1"].rooms == ["room1"]
                            assert orchestrator.agent_bots["team1"].rooms == ["room2", "room3", "room4"]
                            assert set(orchestrator.agent_bots["router"].rooms) == {
                                "room1",
                                "room2",
                                "room3",
                                "room4",
                            }
                            for bot in created_bots.values():
                                bot.stop.assert_not_awaited()
                            assert mock_create_bot.call_count == 3
                            setup_rooms.assert_awaited_once()
                            reconciled_bots = setup_rooms.await_args.args[0]
                            assert {bot.agent_name for bot in reconciled_bots} == {"router", "team1"}
                        finally:
                            await orchestrator.stop()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for team creation
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_new_team_gets_created(self, tmp_path: Path) -> None:
        """Test that a new team in config gets created."""
        # Start with no teams
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
            "teams": {},
            "defaults": {"markdown": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        with (
            patch("mindroom.orchestrator.load_config") as mock_load_config,
            patch("mindroom.orchestration.config_lifecycle.load_config", new=mock_load_config),
        ):
            config1 = Config.model_validate(initial_config_data)
            mock_load_config.return_value = config1

            with patch("mindroom.orchestrator.create_agent_user", new_callable=AsyncMock) as mock_ensure_users:
                mock_agent1_user = MagicMock(user_id="@agent1:localhost", agent_name="agent1")
                mock_router_user = MagicMock(user_id="@router:localhost", agent_name="router")
                mock_ensure_users.return_value = {"agent1": mock_agent1_user, "router": mock_router_user}

                # Mock topic generation to avoid calling AI
                async def mock_generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str:  # noqa: ARG001
                    return f"Test topic for {room_name}"

                # Also need to patch it in the rooms module where it's imported
                with (
                    patch("mindroom.topic_generator.generate_room_topic_ai", mock_generate_room_topic_ai),
                    patch("mindroom.matrix.rooms.generate_room_topic_ai", mock_generate_room_topic_ai),
                    patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()),
                    patch(
                        "mindroom.orchestrator._MultiAgentOrchestrator._setup_rooms_and_memberships",
                        new=AsyncMock(),
                    ),
                ):
                    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

                    with patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot:
                        mock_bot = MagicMock()
                        mock_bot.start = AsyncMock()
                        mock_bot.stop = AsyncMock()
                        mock_bot.sync_forever = AsyncMock()
                        mock_bot.try_start = AsyncMock(return_value=True)
                        mock_bot.prepare_for_sync_shutdown = AsyncMock()
                        mock_bot._set_presence_with_model_info = AsyncMock()
                        mock_bot.mark_sync_loop_started = MagicMock()
                        mock_bot.reset_watchdog_clock = MagicMock()
                        mock_create_bot.return_value = mock_bot

                        try:
                            await orchestrator.initialize()
                            orchestrator.running = True

                            # Add a new team
                            updated_config_data = initial_config_data.copy()
                            updated_config_data["teams"]["new_team"] = {
                                "display_name": "NewTeam",
                                "role": "New test team",
                                "agents": ["agent1"],
                                "rooms": ["room1"],
                                "model": "default",
                                "mode": "coordinate",
                            }
                            config2 = Config.model_validate(updated_config_data)
                            mock_load_config.return_value = config2

                            # Mock ensure_users to include the new team
                            mock_team_user = MagicMock(user_id="@new_team:localhost", agent_name="new_team")
                            mock_ensure_users.return_value = {
                                "agent1": mock_agent1_user,
                                "router": mock_router_user,
                                "new_team": mock_team_user,
                            }

                            # Update config
                            updated = await orchestrator.config_reload._update_config()

                            # Verify the new team was created
                            assert updated is True
                            # The new team should be in the bots now
                            assert "new_team" in orchestrator.agent_bots
                        finally:
                            await orchestrator.stop()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for team configuration
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_no_change_no_restart(self, tmp_path: Path) -> None:
        """Test that no changes in team config doesn't trigger restart."""
        config_data: dict[str, Any] = {
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
                    "rooms": ["room1"],
                    "model": "default",
                    "mode": "coordinate",
                },
            },
            "defaults": {"markdown": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        with (
            patch("mindroom.orchestrator.load_config") as mock_load_config,
            patch("mindroom.orchestration.config_lifecycle.load_config", new=mock_load_config),
        ):
            config = Config.model_validate(config_data)
            mock_load_config.return_value = config

            with patch("mindroom.orchestrator.create_agent_user", new_callable=AsyncMock) as mock_ensure_users:
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
                    patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()),
                    patch(
                        "mindroom.orchestrator._MultiAgentOrchestrator._setup_rooms_and_memberships",
                        new=AsyncMock(),
                    ),
                ):
                    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

                    with patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot:
                        mock_bot = MagicMock()
                        mock_bot.start = AsyncMock()
                        mock_bot.stop = AsyncMock()
                        mock_bot.sync_forever = AsyncMock()
                        mock_bot.try_start = AsyncMock(return_value=True)
                        mock_bot.prepare_for_sync_shutdown = AsyncMock()
                        mock_bot._set_presence_with_model_info = AsyncMock()
                        mock_bot.mark_sync_loop_started = MagicMock()
                        mock_bot.reset_watchdog_clock = MagicMock()
                        mock_create_bot.return_value = mock_bot

                        try:
                            await orchestrator.initialize()
                            orchestrator.running = True

                            # Reset mocks
                            mock_bot.stop.reset_mock()
                            mock_create_bot.reset_mock()

                            # Update with same config
                            updated = await orchestrator.config_reload._update_config()

                            # Verify nothing was restarted
                            assert updated is False
                            assert not mock_bot.stop.called
                            assert mock_create_bot.call_count == 0
                        finally:
                            await orchestrator.stop()
