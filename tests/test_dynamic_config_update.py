"""Test dynamic config updates for scheduling with new agents."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.config import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.scheduling import CronSchedule, ScheduledWorkflow, parse_workflow_schedule

if TYPE_CHECKING:
    from pathlib import Path


class TestDynamicConfigUpdate:
    """Test that dynamic config updates propagate to all existing bots."""

    @pytest.mark.asyncio
    async def test_config_update_propagates_to_existing_bots(self, tmp_path: Path) -> None:
        """Test that when config is updated, all existing bots get the new config."""
        # Create initial config with just one agent
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        )

        # Create orchestrator and set initial config
        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        orchestrator.config = initial_config

        # Create a mock bot for the general agent
        mock_bot = MagicMock(spec=AgentBot)
        mock_bot.config = initial_config
        mock_bot.running = True
        orchestrator.agent_bots["general"] = mock_bot

        # Create updated config with a new agent
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "callagent": {
                    "display_name": "CallAgent",
                    "role": "Call assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        )

        # Mock the from_yaml method to return our updated config
        with patch.object(Config, "from_yaml", return_value=updated_config):  # noqa: SIM117
            # Mock the bot creation and setup methods to avoid actual Matrix operations
            with (
                patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
                patch("mindroom.bot._identify_entities_to_restart") as mock_identify,
                patch.object(orchestrator, "_setup_rooms_and_memberships"),
            ):
                mock_identify.return_value = set()  # No entities need restarting

                # Create a mock for the new bot
                new_bot_mock = MagicMock(spec=AgentBot)
                new_bot_mock.config = updated_config
                new_bot_mock.start.return_value = None
                new_bot_mock.sync_forever.return_value = None
                mock_create_bot.return_value = new_bot_mock

                # Call update_config
                updated = await orchestrator.update_config()

                # Verify the update happened
                assert updated is True
                assert orchestrator.config == updated_config

                # Most importantly: verify that the existing bot got the new config
                assert mock_bot.config == updated_config

                # Verify that the new agent was added
                assert "callagent" in orchestrator.agent_bots
                assert orchestrator.agent_bots["callagent"].config == updated_config

    @pytest.mark.asyncio
    async def test_scheduling_with_dynamically_added_agent(self) -> None:
        """Test that scheduling commands work correctly with dynamically added agents."""
        # Update config to add callagent
        updated_config = Config(
            agents={
                "email_assistant": {
                    "display_name": "EmailAssistant",
                    "role": "Email assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "callagent": {
                    "display_name": "CallAgent",
                    "role": "Call assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        )

        # Test that parse_workflow_schedule correctly recognizes the new agent
        request = "whenever i get an email with title urgent, notify @callagent to send me a text"

        # Mock the AI model to return a proper workflow
        with patch("mindroom.scheduling.get_model_instance") as mock_get_model:
            mock_agent = MagicMock()
            mock_response = MagicMock()

            # Create a mock workflow that references both agents
            mock_workflow = ScheduledWorkflow(
                schedule_type="cron",
                cron_schedule=CronSchedule(minute="*/2", hour="*", day="*", month="*", weekday="*"),
                message="@email_assistant Check for emails with 'urgent' in the title. If found, @callagent notify the user by sending a text.",
                description="Monitor for urgent emails and send text notification",
            )
            mock_response.content = mock_workflow

            # Make the arun method async
            async def async_arun(*args, **kwargs) -> MagicMock:  # noqa: ARG001, ANN002, ANN003
                return mock_response

            mock_agent.arun = async_arun

            # Create a mock model that returns our mock agent
            mock_model = MagicMock()
            mock_get_model.return_value = mock_model

            with patch("mindroom.scheduling.Agent") as mock_agent_class:
                mock_agent_class.return_value = mock_agent

                # Parse with the updated config
                result = await parse_workflow_schedule(
                    request,
                    updated_config,
                    available_agents=["email_assistant", "callagent"],  # Both agents available
                )

                # Verify the workflow was parsed correctly and includes both agents
                assert hasattr(result, "message")
                assert "@email_assistant" in result.message
                assert "@callagent" in result.message
                assert result.description == "Monitor for urgent emails and send text notification"

    @pytest.mark.asyncio
    async def test_defaults_streaming_toggle_updates_existing_bots_without_restart(self, tmp_path: Path) -> None:
        """Changing defaults.enable_streaming should update existing bots on config reload."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
            defaults={"enable_streaming": True},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
            defaults={"enable_streaming": False},
        )

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        orchestrator.config = initial_config

        mock_bot = MagicMock(spec=AgentBot)
        mock_bot.config = initial_config
        mock_bot.enable_streaming = True
        orchestrator.agent_bots["general"] = mock_bot
        router_bot = MagicMock(spec=AgentBot)
        router_bot.config = initial_config
        router_bot.enable_streaming = True
        orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

        with (
            patch.object(Config, "from_yaml", return_value=updated_config),
            patch("mindroom.bot._identify_entities_to_restart", return_value=set()),
        ):
            updated = await orchestrator.update_config()

        # No entities restarted, but existing bots still receive new defaults.
        assert updated is False
        assert mock_bot.config == updated_config
        assert mock_bot.enable_streaming is False
        assert router_bot.config == updated_config
        assert router_bot.enable_streaming is False

    @pytest.mark.asyncio
    async def test_matrix_room_access_change_reconciles_rooms_without_restarts(self, tmp_path: Path) -> None:
        """Changing matrix_room_access should trigger room/invitation reconciliation on config reload."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
            matrix_room_access={"mode": "single_user_private"},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
            matrix_room_access={
                "mode": "multi_user",
                "reconcile_existing_rooms": True,
            },
        )

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        orchestrator.config = initial_config

        general_bot = MagicMock(spec=AgentBot)
        general_bot.config = initial_config
        general_bot.enable_streaming = True
        orchestrator.agent_bots["general"] = general_bot
        router_bot = MagicMock(spec=AgentBot)
        router_bot.config = initial_config
        router_bot.enable_streaming = True
        orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

        with (
            patch.object(Config, "from_yaml", return_value=updated_config),
            patch("mindroom.bot._identify_entities_to_restart", return_value=set()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()) as mock_setup,
        ):
            updated = await orchestrator.update_config()

        assert updated is True
        assert general_bot.config == updated_config
        assert router_bot.config == updated_config
        mock_setup.assert_awaited_once_with([])

    @pytest.mark.asyncio
    async def test_mindroom_user_display_name_change_updates_user_account(self, tmp_path: Path) -> None:
        """Changing mindroom_user.display_name should refresh the internal user account."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
            mindroom_user={"username": "mindroom_user", "display_name": "Alice Internal"},
        )

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        orchestrator.config = initial_config
        mock_bot = MagicMock(spec=AgentBot)
        mock_bot.config = initial_config
        mock_bot.enable_streaming = True
        mock_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots["general"] = mock_bot
        router_bot = MagicMock(spec=AgentBot)
        router_bot.config = initial_config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

        with (
            patch.object(Config, "from_yaml", return_value=updated_config),
            patch("mindroom.bot._identify_entities_to_restart", return_value=set()),
            patch.object(orchestrator, "_ensure_user_account", new=AsyncMock()) as mock_ensure_user,
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()) as mock_setup,
        ):
            updated = await orchestrator.update_config()

        assert updated is True
        assert orchestrator.config == updated_config
        assert router_bot.config == updated_config
        mock_ensure_user.assert_awaited_once_with(updated_config)
        mock_setup.assert_awaited_once_with([])

    @pytest.mark.asyncio
    async def test_mindroom_user_username_change_is_rejected_without_partial_update(self, tmp_path: Path) -> None:
        """Reject changing mindroom_user.username and keep the current runtime config."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
            mindroom_user={"username": "alice_internal", "display_name": "Alice Internal"},
        )

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        orchestrator.config = initial_config
        mock_bot = MagicMock(spec=AgentBot)
        mock_bot.config = initial_config
        mock_bot.enable_streaming = True
        mock_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots["general"] = mock_bot
        router_bot = MagicMock(spec=AgentBot)
        router_bot.config = initial_config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

        with (
            patch.object(Config, "from_yaml", return_value=updated_config),
            patch("mindroom.bot._identify_entities_to_restart", return_value=set()),
            patch.object(
                orchestrator,
                "_ensure_user_account",
                new=AsyncMock(side_effect=ValueError("mindroom_user.username cannot be changed")),
            ) as mock_ensure_user,
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()) as mock_setup,
            pytest.raises(ValueError, match="cannot be changed"),
        ):
            await orchestrator.update_config()

        assert orchestrator.config == initial_config
        assert mock_bot.config == initial_config
        assert router_bot.config == initial_config
        mock_ensure_user.assert_awaited_once_with(updated_config)
        mock_setup.assert_not_awaited()
