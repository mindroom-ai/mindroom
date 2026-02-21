"""Test that voice transcriptions from router are processed for commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.constants import ROUTER_AGENT_NAME, VOICE_ORIGINAL_SENDER_KEY


@pytest.mark.asyncio
async def test_router_processes_own_voice_transcriptions(tmp_path) -> None:  # noqa: ANN001
    """Test that router processes voice transcriptions it sends on behalf of users."""
    # Create a mock router bot
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = MagicMock()
    config.agents = {"calculator": MagicMock()}

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    # Create mock room and event
    room = MagicMock()
    room.room_id = "!test:example.com"

    # Create event that looks like voice transcription from router
    event = MagicMock()
    event.sender = "@mindroom_router:example.com"  # From router itself
    event.body = "🎤 !schedule daily"  # Voice transcription with command
    event.event_id = "test_event"
    event.source = {"content": {"body": "🎤 !schedule daily"}}

    # Mock the command handling and interactive handler
    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
        patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
    ):
        await bot._on_message(room, event)

    # The command should be handled even though it's from router
    mock_handle.assert_called_once()
    command = mock_handle.call_args[0][2]
    assert command.type.value == "schedule"


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages(tmp_path) -> None:  # noqa: ANN001
    """Test that router still ignores its own non-voice messages."""
    # Create a mock router bot
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = MagicMock()

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
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
        patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
    ):
        await bot._on_message(room, event)

    # Should not handle anything - router ignores its own regular messages
    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_router_voice_transcription_includes_original_sender_metadata(tmp_path) -> None:  # noqa: ANN001
    """Router should embed the original sender so downstream permissions use user identity."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = MagicMock()
    config.voice.enabled = True

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(False, "$thread", []))

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = "@alice:example.com"
    event.event_id = "$voice_event"
    event.source = {"content": {"body": "voice.ogg"}}

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_voice.return_value = "🎤 turn on the lights"
        await bot._on_voice_message(room, event)

    bot._send_response.assert_called_once()
    assert bot._send_response.call_args.kwargs["extra_content"] == {VOICE_ORIGINAL_SENDER_KEY: "@alice:example.com"}
