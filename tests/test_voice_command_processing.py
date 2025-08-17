"""Test that voice transcriptions from router are processed for commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.constants import ROUTER_AGENT_NAME


@pytest.mark.asyncio
async def test_router_processes_own_voice_transcriptions() -> None:
    """Test that router processes voice transcriptions it sends on behalf of users."""
    # Create a mock router bot
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    storage_path = MagicMock()
    config = MagicMock()
    config.agents = {"calculator": MagicMock()}

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=storage_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.thread_invite_manager = MagicMock()
    bot.thread_invite_manager.get_agent_threads = AsyncMock(return_value=[])
    bot.thread_invite_manager.update_agent_activity = AsyncMock()
    bot.logger = MagicMock()

    # Create mock room and event
    room = MagicMock()
    room.room_id = "!test:example.com"

    # Create event that looks like voice transcription from router
    event = MagicMock()
    event.sender = "@mindroom_router:example.com"  # From router itself
    event.body = "🎤 !invite calculator"  # Voice transcription with command
    event.event_id = "test_event"
    event.source = {"content": {"body": "🎤 !invite calculator"}}

    # Mock the command handling and interactive handler
    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
    ):
        await bot._on_message(room, event)

    # The command should be handled even though it's from router
    mock_handle.assert_called_once()
    command = mock_handle.call_args[0][2]
    assert command.type.value == "invite"
    assert command.args["agent_name"] == "calculator"


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages() -> None:
    """Test that router still ignores its own non-voice messages."""
    # Create a mock router bot
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    storage_path = MagicMock()
    config = MagicMock()

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=storage_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.thread_invite_manager = MagicMock()
    bot.logger = MagicMock()

    # Create mock room and event
    room = MagicMock()
    room.room_id = "!test:example.com"

    # Create event that's a regular message from router (not voice)
    event = MagicMock()
    event.sender = "@mindroom_router:example.com"  # From router itself
    event.body = "Regular message from router"  # Not a voice transcription
    event.event_id = "test_event"
    event.source = {"content": {"body": "Regular message from router"}}

    # Mock the command handling and interactive handler
    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
    ):
        await bot._on_message(room, event)

    # Should not handle anything - router ignores its own regular messages
    mock_handle.assert_not_called()
