"""Comprehensive unit tests for streaming behavior with agent edits."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker
from mindroom.streaming import StreamingResponse
from mindroom.thread_invites import ThreadInviteManager


@pytest.fixture
def mock_helper_agent() -> AgentMatrixUser:
    """Create a mock helper agent user."""
    return AgentMatrixUser(
        agent_name="helper",
        password="test_password",
        display_name="HelperAgent",
        user_id="@mindroom_helper:localhost",
    )


@pytest.fixture
def mock_calculator_agent() -> AgentMatrixUser:
    """Create a mock calculator agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password="test_password",
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


class TestStreamingBehavior:
    """Test the complete streaming behavior including agent interactions."""

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.ai_response_streaming")
    async def test_streaming_agent_mentions_another_agent(
        self,
        mock_ai_response_streaming: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_helper_agent: AgentMatrixUser,
        mock_calculator_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test complete flow of one agent streaming and mentioning another."""
        # Set up helper bot (the one that will stream)
        helper_bot = AgentBot(mock_helper_agent, tmp_path, rooms=["!test:localhost"], enable_streaming=True)
        helper_bot.client = AsyncMock()
        helper_bot.response_tracker = ResponseTracker(helper_bot.agent_name, base_path=tmp_path)
        helper_bot.thread_invite_manager = ThreadInviteManager(helper_bot.client)

        # Set up calculator bot (the one that will be mentioned)
        calc_bot = AgentBot(mock_calculator_agent, tmp_path, rooms=["!test:localhost"], enable_streaming=False)
        calc_bot.client = AsyncMock()
        calc_bot.response_tracker = ResponseTracker(calc_bot.agent_name, base_path=tmp_path)
        calc_bot.thread_invite_manager = ThreadInviteManager(calc_bot.client)

        # Mock successful room_send responses
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$helper_response_123"
        helper_bot.client.room_send.return_value = mock_send_response
        calc_bot.client.room_send.return_value = mock_send_response

        # Mock AI responses
        mock_ai_response.return_value = "4"

        # Create a generator that yields the streaming response
        async def streaming_generator():
            yield "Let me help with that calculation. "
            yield "@mindroom_calculator:localhost what's 2+2?"

        mock_ai_response_streaming.return_value = streaming_generator()

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # User asks helper for help
        user_event = MagicMock()
        user_event.sender = "@user:localhost"
        user_event.body = "@mindroom_helper:localhost can you help me with math?"
        user_event.event_id = "$user_msg_123"
        user_event.source = {
            "content": {
                "body": "@mindroom_helper:localhost can you help me with math?",
                "m.mentions": {"user_ids": ["@mindroom_helper:localhost"]},
            }
        }

        # Mock that we're mentioned
        with patch("mindroom.bot.check_agent_mentioned") as mock_check:
            mock_check.return_value = (["helper"], True)

            # Process message with helper bot - it should stream a response
            await helper_bot._on_message(mock_room, user_event)

        # Verify helper bot sent initial message and edit
        assert helper_bot.client.room_send.call_count >= 1  # At least initial message

        # Simulate the streaming edit that mentions calculator
        # This would be seen by calculator bot as an edit event
        edit_event = MagicMock()
        edit_event.sender = "@mindroom_helper:localhost"
        edit_event.body = "* Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?"
        edit_event.event_id = "$helper_edit_123"
        edit_event.source = {
            "content": {
                "body": "* Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$helper_response_123",
                },
                "m.new_content": {
                    "body": "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            }
        }

        # Process edit with calculator bot - it should NOT respond
        await calc_bot._on_message(mock_room, edit_event)
        assert calc_bot.client.room_send.call_count == 0
        assert mock_ai_response.call_count == 0  # Calculator didn't process anything

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    async def test_agent_responds_only_to_final_message(
        self,
        mock_ai_response: AsyncMock,
        mock_helper_agent: AgentMatrixUser,
        mock_calculator_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents respond to the final complete message, not edits."""
        # Set up calculator bot
        calc_bot = AgentBot(mock_calculator_agent, tmp_path, rooms=["!test:localhost"], enable_streaming=False)
        calc_bot.client = AsyncMock()
        calc_bot.response_tracker = ResponseTracker(calc_bot.agent_name, base_path=tmp_path)
        calc_bot.thread_invite_manager = ThreadInviteManager(calc_bot.client)

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        calc_bot.client.room_send.return_value = mock_send_response

        # Mock AI response
        mock_ai_response.return_value = "4"

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Helper sends initial complete message mentioning calculator
        initial_event = MagicMock()
        initial_event.sender = "@mindroom_helper:localhost"
        initial_event.body = "Hey @mindroom_calculator:localhost, what's 2+2?"
        initial_event.event_id = "$helper_msg_123"
        initial_event.source = {
            "content": {
                "body": "Hey @mindroom_calculator:localhost, what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            }
        }

        # Process initial message - calculator SHOULD respond
        await calc_bot._on_message(mock_room, initial_event)
        assert calc_bot.client.room_send.call_count == 1
        assert mock_ai_response.call_count == 1

        # Reset mocks
        calc_bot.client.room_send.reset_mock()
        mock_ai_response.reset_mock()

        # Helper edits to add more context (simulating streaming)
        edit_event = MagicMock()
        edit_event.sender = "@mindroom_helper:localhost"
        edit_event.body = "* Hey @mindroom_calculator:localhost, what's 2+2? I need this for a calculation."
        edit_event.event_id = "$helper_edit_456"
        edit_event.source = {
            "content": {
                "body": "* Hey @mindroom_calculator:localhost, what's 2+2? I need this for a calculation.",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$helper_msg_123",
                },
            }
        }

        # Process edit - calculator should NOT respond again
        await calc_bot._on_message(mock_room, edit_event)
        assert calc_bot.client.room_send.call_count == 0
        assert mock_ai_response.call_count == 0

    @pytest.mark.asyncio
    async def test_streaming_response_flow(
        self,
        mock_helper_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test the StreamingResponse class behavior."""
        # Create a mock client
        mock_client = AsyncMock()
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$stream_123"
        mock_client.room_send.return_value = mock_send_response

        # Create streaming response
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
        )

        # Simulate streaming chunks
        await streaming.update_content("Hello ", mock_client)
        assert streaming.accumulated_text == "Hello "

        # Should send initial message
        assert mock_client.room_send.call_count == 1
        assert streaming.event_id == "$stream_123"

        # Add more content immediately (should not trigger update yet)
        await streaming.update_content("world", mock_client)
        assert streaming.accumulated_text == "Hello world"
        # Should NOT send edit because not enough time has passed
        assert mock_client.room_send.call_count == 1

        # Simulate time passing
        await asyncio.sleep(0.11)  # Wait more than update_interval

        # Add more content after delay
        await streaming.update_content("!", mock_client)
        assert streaming.accumulated_text == "Hello world!"
        # NOW it should send an edit
        assert mock_client.room_send.call_count == 2

        # Force finalize
        await streaming.finalize(mock_client)
        # Should send final edit (might be same as previous if no new content)
        assert mock_client.room_send.call_count >= 2

        # Check the edit content
        last_call = mock_client.room_send.call_args_list[-1]
        content = last_call[1]["content"]
        assert content["m.relates_to"]["rel_type"] == "m.replace"
        assert content["m.relates_to"]["event_id"] == "$stream_123"
