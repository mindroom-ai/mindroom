"""Test that edit regeneration works correctly after bot restart."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_bot_handles_redelivered_edit_after_restart(tmp_path: Path) -> None:
    """Test that the bot correctly handles an edit event that gets redelivered after restart.

    Scenario:
    1. User sends message
    2. Bot responds
    3. User edits message
    4. Bot starts regenerating
    5. Bot crashes/restarts
    6. Matrix server redelivers the edit event
    7. Bot should regenerate (not skip as "already seen")
    """
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Simulate that the bot has already responded to the original message
    original_event_id = "$original:example.com"
    response_event_id = "$response:example.com"
    bot.response_tracker.mark_responded(original_event_id, response_event_id)

    # Also mark the edit event as "seen" (simulating it was delivered before restart)
    # With the correct implementation, edits should still be processed
    edit_event_id = "$edit:example.com"
    bot.response_tracker.mark_responded(edit_event_id)

    # Create an edit event that would be redelivered after restart
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent what is 3+3?",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": original_event_id,
                    "rel_type": "m.replace",
                },
            },
            "event_id": edit_event_id,
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent what is 3+3?",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": original_event_id,
                "rel_type": "m.replace",
            },
        },
        "event_id": edit_event_id,
        "sender": "@user:example.com",
    }

    # Mock the methods needed for regeneration
    with (
        patch.object(bot, "_handle_message_edit", new_callable=AsyncMock) as mock_handle_edit,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        # Process the redelivered edit event
        await bot._on_message(room, edit_event)

        # The bot SHOULD handle the edit (regenerate the response)
        # even though we've "seen" this edit event before
        mock_handle_edit.assert_called_once()


@pytest.mark.asyncio
async def test_bot_skips_duplicate_regular_message_after_restart(tmp_path: Path) -> None:
    """Test that the bot correctly skips regular messages that are redelivered after restart.

    This is the original purpose of the has_responded check - prevent duplicate responses.
    """
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Mark a message as already responded to
    message_event_id = "$message:example.com"
    bot.response_tracker.mark_responded(message_event_id)

    # Create a regular message event (not an edit)
    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@test_agent hello",
                "msgtype": "m.text",
            },
            "event_id": message_event_id,
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    message_event.source = {
        "content": {
            "body": "@test_agent hello",
            "msgtype": "m.text",
        },
        "event_id": message_event_id,
        "sender": "@user:example.com",
    }

    # Mock methods
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        # Process the redelivered message
        await bot._on_message(room, message_event)

        # The bot should NOT process this message again
        mock_context.assert_not_called()
        mock_generate.assert_not_called()
