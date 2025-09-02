"""Test that skip_mentions metadata prevents agents from responding to mentions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, _should_skip_mentions


def test_should_skip_mentions_with_metadata() -> None:
    """Test that _should_skip_mentions detects the metadata."""
    # Event with skip_mentions metadata
    event_source = {
        "content": {
            "body": "✅ Scheduled task. @email_agent will be mentioned",
            "com.mindroom.skip_mentions": True,
        },
    }
    assert _should_skip_mentions(event_source) is True


def test_should_skip_mentions_without_metadata() -> None:
    """Test that _should_skip_mentions returns False when no metadata."""
    # Normal event without metadata
    event_source = {
        "content": {
            "body": "Regular message @email_agent",
        },
    }
    assert _should_skip_mentions(event_source) is False


def test_should_skip_mentions_explicit_false() -> None:
    """Test that _should_skip_mentions returns False when metadata is False."""
    event_source = {
        "content": {
            "body": "Message with explicit false @email_agent",
            "com.mindroom.skip_mentions": False,
        },
    }
    assert _should_skip_mentions(event_source) is False


@pytest.mark.asyncio
async def test_send_response_with_skip_mentions() -> None:
    """Test that _send_response adds metadata when skip_mentions is True."""
    # Create a mock bot
    bot = AsyncMock(spec=AgentBot)
    bot.config = AsyncMock()
    bot.matrix_id = AsyncMock()
    bot.matrix_id.domain = "localhost"
    bot.client = AsyncMock()
    bot.logger = AsyncMock()
    bot.response_tracker = AsyncMock()

    # Mock the format_message_with_mentions to return a dict we can check
    mock_content = {"body": "test", "msgtype": "m.text"}

    # Create a test room and event
    room = nio.MatrixRoom(room_id="!test:server", own_user_id="@bot:server")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "!schedule in 5 minutes check email",
                "msgtype": "m.text",
            },
            "sender": "@user:server",
            "event_id": "$event123",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    # Patch the function to capture what was passed

    with patch("mindroom.bot.format_message_with_mentions") as mock_create:
        mock_create.return_value = mock_content.copy()
        with patch("mindroom.bot.send_message") as mock_send:
            mock_send.return_value = "$response123"

            # Call the actual _send_response method with skip_mentions=True
            await AgentBot._send_response(
                bot,
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text="✅ Scheduled. Will notify @email_agent",
                thread_id=None,
                reply_to_event=event,
                skip_mentions=True,
            )

            # Check that send_message was called with content that has skip_mentions
            mock_send.assert_called_once()
            sent_content = mock_send.call_args[0][2]  # Third argument is content
            assert sent_content.get("com.mindroom.skip_mentions") is True


@pytest.mark.asyncio
async def test_extract_context_with_skip_mentions() -> None:
    """Test that _extract_message_context ignores mentions when skip_mentions is set."""
    # Create a mock bot
    bot = AsyncMock(spec=AgentBot)
    bot.config = AsyncMock()
    bot.agent_name = "email_agent"
    bot.client = AsyncMock()
    bot.logger = AsyncMock()

    # Create room
    room = nio.MatrixRoom(room_id="!test:server", own_user_id="@bot:server")

    # Event with skip_mentions metadata and a mention
    event_with_skip = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "✅ Scheduled task. @email_agent will handle it",
                "msgtype": "m.text",
                "com.mindroom.skip_mentions": True,
                "m.mentions": {
                    "user_ids": ["@mindroom_email_agent:localhost"],
                },
            },
            "sender": "@router:server",
            "event_id": "$event123",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    # Mock fetch_thread_history to return empty
    with patch("mindroom.bot.fetch_thread_history") as mock_fetch:
        mock_fetch.return_value = []

        # Extract context - should not detect mentions
        context = await AgentBot._extract_message_context(bot, room, event_with_skip)

        # Verify mentions were ignored
        assert context.am_i_mentioned is False
        assert context.mentioned_agents == []

    # Now test without skip_mentions - should detect mentions
    event_without_skip = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Hey @email_agent can you help?",
                "msgtype": "m.text",
                "m.mentions": {
                    "user_ids": ["@mindroom_email_agent:localhost"],
                },
            },
            "sender": "@user:server",
            "event_id": "$event456",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    # Mock check_agent_mentioned to return that we're mentioned
    with patch("mindroom.bot.check_agent_mentioned") as mock_check:
        mock_check.return_value = (["email_agent"], True)
        with patch("mindroom.bot.fetch_thread_history") as mock_fetch:
            mock_fetch.return_value = []

            context = await AgentBot._extract_message_context(bot, room, event_without_skip)

            # Verify mentions were detected
            assert context.am_i_mentioned is True
            assert "email_agent" in context.mentioned_agents
