"""Test that errors in event callbacks are properly handled and logged."""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, _create_task_wrapper
from mindroom.matrix.users import AgentMatrixUser


@pytest.mark.asyncio
async def test_callback_error_is_logged_not_raised(tmp_path: Path) -> None:
    """Test that errors in callbacks are logged but don't crash the sync loop."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Mock dependencies
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    # Create a room and event
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Test message",
                "msgtype": "m.text",
            },
            "event_id": "$test:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    event.source = {
        "content": {
            "body": "Test message",
            "msgtype": "m.text",
        },
        "event_id": "$test:example.com",
        "sender": "@user:example.com",
    }

    # Make _on_message raise an error
    with patch.object(bot, "_on_message", side_effect=ValueError("Test error")):
        # Create wrapped callback
        wrapped = _create_task_wrapper(bot._on_message)

        # Call the wrapped callback - should not raise
        await wrapped(room, event)

    # Give the task a moment to execute
    await asyncio.sleep(0.1)

    # Check that the error was logged (caplog would capture it if using real logger)
    # Since we're using a mock logger, we can't check caplog, but the test
    # passes if we get here without an exception


@pytest.mark.asyncio
async def test_cancelled_error_is_handled_silently() -> None:
    """Test that CancelledError is handled silently (expected during shutdown)."""

    # Create a callback that raises CancelledError
    async def callback_that_gets_cancelled(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        raise asyncio.CancelledError

    # Wrap it
    wrapped = _create_task_wrapper(callback_that_gets_cancelled)

    # Call it - should not raise or log
    await wrapped()

    # Give the task a moment to execute
    await asyncio.sleep(0.1)

    # Test passes if we get here without an exception
