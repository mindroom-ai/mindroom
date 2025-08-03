"""Test streaming edit handling to prevent duplicate responses."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_invites import ThreadInviteManager


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Create a mock agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password="test_password",
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


class TestStreamingEdits:
    """Test that streaming edits don't trigger duplicate responses."""

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.ai_response_streaming")
    async def test_agent_ignores_edits_of_responded_messages(
        self,
        mock_ai_response_streaming: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents don't respond to edits of messages they already responded to."""
        # Set up bot
        bot = AgentBot(mock_agent_user, tmp_path, rooms=["!test:localhost"], enable_streaming=False)
        bot.client = AsyncMock()

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Initialize response tracker and thread manager
        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)

        # Mock AI response
        mock_ai_response.return_value = "I can help with that!"

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Initial message mentioning the agent
        initial_event = MagicMock()
        initial_event.sender = "@user:localhost"
        initial_event.body = "@mindroom_calculator:localhost: What's 2+2?"
        initial_event.event_id = "$initial123"
        initial_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            }
        }

        # Process initial message - bot should respond
        await bot._on_message(mock_room, initial_event)
        assert bot.client.room_send.call_count == 1
        assert mock_ai_response.call_count == 1

        # Reset mocks
        bot.client.room_send.reset_mock()
        mock_ai_response.reset_mock()

        # Edit event 1 - simulating streaming update
        edit_event1 = MagicMock()
        edit_event1.sender = "@user:localhost"
        edit_event1.body = "* @mindroom_calculator:localhost: What's 2+2? Can you show the work?"
        edit_event1.event_id = "$edit1"
        edit_event1.source = {
            "content": {
                "body": "* @mindroom_calculator:localhost: What's 2+2? Can you show the work?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$initial123",  # References the original message
                },
                "m.new_content": {
                    "body": "@mindroom_calculator:localhost: What's 2+2? Can you show the work?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            }
        }

        # Process edit - bot should NOT respond again
        await bot._on_message(mock_room, edit_event1)
        assert bot.client.room_send.call_count == 0
        assert mock_ai_response.call_count == 0

        # Edit event 2 - another streaming update
        edit_event2 = MagicMock()
        edit_event2.sender = "@user:localhost"
        edit_event2.body = "* @mindroom_calculator:localhost: What's 2+2? Can you show the work step by step?"
        edit_event2.event_id = "$edit2"
        edit_event2.source = {
            "content": {
                "body": "* @mindroom_calculator:localhost: What's 2+2? Can you show the work step by step?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$initial123",  # Still references the original
                },
                "m.new_content": {
                    "body": "@mindroom_calculator:localhost: What's 2+2? Can you show the work step by step?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            }
        }

        # Process second edit - bot should still NOT respond
        await bot._on_message(mock_room, edit_event2)
        assert bot.client.room_send.call_count == 0
        assert mock_ai_response.call_count == 0

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    async def test_agent_responds_to_new_messages_after_edits(
        self,
        mock_ai_response: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents still respond to new messages after seeing edits."""
        # Set up bot
        bot = AgentBot(mock_agent_user, tmp_path, rooms=["!test:localhost"], enable_streaming=False)
        bot.client = AsyncMock()

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Initialize response tracker and thread manager
        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)

        # Mock AI response
        mock_ai_response.return_value = "Here's the answer!"

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Mark that we already responded to some original message
        bot.response_tracker.mark_responded("$original123")

        # New message (NOT an edit) mentioning the agent
        new_event = MagicMock()
        new_event.sender = "@user:localhost"
        new_event.body = "@mindroom_calculator:localhost: What's 5+5?"
        new_event.event_id = "$new456"
        new_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 5+5?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            }
        }

        # Process new message - bot SHOULD respond
        await bot._on_message(mock_room, new_event)
        assert bot.client.room_send.call_count == 1
        assert mock_ai_response.call_count == 1
