"""Test that AI errors are properly displayed to users in the Matrix room."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config import Config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class TestAIErrorDisplay:
    """Test that AI errors are shown to users properly."""

    @pytest.mark.asyncio
    async def test_non_streaming_error_edits_thinking_message(self, tmp_path: Path) -> None:
        """Test that when AI fails in non-streaming mode, the thinking message is edited with the error."""
        # Create a minimal bot instance
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "test_agent"
        bot.storage_path = tmp_path
        bot.config = Config.from_yaml()

        # Mock the _edit_message method to track what gets edited
        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message

        # Create the actual _process_and_respond method bound to our mock bot
        process_method = AgentBot._process_and_respond

        # Mock ai_response to return an error message
        with patch("mindroom.bot.ai_response") as mock_ai:
            error_msg = "[test_agent] 🔴 Authentication failed. Please check your API key configuration."
            mock_ai.return_value = error_msg

            # Call the method with an existing_event_id (simulating thinking message edit)
            await process_method(
                bot,
                room_id="!test:localhost",
                prompt="Help me with something",
                reply_to_event_id="$user_msg",
                thread_id=None,
                thread_history=[],
                existing_event_id="$thinking_msg",  # This is the thinking message to edit
            )

            # Verify the thinking message was edited with the error
            assert len(edited_messages) == 1
            event_id, text = edited_messages[0]
            assert event_id == "$thinking_msg"
            assert "Authentication failed" in text
            assert "API key" in text

    @pytest.mark.asyncio
    async def test_streaming_error_updates_message(self, tmp_path: Path) -> None:
        """Test that when streaming AI fails, the message is updated with the error."""
        # Create a minimal bot instance
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "test_agent"
        bot.matrix_id = MagicMock()
        bot.matrix_id.domain = "localhost"
        bot.config = Config.from_yaml()
        bot.storage_path = tmp_path

        # Mock the _edit_message method to track what gets edited
        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message
        bot._handle_interactive_question = AsyncMock()

        # Create the actual _process_and_respond_streaming method bound to our mock bot
        streaming_method = AgentBot._process_and_respond_streaming

        # Mock stream_agent_response to yield an error message
        with patch("mindroom.bot.stream_agent_response") as mock_stream:

            async def error_stream() -> AsyncIterator[str]:
                yield "[test_agent] 🔴 Rate limited. Please wait before trying again."

            mock_stream.return_value = error_stream()

            # Mock send_streaming_response to return the accumulated text
            with patch("mindroom.bot.send_streaming_response") as mock_send_streaming:
                error_text = "[test_agent] 🔴 Rate limited. Please wait before trying again."
                mock_send_streaming.return_value = ("$msg_id", error_text)

                # Call the method with an existing_event_id
                await streaming_method(
                    bot,
                    room_id="!test:localhost",
                    prompt="Help me with something",
                    reply_to_event_id="$user_msg",
                    thread_id=None,
                    thread_history=[],
                    existing_event_id="$thinking_msg",
                )

                # Verify send_streaming_response was called with the error stream
                mock_send_streaming.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation_shows_cancelled_message(self, tmp_path: Path) -> None:
        """Test that when a response is cancelled, it shows a cancellation message."""
        # Create a minimal bot instance
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.client = AsyncMock()
        bot.agent_name = "test_agent"
        bot.storage_path = tmp_path
        bot.config = Config.from_yaml()

        # Mock the _edit_message method to track what gets edited
        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message

        # Create the actual _process_and_respond method bound to our mock bot
        process_method = AgentBot._process_and_respond

        # Mock ai_response to raise CancelledError
        with patch("mindroom.bot.ai_response") as mock_ai:
            mock_ai.side_effect = asyncio.CancelledError()

            # Call the method and expect it to raise CancelledError
            with pytest.raises(asyncio.CancelledError):
                await process_method(
                    bot,
                    room_id="!test:localhost",
                    prompt="Help me with something",
                    reply_to_event_id="$user_msg",
                    thread_id=None,
                    thread_history=[],
                    existing_event_id="$thinking_msg",
                )

            # Verify the thinking message was edited with cancellation message
            assert len(edited_messages) == 1
            event_id, text = edited_messages[0]
            assert event_id == "$thinking_msg"
            assert "Response cancelled by user" in text

    @pytest.mark.asyncio
    async def test_various_error_messages_are_user_friendly(self, tmp_path: Path) -> None:
        """Test that various error types result in user-friendly messages."""
        # Create a minimal bot instance
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "test_agent"
        bot.storage_path = tmp_path
        bot.config = Config.from_yaml()

        # Track edited messages
        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,  # noqa: ARG001
            text: str,
            thread_id: str | None,  # noqa: ARG001
        ) -> None:
            edited_messages.append(text)

        bot._edit_message = mock_edit_message
        bot._send_response = AsyncMock(return_value="$response_id")

        # Create the actual _process_and_respond method bound to our mock bot
        process_method = AgentBot._process_and_respond

        # Test various error messages
        error_messages = [
            "[test_agent] 🔴 Authentication failed. Please check your API key configuration.",
            "[test_agent] 🔴 Rate limited. Please wait before trying again.",
            "[test_agent] 🔴 Request timed out. Please try again.",
            "[test_agent] 🔴 Service temporarily unavailable. Please try again later.",
            "[test_agent] 🔴 Error: Invalid model specified. Please check your configuration.",
        ]

        for error_msg in error_messages:
            edited_messages.clear()

            with patch("mindroom.bot.ai_response") as mock_ai:
                mock_ai.return_value = error_msg

                await process_method(
                    bot,
                    room_id="!test:localhost",
                    prompt="Help me",
                    reply_to_event_id="$user_msg",
                    thread_id=None,
                    thread_history=[],
                    existing_event_id=f"$thinking_{error_messages.index(error_msg)}",
                )

                # Verify the error message was shown to the user
                assert len(edited_messages) == 1
                displayed_msg = edited_messages[0]

                # Check that key parts of the error are present
                if "Authentication" in error_msg:
                    assert "Authentication" in displayed_msg
                elif "Rate limited" in error_msg:
                    assert "Rate limited" in displayed_msg
                elif "timed out" in error_msg:
                    assert "timed out" in displayed_msg
                elif "unavailable" in error_msg:
                    assert "unavailable" in displayed_msg
                elif "Invalid model" in error_msg:
                    assert "Invalid model" in displayed_msg
