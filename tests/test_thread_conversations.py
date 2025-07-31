"""Test thread-based conversations with context isolation using HTTP mocking.

This file tests:
1. Single thread conversations with context preservation
2. Multiple concurrent threads with isolated contexts
3. Multi-bot conversations in threads
"""

import os
import re
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from aioresponses import aioresponses
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    MatrixRoom,
    MessageDirection,
    RoomMessageText,
)

from mindroom.bot import MinimalBot

from .test_helpers import mock_room_messages_empty, mock_room_messages_with_history

# Load environment variables from .env file
load_dotenv()

# Check if AI is configured
AI_CONFIGURED = bool(os.getenv("AGNO_MODEL"))


@pytest.fixture(autouse=True)
def ensure_test_model_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure a valid test model is configured for all tests."""
    # If no model is configured or it's set to "test", use a default
    model = os.getenv("AGNO_MODEL", "")
    if not model or "test" in model.lower():
        monkeypatch.setenv("AGNO_MODEL", "ollama:llama3.2:3b")


def _mock_room_send_response(
    m: aioresponses,
    homeserver: str,
    room_id: str,
    event_id: str,
    status: int = 200,
    **kwargs: Any,
) -> None:
    """Helper to mock room_send HTTP responses with regex matching."""
    m.put(
        re.compile(rf"{re.escape(homeserver)}/_matrix/client/v3/rooms/{re.escape(room_id)}/send/m\.room\.message/.*"),
        status=status,
        payload={"event_id": event_id},
        **kwargs,
    )


@pytest_asyncio.fixture
async def client():
    """Create an AsyncClient for testing and ensure cleanup."""
    homeserver = "https://matrix.example.org"
    user_id = "@test:example.org"
    client = AsyncClient(homeserver, user_id)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def bot():
    """Create a MinimalBot for testing and ensure cleanup."""
    homeserver = "https://matrix.example.org"
    bot_user_id = "@bot:example.org"

    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", homeserver),
        patch("mindroom.matrix.MATRIX_USER_ID", bot_user_id),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
        patch("os.path.exists", return_value=True),
    ):
        bot = MinimalBot()
        yield bot
        await bot.client.close()


async def _create_thread_message(
    event_id: str,
    sender: str,
    body: str,
    thread_root: str,
) -> RoomMessageText:
    """Create a threaded message event."""
    return RoomMessageText(
        body=body,
        formatted_body=body,
        format="org.matrix.custom.html",
        source={
            "content": {
                "msgtype": "m.text",
                "body": body,
                "m.relates_to": {
                    "event_id": thread_root,
                    "rel_type": "m.thread",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": thread_root},
                },
            },
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
        },
    )


@pytest.mark.asyncio
async def test_single_thread_context_preservation_with_ai(bot: MinimalBot) -> None:
    """Test that bot preserves context within a single thread using actual AI."""
    room_id = "!test:example.org"
    thread_root = "$thread_root:example.org"
    user_id = "@alice:example.org"

    # Set up bot as logged in
    bot.client.access_token = "test_token"
    bot.client.user_id = "@bot:example.org"
    bot.client.user = "bot"

    room = MatrixRoom(room_id, bot.client.user_id)

    # Track AI responses from actual AI
    ai_responses = []

    # Capture responses sent by the bot
    original_room_send = bot.client.room_send

    async def capture_room_send(room_id, message_type, content, tx_id=None, ignore_unverified_devices=True):
        ai_responses.append(content["body"])
        return await original_room_send(room_id, message_type, content, tx_id, ignore_unverified_devices)

    bot.client.room_send = capture_room_send

    # Mock room_messages to return empty thread history
    mock_room_messages_empty(bot)

    with aioresponses() as m:
        # Mock room_send for bot responses - use regex to match any transaction ID
        # nio generates unique transaction IDs, so we need to match them dynamically
        for i in range(1, 5):
            _mock_room_send_response(
                m,
                bot.client.homeserver,
                room_id,
                f"$response{i}:example.org",
            )

        # Use actual AI - no mocking of ai_response
        # First message: introduction
        msg1 = await _create_thread_message(
            "$msg1:example.org",
            user_id,
            f"{bot.client.user_id} Hi, my name is Charlie",
            thread_root,
        )
        msg1.sender = user_id

        await bot._on_message(room, msg1)

        # Second message: mention interest
        msg2 = await _create_thread_message(
            "$msg2:example.org",
            user_id,
            f"{bot.client.user_id} I like JavaScript programming",
            thread_root,
        )
        msg2.sender = user_id

        await bot._on_message(room, msg2)

        # Third message: ask about name
        msg3 = await _create_thread_message(
            "$msg3:example.org",
            user_id,
            f"{bot.client.user_id} What was my name?",
            thread_root,
        )
        msg3.sender = user_id

        await bot._on_message(room, msg3)

        # Fourth message: ask about programming language
        msg4 = await _create_thread_message(
            "$msg4:example.org",
            user_id,
            f"{bot.client.user_id} What's my favorite programming language?",
            thread_root,
        )
        msg4.sender = user_id

        await bot._on_message(room, msg4)

    # Verify AI responded to all messages
    assert len(ai_responses) == 4

    # Join all responses for easier searching
    all_responses = " ".join(ai_responses).lower()

    # Verify the AI remembers Charlie from the introduction
    assert "charlie" in all_responses

    # Verify the AI remembers JavaScript as the favorite language
    assert "javascript" in all_responses

    # The response to "What was my name?" should specifically mention Charlie
    name_response = ai_responses[2].lower()
    assert "charlie" in name_response

    # The response to "What's my favorite programming language?" should mention JavaScript
    language_response = ai_responses[3].lower()
    assert "javascript" in language_response


@pytest.mark.asyncio
async def test_multiple_threads_context_isolation_with_ai(bot: MinimalBot) -> None:
    """Test that contexts are isolated between different threads using actual AI."""
    room_id = "!test:example.org"
    thread1_root = "$thread1:example.org"
    thread2_root = "$thread2:example.org"

    # Set up bot as logged in
    bot.client.access_token = "test_token"
    bot.client.user_id = "@bot:example.org"
    bot.client.user = "bot"

    room = MatrixRoom(room_id, bot.client.user_id)

    # Track AI responses by thread
    thread_responses: dict[str, list[str]] = {thread1_root: [], thread2_root: []}

    # Capture responses sent by the bot
    original_room_send = bot.client.room_send

    async def capture_room_send(room_id, message_type, content, tx_id=None, ignore_unverified_devices=True):
        # Determine which thread this response belongs to based on the content structure
        if "m.relates_to" in content and content["m.relates_to"].get("event_id") in thread_responses:
            thread_id = content["m.relates_to"]["event_id"]
            thread_responses[thread_id].append(content["body"])
        return await original_room_send(room_id, message_type, content, tx_id, ignore_unverified_devices)

    bot.client.room_send = capture_room_send

    # Mock room_messages to return empty thread history
    mock_room_messages_empty(bot)

    with aioresponses() as m:
        # Mock room_send for all bot responses - use regex to match any transaction ID
        for i in range(6):
            _mock_room_send_response(
                m,
                bot.client.homeserver,
                room_id,
                f"$response{i}:example.org",
            )

        # Use actual AI - no mocking of ai_response
        # Thread 1: Charlie likes JavaScript
        msg1_t1 = await _create_thread_message(
            "$msg1_t1:example.org",
            "@charlie:example.org",
            "@bot Hi, I'm Charlie. I like JavaScript",
            thread1_root,
        )
        msg1_t1.sender = "@charlie:example.org"

        # Thread 2: Bob likes Python
        msg1_t2 = await _create_thread_message(
            "$msg1_t2:example.org",
            "@bob:example.org",
            "@bot Hi, I'm Bob. I like Python",
            thread2_root,
        )
        msg1_t2.sender = "@bob:example.org"

        # Process both initial messages
        await bot._on_message(room, msg1_t1)
        await bot._on_message(room, msg1_t2)

        # Thread 1: Charlie asks about name
        msg2_t1 = await _create_thread_message(
            "$msg2_t1:example.org",
            "@charlie:example.org",
            "@bot What was my name?",
            thread1_root,
        )
        msg2_t1.sender = "@charlie:example.org"

        # Thread 2: Bob asks about language
        msg2_t2 = await _create_thread_message(
            "$msg2_t2:example.org",
            "@bob:example.org",
            "@bot What's my favorite programming language?",
            thread2_root,
        )
        msg2_t2.sender = "@bob:example.org"

        # Process follow-up messages
        await bot._on_message(room, msg2_t1)
        await bot._on_message(room, msg2_t2)

        # Thread 1: Charlie asks about language
        msg3_t1 = await _create_thread_message(
            "$msg3_t1:example.org",
            "@charlie:example.org",
            "@bot And my favorite programming language?",
            thread1_root,
        )
        msg3_t1.sender = "@charlie:example.org"

        # Thread 2: Bob asks about name
        msg3_t2 = await _create_thread_message(
            "$msg3_t2:example.org",
            "@bob:example.org",
            "@bot What was my name again?",
            thread2_root,
        )
        msg3_t2.sender = "@bob:example.org"

        # Process final messages
        await bot._on_message(room, msg3_t1)
        await bot._on_message(room, msg3_t2)

    # Verify each thread had its own conversation
    assert len(thread_responses[thread1_root]) == 3  # Charlie's 3 responses
    assert len(thread_responses[thread2_root]) == 3  # Bob's 3 responses

    # Join responses for each thread
    charlie_thread = " ".join(thread_responses[thread1_root]).lower()
    bob_thread = " ".join(thread_responses[thread2_root]).lower()

    # Verify Charlie's thread maintains Charlie/JavaScript context
    assert "charlie" in charlie_thread
    assert "javascript" in charlie_thread

    # Verify Bob's thread maintains Bob/Python context
    assert "bob" in bob_thread
    assert "python" in bob_thread

    # Verify context isolation - Charlie's info shouldn't leak to Bob's thread
    assert "charlie" not in bob_thread
    assert "javascript" not in bob_thread

    # Verify context isolation - Bob's info shouldn't leak to Charlie's thread
    assert "bob" not in charlie_thread
    assert "python" not in charlie_thread


@pytest.mark.asyncio
async def test_multiple_agents_maintain_separate_contexts_in_thread(bot: MinimalBot) -> None:
    """Test that multiple agents (calculator and general) maintain separate contexts in the same thread."""
    room_id = "!test:example.org"
    thread_root = "$thread:example.org"
    user_id = "@user:example.org"

    # Set up bot as logged in
    bot.client.access_token = "test_token"
    bot.client.user_id = "@bot:example.org"
    bot.client.user = "bot"

    room = MatrixRoom(room_id, bot.client.user_id)

    # Track AI responses
    ai_responses = []

    # Capture responses sent by the bot
    original_room_send = bot.client.room_send

    async def capture_room_send(room_id, message_type, content, tx_id=None, ignore_unverified_devices=True):
        ai_responses.append(content["body"])
        return await original_room_send(room_id, message_type, content, tx_id, ignore_unverified_devices)

    bot.client.room_send = capture_room_send

    # Mock room_messages to return empty thread history
    mock_room_messages_empty(bot)

    with aioresponses() as m:
        # Mock room_send for bot responses
        for i in range(1, 7):
            _mock_room_send_response(
                m,
                bot.client.homeserver,
                room_id,
                f"$response{i}:example.org",
            )

        # Test conversation with multiple agents in the same thread

        # First: Ask general agent about the weather
        msg1 = await _create_thread_message(
            "$msg1:example.org",
            user_id,
            "@general: What's the weather like today?",
            thread_root,
        )
        msg1.sender = user_id
        await bot._on_message(room, msg1)

        # Second: Ask calculator agent for math
        msg2 = await _create_thread_message(
            "$msg2:example.org",
            user_id,
            "@calculator: What's 25 * 4?",
            thread_root,
        )
        msg2.sender = user_id
        await bot._on_message(room, msg2)

        # Third: Continue with general agent
        msg3 = await _create_thread_message(
            "$msg3:example.org",
            user_id,
            "@general: Should I bring an umbrella?",
            thread_root,
        )
        msg3.sender = user_id
        await bot._on_message(room, msg3)

        # Fourth: More math with calculator
        msg4 = await _create_thread_message(
            "$msg4:example.org",
            user_id,
            "@calculator: Now divide that by 5",
            thread_root,
        )
        msg4.sender = user_id
        await bot._on_message(room, msg4)

        # Fifth: Back to general agent
        msg5 = await _create_thread_message(
            "$msg5:example.org",
            user_id,
            "@general: Thanks for the weather info!",
            thread_root,
        )
        msg5.sender = user_id
        await bot._on_message(room, msg5)

        # Sixth: Calculator should remember previous calculation
        msg6 = await _create_thread_message(
            "$msg6:example.org",
            user_id,
            "@calculator: What was the result of the multiplication again?",
            thread_root,
        )
        msg6.sender = user_id
        await bot._on_message(room, msg6)

    # Verify all agents responded
    assert len(ai_responses) == 6

    # Separate responses by agent (based on message order)
    general_responses = [ai_responses[0], ai_responses[2], ai_responses[4]]
    calculator_responses = [ai_responses[1], ai_responses[3], ai_responses[5]]

    # Join responses for analysis
    general_text = " ".join(general_responses).lower()
    calculator_text = " ".join(calculator_responses).lower()

    # Verify general agent discussed weather
    assert any(word in general_text for word in ["weather", "umbrella", "rain", "sunny", "cloudy"]), (
        "General agent should discuss weather topics"
    )

    # Verify calculator agent did math
    assert any(word in calculator_text for word in ["100", "20", "multiply", "divide", "result"]), (
        "Calculator agent should provide math results"
    )

    # Verify calculator maintains context - should reference the 25*4=100 calculation
    last_calc_response = calculator_responses[2].lower()
    assert any(word in last_calc_response for word in ["100", "multiplication", "25", "4"]), (
        "Calculator should remember previous calculation context"
    )


@pytest.mark.asyncio
async def test_thread_all_messages_treated_as_mentions_with_ai(bot: MinimalBot) -> None:
    """Test that all thread messages are treated as bot mentions using actual AI."""
    room_id = "!test:example.org"
    thread_root = "$thread:example.org"
    user_id = "@user:example.org"

    # Set up bot as logged in
    bot.client.access_token = "test_token"
    bot.client.user_id = "@bot:example.org"
    bot.client.user = "bot"

    room = MatrixRoom(room_id, bot.client.user_id)

    # Track AI responses from actual AI
    ai_responses = []

    # Capture responses sent by the bot
    original_room_send = bot.client.room_send

    async def capture_room_send(room_id, message_type, content, tx_id=None, ignore_unverified_devices=True):
        ai_responses.append(content["body"])
        return await original_room_send(room_id, message_type, content, tx_id, ignore_unverified_devices)

    bot.client.room_send = capture_room_send

    # Mock room_messages to return empty thread history
    mock_room_messages_empty(bot)

    with aioresponses() as m:
        # Mock responses for all messages since bot treats all thread messages as mentions
        # Use regex to match any transaction ID
        for i in range(1, 4):
            _mock_room_send_response(
                m,
                bot.client.homeserver,
                room_id,
                f"$response{i}:example.org",
            )

        # Use actual AI - no mocking of ai_response
        # First message: no explicit bot mention, ask a question
        msg1 = await _create_thread_message(
            "$msg1:example.org",
            user_id,
            "What's the weather like today?",
            thread_root,
        )
        msg1.sender = user_id

        await bot._on_message(room, msg1)

        # Second message: still no explicit bot mention, follow-up question
        msg2 = await _create_thread_message(
            "$msg2:example.org",
            user_id,
            "Should I bring an umbrella?",
            thread_root,
        )
        msg2.sender = user_id

        await bot._on_message(room, msg2)

        # Third message: explicit bot mention
        msg3 = await _create_thread_message(
            "$msg3:example.org",
            user_id,
            f"{bot.client.user_id} Thanks for your help!",
            thread_root,
        )
        msg3.sender = user_id

        await bot._on_message(room, msg3)

    # All messages should have triggered responses
    assert len(ai_responses) == 3, "Bot should respond to all thread messages"

    # Verify all responses are meaningful
    assert all(len(resp) > 20 for resp in ai_responses), "All responses should be substantial"

    # Join all responses for analysis
    all_responses = " ".join(ai_responses).lower()

    # Verify the bot engaged with the weather topic
    assert any(word in all_responses for word in ["weather", "umbrella", "rain", "forecast", "temperature"])

    # Verify the bot maintains context - the second response should relate to umbrellas/rain
    second_response = ai_responses[1].lower()
    assert any(word in second_response for word in ["umbrella", "rain", "weather", "bring", "need"])

    # The third response should acknowledge the thanks
    third_response = ai_responses[2].lower()
    assert any(word in third_response for word in ["welcome", "glad", "help", "happy", "pleasure"])


@pytest.mark.asyncio
async def test_agent_sees_full_thread_history(bot: MinimalBot) -> None:
    """Test that agents can access entire thread history when mentioned."""
    room_id = "!test:example.org"
    thread_root = "$thread_root:example.org"
    user_id = "@alice:example.org"

    # Set up bot as logged in
    bot.client.access_token = "test_token"
    bot.client.user_id = "@bot:example.org"
    bot.client.user = "bot"

    room = MatrixRoom(room_id, bot.client.user_id)

    # Track AI responses
    ai_responses = []

    # Capture responses sent by the bot
    original_room_send = bot.client.room_send

    async def capture_room_send(room_id, message_type, content, tx_id=None, ignore_unverified_devices=True):
        ai_responses.append(content["body"])
        return await original_room_send(room_id, message_type, content, tx_id, ignore_unverified_devices)

    bot.client.room_send = capture_room_send

    # Mock room_messages with thread history
    thread_history = [
        ("@bob:example.org", "Hi everyone, I'm planning a trip to Japan", "$hist1:example.org"),
        ("@charlie:example.org", "That sounds exciting! When are you planning to go?", "$hist2:example.org"),
        ("@bob:example.org", "I'm thinking April for the cherry blossoms", "$hist3:example.org"),
        ("@charlie:example.org", "Great choice! I went in April 2019 and it was beautiful", "$hist4:example.org"),
    ]
    mock_room_messages_with_history(bot, thread_root, thread_history)

    with aioresponses() as m:
        # Mock room_send for bot response
        _mock_room_send_response(
            m,
            bot.client.homeserver,
            room_id,
            "$response1:example.org",
        )

        # Alice joins the thread late and asks for recommendations
        msg = await _create_thread_message(
            "$msg1:example.org",
            user_id,
            f"{bot.client.user_id} What are some good places to visit in Japan based on what Bob and Charlie discussed?",
            thread_root,
        )
        msg.sender = user_id

        await bot._on_message(room, msg)

    # Verify the bot was called with room_messages
    bot.client.room_messages.assert_called_once_with(
        room_id,
        start=None,
        limit=100,
        message_filter={"types": ["m.room.message"]},
        direction=MessageDirection.back,
    )

    # Verify AI responded
    assert len(ai_responses) == 1

    # Verify the response shows awareness of the thread history
    response = ai_responses[0].lower()

    # The bot should reference the April/cherry blossom discussion
    assert any(word in response for word in ["april", "cherry", "blossom", "spring"]), (
        "Bot should reference the April/cherry blossom timing from thread history"
    )

    # The bot should show awareness this is about Japan
    assert "japan" in response, "Bot should know the conversation is about Japan"

    # Optionally check if bot references Bob or Charlie's experience
    # This is optional as the bot might not always mention names
    bob_charlie_mentioned = "bob" in response or "charlie" in response or "2019" in response
    if bob_charlie_mentioned:
        pass
