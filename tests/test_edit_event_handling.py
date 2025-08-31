"""Test that edit events are not processed as new messages."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser


@pytest.mark.asyncio
async def test_bot_ignores_edit_events(tmp_path: Path) -> None:
    """Test that the bot does not process edit events as new messages.

    This is a regression test for the bug where edit events (with m.relates_to.rel_type == "m.replace")
    were being treated as new messages, causing the router to create threads from them.
    """
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@router:example.com",
        display_name="Router",
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
    bot.client.user_id = "@router:example.com"

    # Mock other dependencies
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@router:example.com")

    # Create an original message event
    original_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Original message",
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
            "body": "Original message",
            "msgtype": "m.text",
        },
        "event_id": "$original:example.com",
        "sender": "@user:example.com",
    }

    # Create an edit event - this is what Matrix sends when a message is edited
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Edited message",  # Note the "* " prefix
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Edited message",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",  # This indicates it's an edit
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
            "body": "* Edited message",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Edited message",
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

    # Mock the routing method that would be called for the router
    with patch.object(bot, "_handle_ai_routing", new_callable=AsyncMock) as mock_routing:
        # Process the original message - this should trigger routing
        await bot._on_message(room, original_event)

        # The router should have attempted to route the original message
        assert mock_routing.called, "Router should process original messages"
        mock_routing.reset_mock()

        # Process the edit event - this should NOT trigger routing
        await bot._on_message(room, edit_event)

        # The router should NOT have attempted to route the edit
        # This assertion will FAIL with the current code and PASS once fixed
        assert not mock_routing.called, "Router should NOT process edit events as new messages"


@pytest.mark.asyncio
async def test_bot_ignores_multiple_edits(tmp_path: Path) -> None:
    """Test that the bot ignores multiple consecutive edits."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@router:example.com",
        display_name="Router",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        rooms=["!test:example.com"],
    )

    # Mock the client and dependencies
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@router:example.com"
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@router:example.com")

    # Create multiple edit events like what happened in the bug
    edit_events = []
    for i in range(1, 4):
        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": f"* edit {i}",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": f"edit {i}",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {
                        "event_id": "$original:example.com",
                        "rel_type": "m.replace",
                    },
                },
                "event_id": f"$edit{i}:example.com",
                "sender": "@user:example.com",
                "origin_server_ts": 1000000 + i,
                "type": "m.room.message",
                "room_id": "!test:example.com",
            },
        )
        edit_event.source = {
            "content": {
                "body": f"* edit {i}",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": f"edit {i}",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": f"$edit{i}:example.com",
            "sender": "@user:example.com",
        }
        edit_events.append(edit_event)

    # Mock the routing method
    with patch.object(bot, "_handle_ai_routing", new_callable=AsyncMock) as mock_routing:
        # Process all edit events
        for edit_event in edit_events:
            await bot._on_message(room, edit_event)

        # None of the edits should have triggered routing
        # This will FAIL with current code (it will be called 3 times)
        assert not mock_routing.called, (
            f"Router should not process any edit events, but was called {mock_routing.call_count} times"
        )


@pytest.mark.asyncio
async def test_regular_agent_ignores_edits(tmp_path: Path) -> None:
    """Test that regular agents also ignore edit events."""
    # Create a mock agent user for a regular agent
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

    # Mock the client and dependencies
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create an edit event with a mention of the agent
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent help me",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent help me",
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
            "body": "* @test_agent help me",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent help me",
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

    # Mock the generate_response method
    with patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate:
        # Process the edit event
        await bot._on_message(room, edit_event)

        # The agent should NOT have attempted to generate a response
        # This will FAIL with current code and PASS once fixed
        assert not mock_generate.called, "Agent should NOT respond to edit events"
