"""Test dynamic config updates for scheduling with new agents."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.config import Config
from mindroom.scheduling import CronSchedule, ScheduledWorkflow, parse_workflow_schedule


class TestDynamicConfigUpdate:
    """Test that dynamic config updates propagate to all existing bots."""

    @pytest.mark.asyncio
    async def test_config_update_propagates_to_existing_bots(self) -> None:
        """Test that when config is updated, all existing bots get the new config."""
        # Create initial config with just one agent
        initial_config = Config(
            agents={  # type: ignore[arg-type]
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
        )

        # Create orchestrator and set initial config
        orchestrator = MultiAgentOrchestrator(storage_path=MagicMock())
        orchestrator.config = initial_config

        # Create a mock bot for the general agent
        mock_bot = MagicMock(spec=AgentBot)
        mock_bot.config = initial_config
        mock_bot.running = True
        orchestrator.agent_bots["general"] = mock_bot

        # Create updated config with a new agent
        updated_config = Config(
            agents={  # type: ignore[arg-type]
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
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
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
            agents={  # type: ignore[arg-type]
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
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
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
