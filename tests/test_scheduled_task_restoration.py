"""Test that scheduled task restoration only happens once after restart."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path


class TestScheduledTaskRestoration:
    """Test scheduled task restoration behavior after bot restart."""

    @pytest.mark.asyncio
    async def test_only_router_restores_tasks(self, tmp_path: Path) -> None:
        """Test that only the router agent restores scheduled tasks."""
        # Create a mock config with multiple agents
        config = bind_runtime_paths(
            Config(
                agents={
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
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
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
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["lobby"],
        )

        # Mock the client and join_room
        router_bot.client = AsyncMock(spec=nio.AsyncClient)
        router_bot.client.rooms = {}

        with (
            patch("mindroom.bot.get_joined_rooms", new_callable=AsyncMock, return_value=[]),
            patch("mindroom.bot.join_room", new_callable=AsyncMock, return_value=True) as mock_join,
            patch("mindroom.bot.restore_scheduled_tasks", new_callable=AsyncMock, return_value=2) as mock_restore,
            patch(
                "mindroom.bot.config_confirmation.restore_pending_changes",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch("mindroom.bot.AgentBot._send_welcome_message_if_empty", new_callable=AsyncMock),
        ):
            await router_bot.join_configured_rooms()

            # Verify router agent called restore_scheduled_tasks
            mock_join.assert_awaited_once_with(router_bot.client, "lobby")
            mock_restore.assert_awaited_once_with(router_bot.client, "lobby", config)

    @pytest.mark.asyncio
    async def test_non_router_agents_dont_restore_tasks(self, tmp_path: Path) -> None:
        """Test that non-router agents don't restore scheduled tasks."""
        config = bind_runtime_paths(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
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
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["lobby"],
        )

        # Mock the client and join_room
        regular_bot.client = AsyncMock(spec=nio.AsyncClient)
        regular_bot.client.rooms = {}

        with (
            patch("mindroom.bot.get_joined_rooms", new_callable=AsyncMock, return_value=[]),
            patch("mindroom.bot.join_room", new_callable=AsyncMock, return_value=True) as mock_join,
            patch("mindroom.bot.restore_scheduled_tasks", new_callable=AsyncMock, return_value=2) as mock_restore,
        ):
            await regular_bot.join_configured_rooms()

            # Verify regular agent did NOT call restore_scheduled_tasks
            mock_join.assert_awaited_once_with(regular_bot.client, "lobby")
            mock_restore.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_restores_tasks_without_rejoining_existing_room(self, tmp_path: Path) -> None:
        """Router restart setup should run even when the room is already joined."""
        config = bind_runtime_paths(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=f"@{ROUTER_AGENT_NAME}:mindroom.com",
            password="test",  # noqa: S106
            display_name="RouterAgent",
        )
        router_bot = AgentBot(
            agent_user=router_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["lobby"],
        )
        router_bot.client = AsyncMock(spec=nio.AsyncClient)
        router_bot.client.rooms = {"lobby": object()}

        with (
            patch("mindroom.bot.get_joined_rooms", new_callable=AsyncMock, return_value=["lobby"]),
            patch("mindroom.bot.join_room", new_callable=AsyncMock) as mock_join,
            patch("mindroom.bot.restore_scheduled_tasks", new_callable=AsyncMock, return_value=2) as mock_restore,
            patch(
                "mindroom.bot.config_confirmation.restore_pending_changes",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_restore_configs,
            patch(
                "mindroom.bot.AgentBot._send_welcome_message_if_empty",
                new_callable=AsyncMock,
            ) as mock_welcome,
        ):
            await router_bot.join_configured_rooms()

        mock_join.assert_not_awaited()
        mock_restore.assert_awaited_once_with(router_bot.client, "lobby", config)
        mock_restore_configs.assert_awaited_once_with(router_bot.client, "lobby")
        mock_welcome.assert_awaited_once_with("lobby")

    @pytest.mark.asyncio
    async def test_router_stop_cancels_running_scheduled_tasks(self, tmp_path: Path) -> None:
        """Stopping the router should clear in-memory scheduled tasks before restart."""
        config = bind_runtime_paths(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=f"@{ROUTER_AGENT_NAME}:mindroom.com",
            password="test",  # noqa: S106
            display_name="RouterAgent",
        )
        router_bot = AgentBot(
            agent_user=router_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["lobby"],
        )
        router_bot.client = AsyncMock(spec=nio.AsyncClient)
        router_bot.client.rooms = {}

        with (
            patch("mindroom.bot.wait_for_background_tasks", new_callable=AsyncMock),
            patch(
                "mindroom.bot.cancel_all_running_scheduled_tasks",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_cancel,
        ):
            await router_bot.stop()

        mock_cancel.assert_awaited_once()
        router_bot.client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multiple_agents_only_router_restores(self, tmp_path: Path) -> None:
        """Test that when multiple agents join a room, only router restores tasks."""
        config = bind_runtime_paths(
            Config(
                agents={
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
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
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
                storage_path=tmp_path / agent_name,
                config=config,
                runtime_paths=runtime_paths_for(config),
                rooms=["lobby"],
            )
            bot.client = AsyncMock(spec=nio.AsyncClient)
            bot.client.rooms = {}

            with (
                patch("mindroom.bot.get_joined_rooms", new_callable=AsyncMock, return_value=[]),
                patch("mindroom.bot.join_room", new_callable=AsyncMock, return_value=True),
                patch("mindroom.bot.restore_scheduled_tasks", new_callable=AsyncMock, return_value=2) as mock_restore,
                patch(
                    "mindroom.bot.config_confirmation.restore_pending_changes",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch("mindroom.bot.AgentBot._send_welcome_message_if_empty", new_callable=AsyncMock),
            ):
                await bot.join_configured_rooms()

                if should_restore:
                    mock_restore.assert_called_once()
                    restore_call_count += 1
                else:
                    mock_restore.assert_not_called()

        # Verify only one agent (router) called restore
        assert restore_call_count == 1
