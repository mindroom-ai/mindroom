"""Test that scheduled task restoration only happens once after restart."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser


class TestScheduledTaskRestoration:
    """Test scheduled task restoration behavior after bot restart."""

    @pytest.mark.asyncio
    async def test_only_router_restores_tasks(self) -> None:
        """Test that only the router agent restores scheduled tasks."""
        # Create a mock config with multiple agents
        config = Config(
            agents={  # type: ignore[arg-type]
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "email_assistant": {
                    "display_name": "EmailAssistant",
                    "role": "Email assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
        )

        # Test with RouterAgent
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=f"@{ROUTER_AGENT_NAME}:mindroom.com",
            password="test",  # noqa: S106
            display_name="RouterAgent",
        )
        router_bot = AgentBot(
            agent_user=router_user,
            storage_path=MagicMock(),
            config=config,
            rooms=["lobby"],
        )

        # Mock the client and join_room
        router_bot.client = AsyncMock(spec=nio.AsyncClient)

        with (
            patch("mindroom.bot.join_room", return_value=True) as mock_join,
            patch("mindroom.bot.restore_scheduled_tasks", return_value=2) as mock_restore,
        ):
            await router_bot.join_configured_rooms()

            # Verify router agent called restore_scheduled_tasks
            mock_join.assert_called_once()
            mock_restore.assert_called_once_with(router_bot.client, "lobby", config)

    @pytest.mark.asyncio
    async def test_non_router_agents_dont_restore_tasks(self) -> None:
        """Test that non-router agents don't restore scheduled tasks."""
        config = Config(
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

        # Test with regular agent (not router)
        regular_user = AgentMatrixUser(
            agent_name="general",
            user_id="@general:mindroom.com",
            password="test",  # noqa: S106
            display_name="GeneralAgent",
        )
        regular_bot = AgentBot(
            agent_user=regular_user,
            storage_path=MagicMock(),
            config=config,
            rooms=["lobby"],
        )

        # Mock the client and join_room
        regular_bot.client = AsyncMock(spec=nio.AsyncClient)

        with (
            patch("mindroom.bot.join_room", return_value=True) as mock_join,
            patch("mindroom.bot.restore_scheduled_tasks", return_value=2) as mock_restore,
        ):
            await regular_bot.join_configured_rooms()

            # Verify regular agent did NOT call restore_scheduled_tasks
            mock_join.assert_called_once()
            mock_restore.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_agents_only_router_restores(self) -> None:
        """Test that when multiple agents join a room, only router restores tasks."""
        config = Config(
            agents={  # type: ignore[arg-type]
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "email_assistant": {
                    "display_name": "EmailAssistant",
                    "role": "Email assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
        )

        agents_to_test = [
            ("general", "GeneralAgent", False),
            ("email_assistant", "EmailAssistant", False),
            (ROUTER_AGENT_NAME, "RouterAgent", True),  # Only router should restore
        ]

        restore_call_count = 0

        for agent_name, display_name, should_restore in agents_to_test:
            user = AgentMatrixUser(
                agent_name=agent_name,
                user_id=f"@{agent_name}:mindroom.com",
                password="test",  # noqa: S106
                display_name=display_name,
            )
            bot = AgentBot(
                agent_user=user,
                storage_path=MagicMock(),
                config=config,
                rooms=["lobby"],
            )
            bot.client = AsyncMock(spec=nio.AsyncClient)

            with (
                patch("mindroom.bot.join_room", return_value=True),
                patch("mindroom.bot.restore_scheduled_tasks", return_value=2) as mock_restore,
            ):
                await bot.join_configured_rooms()

                if should_restore:
                    mock_restore.assert_called_once()
                    restore_call_count += 1
                else:
                    mock_restore.assert_not_called()

        # Verify only one agent (router) called restore
        assert restore_call_count == 1
