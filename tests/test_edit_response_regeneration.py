"""Test that agent responses are regenerated when user edits their message."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker


@pytest.mark.asyncio
async def test_bot_regenerates_response_on_edit(tmp_path: Path) -> None:
    """Test that the bot regenerates its response when a user edits their message."""
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

    # Create an original message event
    original_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@test_agent what is 2+2?",
                "msgtype": "m.text",
            },
            "event_id": "$original:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    original_event.source = {
        "content": {
            "body": "@test_agent what is 2+2?",
            "msgtype": "m.text",
        },
        "event_id": "$original:example.com",
        "sender": "@user:example.com",
    }

    # Simulate that the bot has already responded to the original message
    response_event_id = "$response:example.com"
    bot.response_tracker.mark_responded(original_event.event_id, response_event_id)

    # Create an edit event
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
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
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
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the methods needed for regeneration
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
        patch("mindroom.bot.should_agent_respond") as mock_should_respond,
        patch("mindroom.bot.should_use_streaming", new_callable=AsyncMock) as mock_streaming,
        patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai_response,
    ):
        # Setup mocks
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=["test_agent"],
        )
        mock_should_respond.return_value = True
        mock_streaming.return_value = False  # Use non-streaming for simpler test
        mock_ai_response.return_value = "The answer is 6"

        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot attempted to regenerate the response
        mock_context.assert_called_once()
        mock_should_respond.assert_called_once()
        mock_ai_response.assert_called_once()

        # Verify that the bot edited the existing response message
        mock_edit.assert_called_once_with(
            room.room_id,
            response_event_id,
            "The answer is 6",
            None,  # thread_id
        )

        # Verify that the response tracker still maps to the same response
        assert bot.response_tracker.get_response_event_id(original_event.event_id) == response_event_id


@pytest.mark.asyncio
async def test_bot_ignores_edit_without_previous_response(tmp_path: Path) -> None:
    """Test that the bot ignores edits if it didn't respond to the original message."""
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

    # Create an edit event for a message we never responded to
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent help",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent help",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$unknown:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent help",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent help",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$unknown:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the methods
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot did NOT attempt to regenerate
        mock_context.assert_not_called()
        mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_bot_ignores_own_edits(tmp_path: Path) -> None:
    """Test that the bot ignores its own edit events (e.g., streaming edits)."""
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

    # Simulate that the bot has responded to some message
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

    # Create an edit event from the bot itself
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Updated response",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Updated response",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@test_agent:example.com",  # Bot's own edit
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* Updated response",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Updated response",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@test_agent:example.com",
    }

    # Mock the methods
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot did NOT attempt to regenerate (ignores its own edits)
        mock_context.assert_not_called()
        mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_response_tracker_mapping_persistence(tmp_path: Path) -> None:
    """Test that ResponseTracker correctly persists and retrieves user-to-response mappings."""
    # Create a response tracker
    tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mark some responses
    user_event_1 = "$user1:example.com"
    response_event_1 = "$response1:example.com"
    tracker.mark_responded(user_event_1, response_event_1)

    user_event_2 = "$user2:example.com"
    response_event_2 = "$response2:example.com"
    tracker.mark_responded(user_event_2, response_event_2)

    # Verify mappings are stored
    assert tracker.get_response_event_id(user_event_1) == response_event_1
    assert tracker.get_response_event_id(user_event_2) == response_event_2
    assert tracker.get_response_event_id("$unknown:example.com") is None

    # Create a new tracker instance to test persistence
    tracker2 = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Verify mappings were loaded from disk
    assert tracker2.get_response_event_id(user_event_1) == response_event_1
    assert tracker2.get_response_event_id(user_event_2) == response_event_2

    # Test removal
    tracker2.remove_response_mapping(user_event_1)
    assert tracker2.get_response_event_id(user_event_1) is None
    assert tracker2.get_response_event_id(user_event_2) == response_event_2

    # Verify removal persists
    tracker3 = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    assert tracker3.get_response_event_id(user_event_1) is None
    assert tracker3.get_response_event_id(user_event_2) == response_event_2
