"""Test that skip_mentions metadata prevents agents from responding to mentions."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.bot import AgentBot, _should_skip_mentions
from mindroom.matrix.identity import MatrixID


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
    bot = Mock(spec=AgentBot)
    bot.agent_name = "email_agent"
    bot.config = Mock()
    bot.matrix_id = MatrixID.from_agent("email_agent", "localhost")
    bot.client = Mock()
    bot.logger = Mock()
    bot.response_tracker = Mock()
    bot._resolve_reply_thread_id = Mock(return_value=None)

    mock_content = {"body": "test", "msgtype": "m.text"}
    bot.format_message_with_mentions = Mock(return_value=mock_content.copy())
    bot.send_message = AsyncMock(return_value="$response123")

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

    await AgentBot._send_response(
        bot,
        room_id=room.room_id,
        reply_to_event_id=event.event_id,
        response_text="✅ Scheduled. Will notify @email_agent",
        thread_id=None,
        reply_to_event=event,
        skip_mentions=True,
    )

    bot.send_message.assert_awaited_once()
    sent_content = bot.send_message.await_args.args[2]
    assert sent_content.get("com.mindroom.skip_mentions") is True


@pytest.mark.asyncio
async def test_extract_context_with_skip_mentions() -> None:
    """Test that _extract_message_context ignores mentions when skip_mentions is set."""
    bot = Mock(spec=AgentBot)
    bot.config = Mock()
    bot.config.get_entity_thread_mode.return_value = "thread"
    bot.agent_name = "email_agent"
    bot.client = Mock()
    bot.logger = Mock()
    bot.matrix_id = MatrixID.from_agent("email_agent", "localhost")
    bot._derive_conversation_context = AsyncMock(return_value=(False, None, []))
    bot.check_agent_mentioned = Mock(return_value=([bot.matrix_id], True, False))

    room = nio.MatrixRoom(room_id="!test:server", own_user_id="@bot:server")

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

    context = await AgentBot._extract_message_context(bot, room, event_with_skip)

    assert context.am_i_mentioned is False
    assert context.mentioned_agents == []

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

    context = await AgentBot._extract_message_context(bot, room, event_without_skip)

    assert context.am_i_mentioned is True
    assert bot.matrix_id in context.mentioned_agents
