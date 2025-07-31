import pytest

from mindroom.matrix import parse_message


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


# Thread reply test removed - functionality is now tested in test_multi_agent_bot.py
# The old Bot class is deprecated and only exists for backward compatibility
