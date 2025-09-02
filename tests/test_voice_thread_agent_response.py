"""Test that agents respond correctly to voice transcriptions in threads."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import ROUTER_AGENT_NAME, AgentBot
from mindroom.config import Config
from mindroom.constants import VOICE_PREFIX
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD


@pytest.fixture
def mock_home_bot() -> AgentBot:
    """Create a mock home assistant bot for testing."""
    agent_user = AgentMatrixUser(
        agent_name="home",
        user_id="@mindroom_home:localhost",
        display_name="HomeAssistant",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    config = Config.from_yaml()
    with tempfile.TemporaryDirectory() as tmpdir:
        bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
    bot.client = AsyncMock()
    bot.logger = MagicMock()
    bot._generate_response = AsyncMock()  # type: ignore[method-assign]
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    return bot


@pytest.mark.asyncio
async def test_agent_responds_to_voice_transcription_in_thread(mock_home_bot: AgentBot) -> None:
    """Test that agents respond to router's voice transcriptions in threads.

    Scenario:
    1. User starts a thread asking HomeAssistant something
    2. HomeAssistant responds in the thread
    3. User sends a voice message with "turn on the lights"
    4. Router transcribes it as "🎤 turn on the lights"
    5. HomeAssistant should respond (as the only agent in thread)
    """
    bot = mock_home_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"

    # Create a voice transcription message from the router
    voice_transcription_event = MagicMock(spec=nio.RoomMessageText)
    voice_transcription_event.event_id = "$transcription123"
    voice_transcription_event.sender = f"@mindroom_{ROUTER_AGENT_NAME}:localhost"  # Router's user ID
    voice_transcription_event.body = f"{VOICE_PREFIX}turn on the guest room lights"
    voice_transcription_event.source = {
        "content": {
            "body": f"{VOICE_PREFIX}turn on the guest room lights",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    # Mock thread history showing HomeAssistant has participated
    thread_history = [
        {
            "event_id": "$thread_root",
            "sender": "@user:example.com",
            "content": {"body": "@home what lights are on?"},
        },
        {
            "event_id": "$home_response",
            "sender": "@mindroom_home:localhost",  # HomeAssistant's response
            "content": {"body": "The living room and kitchen lights are currently on."},
        },
    ]

    # Mock context extraction
    with (
        patch("mindroom.bot.fetch_thread_history", return_value=thread_history),
        patch("mindroom.bot.extract_agent_name") as mock_extract_agent,
        patch("mindroom.bot.get_agents_in_thread", return_value=["home"]),  # HomeAssistant is in thread
        patch("mindroom.bot.should_agent_respond", return_value=True),  # HomeAssistant should respond
    ):
        # Set up extract_agent_name to return correct values
        def extract_agent_side_effect(user_id: str, config: Config) -> str | None:  # noqa: ARG001
            if user_id == f"@mindroom_{ROUTER_AGENT_NAME}:localhost":
                return ROUTER_AGENT_NAME
            if user_id == "@mindroom_home:localhost":
                return "home"
            return None

        mock_extract_agent.side_effect = extract_agent_side_effect

        # Process the voice transcription
        await bot._on_message(room, voice_transcription_event)

        # Verify that HomeAssistant generates a response
        bot._generate_response.assert_called_once()
        call_kwargs = bot._generate_response.call_args[1]

        # Should be responding to the voice transcription
        assert call_kwargs["prompt"] == f"{VOICE_PREFIX}turn on the guest room lights"
        assert call_kwargs["reply_to_event_id"] == "$transcription123"
        assert call_kwargs["thread_id"] == "$thread_root"


@pytest.mark.asyncio
async def test_agent_ignores_non_voice_router_messages(mock_home_bot: AgentBot) -> None:
    """Test that agents still ignore regular router messages (not voice)."""
    bot = mock_home_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"

    # Create a regular message from the router (not voice)
    router_message = MagicMock(spec=nio.RoomMessageText)
    router_message.event_id = "$router_msg"
    router_message.sender = f"@mindroom_{ROUTER_AGENT_NAME}:localhost"
    router_message.body = "I'll help you with that"  # No voice prefix
    router_message.source = {
        "content": {
            "body": "I'll help you with that",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    # Mock context extraction
    with (
        patch("mindroom.bot.fetch_thread_history", return_value=[]),
        patch("mindroom.bot.extract_agent_name") as mock_extract_agent,
    ):

        def extract_agent_side_effect(user_id: str, config: Config) -> str | None:  # noqa: ARG001
            if user_id == f"@mindroom_{ROUTER_AGENT_NAME}:localhost":
                return ROUTER_AGENT_NAME
            if user_id == "@mindroom_home:localhost":
                return "home"
            return None

        mock_extract_agent.side_effect = extract_agent_side_effect

        # Process the regular router message
        await bot._on_message(room, router_message)

        # Verify that HomeAssistant does NOT generate a response
        bot._generate_response.assert_not_called()
