from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nio import RoomMessageText

from mindroom.bot import Bot
from mindroom.matrix import parse_message

from .test_helpers import mock_room_messages_empty


@pytest.mark.parametrize(
    ("message", "expected_agent", "expected_prompt"),
    [
        ("@calculator: 2 + 2", "calculator", "2 + 2"),
        ("@general: Hello", "general", "Hello"),
        ("@bot_user_id: Hello", "general", "Hello"),
        ("Hello @bot_user_id", "general", "Hello"),
    ],
)
def test_parse_message(message: str, expected_agent: str, expected_prompt: str) -> None:
    """Tests the parse_message function."""
    bot_user_id = "@bot_user_id:matrix.org"
    bot_display_name = "Bot User"

    # Replace placeholder with actual bot user id
    message = message.replace("@bot_user_id", bot_user_id)

    result = parse_message(message, bot_user_id, bot_display_name)
    assert result is not None
    agent_name, prompt = result
    assert agent_name == expected_agent
    assert prompt == expected_prompt


def test_parse_message_no_mention() -> None:
    """Tests that a message with no mention returns None."""
    result = parse_message("Hello world", "@bot_user_id:matrix.org", "Bot User")
    assert result is None


@pytest.mark.asyncio
@patch("mindroom.bot.ai_response", new_callable=AsyncMock)
async def test_on_message_thread_reply(mock_ai_response: AsyncMock) -> None:
    """Tests that the bot replies in a thread."""
    # Arrange
    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", "https://example.org"),
        patch("mindroom.matrix.MATRIX_USER_ID", "@bot:localhost"),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
    ):
        bot = Bot()
    bot.client = AsyncMock()
    bot.client.user_id = "@bot:localhost"
    bot.client.user = "bot"

    room = MagicMock()
    room.room_id = "!room:localhost"

    event = RoomMessageText(
        body="a threaded message",
        formatted_body="a threaded message",
        format="org.matrix.custom.html",
        source={
            "content": {
                "body": "a threaded message",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root:localhost",
                },
            },
            "event_id": "$event:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
        },
    )
    event.sender = "@user:localhost"

    mock_ai_response.return_value = "a threaded response"

    # Mock room_messages to return empty thread history
    mock_room_messages_empty(bot)

    # Act
    await bot._on_message(room, event)

    # Assert
    # Session ID should include thread ID for context isolation
    expected_session_id = f"{room.room_id}:$thread_root:localhost"
    mock_ai_response.assert_called_once_with("general", "a threaded message", expected_session_id, thread_history=[])
    bot.client.room_send.assert_called_once()
    sent_content = bot.client.room_send.call_args[1]["content"]
    assert sent_content["m.relates_to"]["rel_type"] == "m.thread"
    assert sent_content["m.relates_to"]["event_id"] == "$thread_root:localhost"
