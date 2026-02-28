"""Test that bot handles voice message threading correctly."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import ROUTER_AGENT_NAME, AgentBot
from mindroom.config.main import Config
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD


@pytest.fixture
def mock_router_bot() -> AgentBot:
    """Create a mock router bot for testing."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
        display_name="Router Agent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    config = Config.from_yaml()
    with tempfile.TemporaryDirectory() as tmpdir:
        bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
    bot.client = AsyncMock()
    bot.logger = MagicMock()
    bot._send_response = AsyncMock()
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    return bot


@pytest.mark.asyncio
async def test_voice_message_in_main_room_creates_thread(mock_router_bot: AgentBot) -> None:
    """Test that voice message in main room creates a thread from the voice message."""
    bot = mock_router_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None

    # Voice message in main room (not in a thread)
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = "$voice123"
    voice_event.sender = "@user:example.com"
    voice_event.source = {"content": {}}  # No thread relation

    # Mock voice handler to return transcription
    with patch("mindroom.bot.voice_handler.handle_voice_message", return_value="🎤 what is the weather"):
        await bot._on_voice_message(room, voice_event)

        # Verify _send_response was called with correct threading
        bot._send_response.assert_called_once()
        call_kwargs = bot._send_response.call_args[1]

        # Should reply to voice message
        assert call_kwargs["reply_to_event_id"] == "$voice123"
        # Should NOT have a thread_id (None means create new thread from reply_to)
        assert call_kwargs["thread_id"] is None
        # Message should have voice prefix
        assert call_kwargs["response_text"] == "🎤 what is the weather"


@pytest.mark.asyncio
async def test_voice_message_in_thread_continues_thread(mock_router_bot: AgentBot) -> None:
    """Test that voice message in an existing thread continues that thread."""
    bot = mock_router_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None

    # Voice message in an existing thread
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = "$voice456"
    voice_event.sender = "@user:example.com"
    voice_event.source = {
        "content": {
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",  # Part of existing thread
            },
        },
    }

    # Mock voice handler to return transcription
    with patch("mindroom.bot.voice_handler.handle_voice_message", return_value="🎤 show me the forecast"):
        await bot._on_voice_message(room, voice_event)

        # Verify _send_response was called with correct threading
        bot._send_response.assert_called_once()
        call_kwargs = bot._send_response.call_args[1]

        # Should reply to voice message
        assert call_kwargs["reply_to_event_id"] == "$voice456"
        # Should continue in the SAME thread
        assert call_kwargs["thread_id"] == "$thread_root"
        # Message should have voice prefix
        assert call_kwargs["response_text"] == "🎤 show me the forecast"


@pytest.mark.asyncio
async def test_voice_plain_reply_to_thread_message_uses_thread_root(mock_router_bot: AgentBot) -> None:
    """Voice messages without m.thread should still resolve to existing thread roots via reply chain."""
    bot = mock_router_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None

    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = "$voice789"
    voice_event.sender = "@user:example.com"
    voice_event.source = {
        "content": {
            "m.relates_to": {
                "m.in_reply_to": {"event_id": "$thread_msg"},
            },
        },
    }

    bot.client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventResponse.from_dict(
            {
                "content": {
                    "body": "Earlier thread message",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "event_id": "$thread_msg",
                "sender": "@mindroom_general:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:server",
                "type": "m.room.message",
            },
        ),
    )

    with (
        patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=[])),
        patch("mindroom.bot.voice_handler.handle_voice_message", return_value="🎤 continue the same thread"),
    ):
        await bot._on_voice_message(room, voice_event)

    bot._send_response.assert_called_once()
    call_kwargs = bot._send_response.call_args[1]
    assert call_kwargs["reply_to_event_id"] == "$voice789"
    assert call_kwargs["thread_id"] == "$thread_root"
    assert call_kwargs["response_text"] == "🎤 continue the same thread"
