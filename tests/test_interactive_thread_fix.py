"""Regression test for interactive question thread reference bug.

This test ensures that when an agent sends an interactive message with reaction options,
and a user reacts to it, the subsequent acknowledgment and response stay in the original
thread instead of creating a new thread rooted at the agent's message.

Bug: Interactive questions were being registered with the wrong thread_id (the agent's
message ID instead of the original user message ID), causing reactions to create new
threads instead of continuing the existing conversation.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import Config
from mindroom.matrix.users import AgentMatrixUser


@pytest.mark.asyncio
async def test_interactive_question_preserves_thread_root_in_streaming(tmp_path: Path) -> None:
    """Test that streaming responses register interactive questions with correct thread root."""
    # This test verifies that when the bot sends an interactive message in a thread,
    # it registers the interactive question with the original thread_id, not the agent's message ID

    with (
        patch("mindroom.bot.stream_agent_response") as mock_ai_response,
        patch("mindroom.bot.interactive.should_create_interactive_question") as mock_should_create,
        patch("mindroom.bot.interactive.parse_and_format_interactive") as mock_parse,
        patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
        patch("mindroom.bot.interactive.add_reaction_buttons"),
        patch("mindroom.streaming.ReplacementStreamingResponse") as mock_streaming_class,
    ):
        # Setup mock streaming response
        mock_streaming = MagicMock()
        mock_streaming.event_id = "$agent_message_id"
        mock_streaming.accumulated_text = "Test interactive response"
        # Make methods awaitable to match production async API
        mock_streaming.update_content = AsyncMock()
        mock_streaming.finalize = AsyncMock()
        mock_streaming_class.return_value = mock_streaming

        # Setup mocks
        async def mock_stream() -> AsyncIterator[str]:
            yield "Test interactive response"

        mock_ai_response.return_value = mock_stream()
        mock_should_create.return_value = True

        mock_response = MagicMock()
        mock_response.formatted_text = "Test interactive question"
        mock_response.option_map = {"1": "option1", "2": "option2"}
        mock_response.options_list = [{"emoji": "1", "label": "Option 1"}, {"emoji": "2", "label": "Option 2"}]
        mock_parse.return_value = mock_response

        # Create bot
        config = Config.from_yaml()
        agent_user = AgentMatrixUser(
            agent_name="test",
            user_id="@mindroom_test:localhost",
            display_name="TestAgent",
            password="test_password",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:localhost"],
        )

        # Mock client
        client = AsyncMock()
        client.user_id = "@mindroom_test:localhost"
        # Mock room_send to return a proper response
        mock_send_response = MagicMock(spec=nio.RoomSendResponse)
        mock_send_response.event_id = "$agent_message_id"
        client.room_send.return_value = mock_send_response
        bot.client = client

        # Setup room and thread context
        room_id = "!test:localhost"

        user_message_id = "$user_original_message"
        thread_id = user_message_id  # The original thread root

        # Call the streaming response handler
        await bot._process_and_respond_streaming(
            room_id,
            prompt="Test prompt",
            reply_to_event_id=user_message_id,
            thread_id=thread_id,
            thread_history=[],
        )

        # Verify that register_interactive_question was called with the correct thread_id
        mock_register.assert_called_once()
        call_args = mock_register.call_args[0]

        registered_event_id = call_args[0]  # First arg is the agent's message event_id
        registered_room_id = call_args[1]  # Second arg is room_id
        registered_thread_id = call_args[2]  # Third arg is thread_id

        # The critical assertion: thread_id should be the original user message, NOT the agent's message
        assert registered_event_id == "$agent_message_id", "Event ID should be the agent's message"
        assert registered_room_id == "!test:localhost", "Room ID should match"
        assert registered_thread_id == user_message_id, (
            f"Thread ID should be the original user message {user_message_id}, "
            f"not the agent's message {registered_event_id}. "
            f"Got: {registered_thread_id}"
        )


@pytest.mark.asyncio
async def test_interactive_question_preserves_thread_root_in_non_streaming(tmp_path: Path) -> None:
    """Test that non-streaming responses register interactive questions with correct thread root."""
    with (
        patch("mindroom.bot.ai_response") as mock_ai_response,
        patch("mindroom.bot.interactive.parse_and_format_interactive") as mock_parse,
        patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
        patch("mindroom.bot.interactive.add_reaction_buttons"),
    ):
        # Setup mocks
        mock_ai_response.return_value = "Test interactive response"

        # Mock the parse_and_format_interactive to return an interactive response
        mock_response_with_interactive = MagicMock()
        mock_response_with_interactive.formatted_text = "Test interactive question"
        mock_response_with_interactive.option_map = {"A": "optionA", "B": "optionB"}
        mock_response_with_interactive.options_list = [
            {"emoji": "A", "label": "Option A"},
            {"emoji": "B", "label": "Option B"},
        ]

        # Always return the interactive response
        mock_parse.return_value = mock_response_with_interactive

        # Create bot
        config = Config.from_yaml()
        agent_user = AgentMatrixUser(
            agent_name="test",
            user_id="@mindroom_test:localhost",
            display_name="TestAgent",
            password="test_password",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:localhost"],
        )

        # Mock client and make sure room_send returns a proper response
        client = AsyncMock()
        client.user_id = "@mindroom_test:localhost"

        # Mock a successful send response with event_id
        mock_send_response = MagicMock()
        mock_send_response.event_id = "$agent_response_id"
        # Need to mock the class check
        mock_send_response.__class__ = nio.RoomSendResponse

        client.room_send.return_value = mock_send_response
        bot.client = client

        # Setup room and thread context

        room_id = "!test:localhost"

        user_message_id = "$user_thread_start"
        thread_id = user_message_id  # The original thread root

        # Call the non-streaming response handler
        await bot._process_and_respond(
            room_id=room_id,
            prompt="Test prompt",
            reply_to_event_id=user_message_id,
            thread_id=thread_id,
            thread_history=[],
        )

        # Verify that register_interactive_question was called with the correct thread_id
        mock_register.assert_called_once()
        call_args = mock_register.call_args[0]

        registered_event_id = call_args[0]  # First arg is the agent's message event_id
        registered_room_id = call_args[1]  # Second arg is room_id
        registered_thread_id = call_args[2]  # Third arg is thread_id

        # The critical assertion: thread_id should be the original user message, NOT the agent's message
        assert registered_event_id == "$agent_response_id", "Event ID should be the agent's message"
        assert registered_room_id == "!test:localhost", "Room ID should match"
        assert registered_thread_id == user_message_id, (
            f"Thread ID should be the original user message {user_message_id}, "
            f"not the agent's message {registered_event_id}. "
            f"Got: {registered_thread_id}"
        )


@pytest.mark.asyncio
async def test_interactive_question_without_thread_streaming(tmp_path: Path) -> None:
    """Test that interactive questions work correctly when not in a thread (streaming)."""
    with (
        patch("mindroom.bot.stream_agent_response") as mock_ai_response,
        patch("mindroom.bot.interactive.should_create_interactive_question") as mock_should_create,
        patch("mindroom.bot.interactive.parse_and_format_interactive") as mock_parse,
        patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
        patch("mindroom.bot.interactive.add_reaction_buttons"),
        patch("mindroom.streaming.ReplacementStreamingResponse") as mock_streaming_class,
    ):
        # Setup mock streaming response
        mock_streaming = MagicMock()
        mock_streaming.event_id = "$standalone_message"
        mock_streaming.accumulated_text = "Test interactive response"
        # Make methods awaitable to match production async API
        mock_streaming.update_content = AsyncMock()
        mock_streaming.finalize = AsyncMock()
        mock_streaming_class.return_value = mock_streaming

        # Setup mocks
        async def mock_stream() -> AsyncIterator[str]:
            yield "Test interactive response"

        mock_ai_response.return_value = mock_stream()
        mock_should_create.return_value = True

        mock_response = MagicMock()
        mock_response.formatted_text = "Test interactive question"
        mock_response.option_map = {"✓": "yes", "✗": "no"}
        mock_response.options_list = [{"emoji": "✓", "label": "Yes"}, {"emoji": "✗", "label": "No"}]
        mock_parse.return_value = mock_response

        # Create bot
        config = Config.from_yaml()
        agent_user = AgentMatrixUser(
            agent_name="test",
            user_id="@mindroom_test:localhost",
            display_name="TestAgent",
            password="test_password",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:localhost"],
        )

        # Mock client
        client = AsyncMock()
        client.user_id = "@mindroom_test:localhost"
        # Mock room_send to return a proper response
        mock_send_response = MagicMock(spec=nio.RoomSendResponse)
        mock_send_response.event_id = "$standalone_message"
        client.room_send.return_value = mock_send_response
        bot.client = client

        # Setup room without thread context
        room_id = "!test:localhost"

        # Call without thread_id
        await bot._process_and_respond_streaming(
            room_id=room_id,
            prompt="Test prompt",
            reply_to_event_id="$some_message",
            thread_id=None,  # No thread
            thread_history=[],
        )

        # Verify that register_interactive_question was called
        mock_register.assert_called_once()
        call_args = mock_register.call_args[0]

        registered_event_id = call_args[0]
        registered_thread_id = call_args[2]

        # When not in a thread, WITHOUT THE FIX the thread_id would be None
        # WITH THE FIX it should be the agent's message itself
        assert registered_event_id == "$standalone_message"
        # This assertion will FAIL without the fix because thread_id will be None
        assert registered_thread_id is not None, (
            "When not in a thread, thread_id should not be None. "
            "It should be the agent's message ID for proper thread creation."
        )
