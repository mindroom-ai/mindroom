"""Test that agents can handle multiple consecutive edits to the same message."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config, ModelConfig, RouterConfig
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_agent_regenerates_on_multiple_edits(tmp_path: Path) -> None:
    """Test that agents regenerate their response on each consecutive edit."""
    # Set up agent and bot
    agent_user = AgentMatrixUser(
        user_id="@mindroom_test:localhost",
        password=TEST_PASSWORD,
        display_name="TestAgent",
        agent_name="test",
    )

    config = Config(
        agents={"test": AgentConfig(display_name="TestAgent", rooms=["!test:localhost"])},
        teams={},
        room_models={},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        rooms=["!test:localhost"],
        enable_streaming=False,
        config=config,
    )

    # Mock the orchestrator and client
    mock_orchestrator = MagicMock()
    mock_orchestrator.current_config = config
    bot.orchestrator = mock_orchestrator

    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@mindroom_test:localhost"

    # Mock room send to return a response event ID
    mock_send_response = MagicMock()
    mock_send_response.__class__ = nio.RoomSendResponse
    mock_send_response.event_id = "$response123"
    bot.client.room_send.return_value = mock_send_response

    # Mock room messages for thread history
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse.from_dict(
            {"chunk": [], "start": "s1", "end": "e1"},
            room_id="!test:localhost",
        ),
    )

    # Set up room
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)

    # Original message from user
    original_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@mindroom_test What's 2+2?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},
            },
            "event_id": "$original123",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    original_event.source = original_event.__dict__["source"]

    # Process original message with mocked AI response
    with patch("mindroom.bot.ai_response", AsyncMock(return_value="Original: 4")):
        await bot._on_message(room, original_event)

    # Verify bot responded
    assert bot.client.room_send.call_count == 2  # thinking + final

    # Reset mock
    bot.client.room_send.reset_mock()

    # First edit from user
    edit1_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @mindroom_test What's 3+3?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},  # Mentions at top level
                "m.new_content": {
                    "body": "@mindroom_test What's 3+3?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original123",  # Points to original
                },
            },
            "event_id": "$edit1",
            "sender": "@user:localhost",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    edit1_event.source = edit1_event.__dict__["source"]

    # Process first edit with mocked AI response
    with patch("mindroom.bot.ai_response", AsyncMock(return_value="Edit 1: 6")):
        await bot._on_message(room, edit1_event)

    # Verify bot regenerated (sends thinking message when editing)
    assert bot.client.room_send.call_count >= 1
    print(f"After first edit, room_send called {bot.client.room_send.call_count} times")

    # Reset mock
    bot.client.room_send.reset_mock()

    # Second edit from user
    edit2_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @mindroom_test What's 4+4?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},  # Mentions at top level
                "m.new_content": {
                    "body": "@mindroom_test What's 4+4?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original123",  # Still points to original!
                },
            },
            "event_id": "$edit2",
            "sender": "@user:localhost",
            "origin_server_ts": 1000002,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    edit2_event.source = edit2_event.__dict__["source"]

    # Process second edit with mocked AI response
    with patch("mindroom.bot.ai_response", AsyncMock(return_value="Edit 2: 8")):
        await bot._on_message(room, edit2_event)

    # Verify bot regenerated again
    assert bot.client.room_send.call_count >= 1, "Bot should regenerate on second edit"
    print(f"After second edit, room_send called {bot.client.room_send.call_count} times")

    # Third edit from user
    edit3_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @mindroom_test What's 5+5?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},  # Mentions at top level
                "m.new_content": {
                    "body": "@mindroom_test What's 5+5?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_test:localhost"]},
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original123",  # Always points to original
                },
            },
            "event_id": "$edit3",
            "sender": "@user:localhost",
            "origin_server_ts": 1000003,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    edit3_event.source = edit3_event.__dict__["source"]

    # Reset mock
    bot.client.room_send.reset_mock()

    # Process third edit with mocked AI response
    with patch("mindroom.bot.ai_response", AsyncMock(return_value="Edit 3: 10")):
        await bot._on_message(room, edit3_event)

    # Verify bot regenerated yet again
    assert bot.client.room_send.call_count >= 1, "Bot should regenerate on third edit"
    print(f"After third edit, room_send called {bot.client.room_send.call_count} times")
