"""Every bot hands the rooms it joins to the scheduled-task runtime, which restores the schedules it owns."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.users import AgentMatrixUser
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot


def _bind_runtime(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"))


def _bot_with_scheduled_task_runtime(
    agent_name: str,
    config: Config,
    joined_rooms: dict[str, object],
) -> tuple[AgentBot, AsyncMock]:
    """Return a bot whose orchestrator's scheduled-task runtime records each restored room."""
    user = AgentMatrixUser(
        agent_name=agent_name,
        user_id=f"@{agent_name}:mindroom.com",
        password="test",  # noqa: S106
        display_name=agent_name,
    )
    bot = make_test_agent_bot(
        agent_user=user,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["lobby"],
    )
    bot.client = make_matrix_client_mock(user_id=user.user_id)
    bot.client.rooms = joined_rooms
    install_runtime_journal_support(bot)
    restore_room = AsyncMock(return_value=2)
    bot.orchestrator.scheduled_tasks.restore_room = restore_room
    return bot, restore_room


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [ROUTER_AGENT_NAME, "general"])
async def test_configured_join_restores_schedules_for_every_bot(tmp_path: Path, agent_name: str) -> None:
    """Router and agents restore the schedules they own; only the router restores config changes and welcomes."""
    config = _bind_runtime(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "role": "General assistant", "rooms": ["lobby"]}},
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        tmp_path,
    )
    bot, restore_room = _bot_with_scheduled_task_runtime(agent_name, config, {})

    with (
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", new_callable=AsyncMock, return_value=[]),
        patch(
            "mindroom.matrix.client_room_admin.join_room",
            new_callable=AsyncMock,
            return_value=RoomJoinOutcome.JOINED,
        ) as mock_join,
        patch(
            "mindroom.bot.config_confirmation.restore_pending_changes",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_restore_configs,
        patch(
            "mindroom.bot_room_lifecycle.BotRoomLifecycle.send_welcome_message_if_empty",
            new_callable=AsyncMock,
        ) as mock_welcome,
    ):
        await bot.join_configured_rooms()

    mock_join.assert_awaited_once_with(bot.client, "lobby")
    restore_room.assert_awaited_once_with(bot, "lobby")
    is_router = agent_name == ROUTER_AGENT_NAME
    assert mock_restore_configs.await_count == int(is_router)
    assert mock_welcome.await_count == int(is_router)


@pytest.mark.asyncio
async def test_router_restores_tasks_without_rejoining_existing_room(tmp_path: Path) -> None:
    """Restart setup runs even when the room is already joined."""
    config = _bind_runtime(Config(models={"default": {"provider": "openai", "id": "test-model"}}), tmp_path)
    bot, restore_room = _bot_with_scheduled_task_runtime(ROUTER_AGENT_NAME, config, {"lobby": object()})

    with (
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", new_callable=AsyncMock, return_value=["lobby"]),
        patch("mindroom.matrix.client_room_admin.join_room", new_callable=AsyncMock) as mock_join,
        patch(
            "mindroom.bot.config_confirmation.restore_pending_changes",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_restore_configs,
        patch(
            "mindroom.bot_room_lifecycle.BotRoomLifecycle.send_welcome_message_if_empty",
            new_callable=AsyncMock,
        ) as mock_welcome,
    ):
        await bot.join_configured_rooms()

    mock_join.assert_not_awaited()
    restore_room.assert_awaited_once_with(bot, "lobby")
    mock_restore_configs.assert_awaited_once_with(bot.client, "lobby")
    mock_welcome.assert_awaited_once_with("lobby")
