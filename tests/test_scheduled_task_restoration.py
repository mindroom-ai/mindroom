"""Test that scheduled task restoration only happens once after restart."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestScheduledTaskRestoration:
    """Test scheduled task restoration behavior after bot restart."""

    @staticmethod
    def _bind_runtime(config: Config, tmp_path: Path) -> Config:
        return bind_runtime_paths(
            config,
            orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
        )

    @staticmethod
    def _install_runtime_support(bot: AgentBot) -> AgentBot:
        return install_runtime_cache_support(bot)

    @pytest.mark.asyncio
    async def test_only_router_restores_tasks(self, tmp_path: Path) -> None:
        """Test that only the router agent restores scheduled tasks."""
        # Create a mock config with multiple agents
        config = self._bind_runtime(
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
        router_bot.client = make_matrix_client_mock(user_id=router_user.user_id)
        router_bot.client.rooms = {}
        self._install_runtime_support(router_bot)

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
            mock_restore.assert_awaited_once_with(
                router_bot.client,
                "lobby",
                config,
                runtime_paths_for(config),
                router_bot.event_cache,
                router_bot._conversation_cache,
            )

    @pytest.mark.asyncio
    async def test_non_router_agents_dont_restore_tasks(self, tmp_path: Path) -> None:
        """Test that non-router agents don't restore scheduled tasks."""
        config = self._bind_runtime(
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
        regular_bot.client = make_matrix_client_mock(user_id=regular_user.user_id)
        regular_bot.client.rooms = {}
        self._install_runtime_support(regular_bot)

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
        config = self._bind_runtime(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

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
        router_bot.client = make_matrix_client_mock(user_id=router_user.user_id)
        router_bot.client.rooms = {"lobby": object()}
        self._install_runtime_support(router_bot)

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
        mock_restore.assert_awaited_once_with(
            router_bot.client,
            "lobby",
            config,
            runtime_paths_for(config),
            router_bot.event_cache,
            router_bot._conversation_cache,
        )
        mock_restore_configs.assert_awaited_once_with(router_bot.client, "lobby")
        mock_welcome.assert_awaited_once_with("lobby")

    @pytest.mark.asyncio
    async def test_router_drains_deferred_overdue_tasks_on_first_sync_response(self, tmp_path: Path) -> None:
        """The router should start deferred overdue tasks after the first successful sync."""
        config = self._bind_runtime(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

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
        router_bot.client = make_matrix_client_mock(user_id=router_user.user_id)
        self._install_runtime_support(router_bot)

        with (
            patch(
                "mindroom.bot.drain_deferred_overdue_tasks",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_drain,
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        ):
            await router_bot._on_sync_response(MagicMock())

            assert router_bot._deferred_overdue_task_drain_task is not None
            await router_bot._deferred_overdue_task_drain_task

            mock_drain.assert_awaited_once_with(
                router_bot.client,
                config,
                runtime_paths_for(config),
                router_bot.event_cache,
                router_bot._conversation_cache,
            )

            await router_bot._on_sync_response(MagicMock())
            mock_drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_restarts_deferred_drain_when_queue_remains_after_sync_restart(
        self,
        tmp_path: Path,
    ) -> None:
        """A later sync response should restart draining if queued overdue work remains."""
        config = self._bind_runtime(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

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
        router_bot.client = make_matrix_client_mock(user_id=router_user.user_id)
        self._install_runtime_support(router_bot)

        with (
            patch(
                "mindroom.bot.drain_deferred_overdue_tasks",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_drain,
            patch("mindroom.bot.has_deferred_overdue_tasks", return_value=True),
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        ):
            await router_bot._on_sync_response(MagicMock())
            assert router_bot._deferred_overdue_task_drain_task is not None
            await router_bot._deferred_overdue_task_drain_task

            await router_bot._on_sync_response(MagicMock())
            assert router_bot._deferred_overdue_task_drain_task is not None
            await router_bot._deferred_overdue_task_drain_task

            assert mock_drain.await_count == 2

    @pytest.mark.asyncio
    async def test_router_does_not_restart_deferred_drain_after_sync_shutdown_preparation(
        self,
        tmp_path: Path,
    ) -> None:
        """Late sync responses during teardown must not respawn the deferred drain."""
        config = self._bind_runtime(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

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
        router_bot.client = make_matrix_client_mock(user_id=router_user.user_id)
        self._install_runtime_support(router_bot)

        with (
            patch(
                "mindroom.bot.drain_deferred_overdue_tasks",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_drain,
            patch("mindroom.bot.has_deferred_overdue_tasks", return_value=True),
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        ):
            await router_bot.prepare_for_sync_shutdown()
            await router_bot._on_sync_response(MagicMock())

            assert router_bot._deferred_overdue_task_drain_task is None
            mock_drain.assert_not_awaited()

            router_bot.mark_sync_loop_started()
            await router_bot._on_sync_response(MagicMock())

            assert router_bot._deferred_overdue_task_drain_task is not None
            await router_bot._deferred_overdue_task_drain_task
            mock_drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_stop_cancels_running_scheduled_tasks(self, tmp_path: Path) -> None:
        """Stopping the router should clear in-memory scheduled tasks before restart."""
        config = self._bind_runtime(Config(models={"default": {"provider": "test", "id": "test-model"}}), tmp_path)

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
        router_bot.client = make_matrix_client_mock(user_id=router_user.user_id)
        router_bot.client.rooms = {}
        self._install_runtime_support(router_bot)
        drain_task = asyncio.create_task(asyncio.sleep(60))
        router_bot._deferred_overdue_task_drain_task = drain_task

        async def wait_for_background_tasks_side_effect(**kwargs: float) -> None:
            assert "timeout" in kwargs
            assert drain_task.cancelled()

        with (
            patch(
                "mindroom.bot.wait_for_background_tasks",
                new_callable=AsyncMock,
                side_effect=wait_for_background_tasks_side_effect,
            ),
            patch("mindroom.bot.clear_deferred_overdue_tasks", return_value=1) as mock_clear,
            patch(
                "mindroom.bot.cancel_all_running_scheduled_tasks",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_cancel,
        ):
            await router_bot.stop()

        assert drain_task.cancelled()
        mock_clear.assert_called_once_with()
        mock_cancel.assert_awaited_once()
        router_bot.client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multiple_agents_only_router_restores(self, tmp_path: Path) -> None:
        """Test that when multiple agents join a room, only router restores tasks."""
        config = self._bind_runtime(
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
            bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
            bot.client.rooms = {}
            self._install_runtime_support(bot)

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
