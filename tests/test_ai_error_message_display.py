"""Test that AI errors are properly displayed to users in the Matrix room."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def _make_response_bot(tmp_path: Path) -> MagicMock:
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.agent_name = "test_agent"
    bot.storage_path = tmp_path
    bot.config = Config.from_yaml()
    bot._knowledge_for_agent = MagicMock(return_value=None)
    bot._build_tool_runtime_context = MagicMock(return_value=None)
    bot._append_matrix_prompt_context = MagicMock(side_effect=lambda prompt, **_kwargs: prompt)
    bot._agent_has_matrix_messaging_tool = MagicMock(return_value=False)
    bot.typing_indicator = _noop_typing_indicator
    bot.show_tool_calls = False
    return bot


class TestAIErrorDisplay:
    """Test that AI errors are shown to users properly."""

    @pytest.mark.asyncio
    async def test_non_streaming_error_edits_thinking_message(self, tmp_path: Path) -> None:
        """Test that when AI fails in non-streaming mode, the thinking message is edited with the error."""
        bot = _make_response_bot(tmp_path)
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()

        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
            tool_trace: object | None = None,  # noqa: ARG001
            extra_content: object | None = None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message

        process_method = AgentBot._process_and_respond
        error_msg = "[test_agent] 🔴 Authentication failed. Please check your API key configuration."
        bot.ai_response = AsyncMock(return_value=error_msg)

        await process_method(
            bot,
            room_id="!test:localhost",
            prompt="Help me with something",
            reply_to_event_id="$user_msg",
            thread_id=None,
            thread_history=[],
            existing_event_id="$thinking_msg",
        )

        assert len(edited_messages) == 1
        event_id, text = edited_messages[0]
        assert event_id == "$thinking_msg"
        assert "Authentication failed" in text
        assert "API key" in text

    @pytest.mark.asyncio
    async def test_streaming_error_updates_message(self, tmp_path: Path) -> None:
        """Test that when streaming AI fails, the message is updated with the error."""
        bot = _make_response_bot(tmp_path)
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.matrix_id = MagicMock()
        bot.matrix_id.domain = "localhost"

        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
            extra_content: object | None = None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message
        bot._handle_interactive_question = AsyncMock()

        streaming_method = AgentBot._process_and_respond_streaming

        async def error_stream() -> AsyncIterator[str]:
            yield "[test_agent] 🔴 Rate limited. Please wait before trying again."

        error_text = "[test_agent] 🔴 Rate limited. Please wait before trying again."
        bot.stream_agent_response = MagicMock(return_value=error_stream())
        bot.send_streaming_response = AsyncMock(return_value=("$msg_id", error_text))

        await streaming_method(
            bot,
            room_id="!test:localhost",
            prompt="Help me with something",
            reply_to_event_id="$user_msg",
            thread_id=None,
            thread_history=[],
            existing_event_id="$thinking_msg",
        )

        bot.send_streaming_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancellation_shows_cancelled_message(self, tmp_path: Path) -> None:
        """Test that when a response is cancelled, it shows a cancellation message."""
        bot = _make_response_bot(tmp_path)

        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
            extra_content: object | None = None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message

        process_method = AgentBot._process_and_respond
        bot.ai_response = AsyncMock(side_effect=asyncio.CancelledError())

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

        assert len(edited_messages) == 1
        event_id, text = edited_messages[0]
        assert event_id == "$thinking_msg"
        assert "Response cancelled by user" in text

    @pytest.mark.asyncio
    async def test_various_error_messages_are_user_friendly(self, tmp_path: Path) -> None:
        """Test that various error types result in user-friendly messages."""
        bot = _make_response_bot(tmp_path)
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()

        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,  # noqa: ARG001
            text: str,
            thread_id: str | None,  # noqa: ARG001
            tool_trace: object | None = None,  # noqa: ARG001
            extra_content: object | None = None,  # noqa: ARG001
        ) -> None:
            edited_messages.append(text)

        bot._edit_message = mock_edit_message
        bot._send_response = AsyncMock(return_value="$response_id")

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
            bot.ai_response = AsyncMock(return_value=error_msg)

            await process_method(
                bot,
                room_id="!test:localhost",
                prompt="Help me",
                reply_to_event_id="$user_msg",
                thread_id=None,
                thread_history=[],
                existing_event_id=f"$thinking_{error_messages.index(error_msg)}",
            )

            assert len(edited_messages) == 1
            displayed_msg = edited_messages[0]

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
