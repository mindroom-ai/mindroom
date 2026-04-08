"""Test that AI errors are properly displayed to users in the Matrix room."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.hooks import HookRegistry
from mindroom.message_target import MessageTarget
from mindroom.response_coordinator import ResponseRequest
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _runtime_bound_config() -> Config:
    """Return a minimal runtime-bound config for bot error-display tests."""
    return bind_runtime_paths(Config(), test_runtime_paths(Path(tempfile.mkdtemp())))


def _mock_bot(tmp_path: Path) -> MagicMock:
    """Create a bot double with explicit runtime state."""
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "test_agent"
    bot.storage_path = tmp_path
    bot.config = _runtime_bound_config()
    bot.runtime_paths = runtime_paths_for(bot.config)
    bot.matrix_id = MagicMock(full_id="@mindroom_test_agent:localhost", domain="localhost")
    bot.hook_registry = HookRegistry.empty()
    bot.show_tool_calls = True
    bot._agent_has_matrix_messaging_tool = MagicMock(return_value=False)
    bot._append_matrix_prompt_context = MagicMock(side_effect=lambda prompt, **_kwargs: prompt)
    bot._build_tool_runtime_context = MagicMock(return_value=None)
    bot._build_tool_execution_identity = MagicMock(return_value=None)
    bot._build_message_target = MagicMock(
        return_value=MessageTarget.resolve("!room:localhost", None, None, room_mode=True),
    )
    bot._request_with_resolved_thread_target = AgentBot._request_with_resolved_thread_target.__get__(
        bot,
        AgentBot,
    )
    bot._run_in_tool_context = AgentBot._run_in_tool_context.__get__(bot, AgentBot)
    bot._stream_in_tool_context = AgentBot._stream_in_tool_context.__get__(bot, AgentBot)
    bot._delivery_gateway = AgentBot._delivery_gateway.__get__(bot, AgentBot)
    bot._response_coordinator = AgentBot._response_coordinator.__get__(bot, AgentBot)
    bot._apply_before_response_hooks = AgentBot._apply_before_response_hooks.__get__(bot, AgentBot)
    bot._emit_after_response_hooks = AgentBot._emit_after_response_hooks.__get__(bot, AgentBot)
    bot._deliver_generated_response = AgentBot._deliver_generated_response.__get__(bot, AgentBot)
    bot._ensure_request_knowledge_managers = AsyncMock(return_value={})
    return bot


def _response_request(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$user_msg",
    thread_id: str | None = None,
    prompt: str = "Help me with something",
    existing_event_id: str | None = None,
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        thread_history=(),
        prompt=prompt,
        existing_event_id=existing_event_id,
    )


class TestAIErrorDisplay:
    """Test that AI errors are shown to users properly."""

    @pytest.mark.asyncio
    async def test_non_streaming_error_edits_thinking_message(self, tmp_path: Path) -> None:
        """Test that when AI fails in non-streaming mode, the thinking message is edited with the error."""
        bot = _mock_bot(tmp_path)

        edited_messages = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],  # noqa: ARG001
            text: str,
        ) -> str:
            edited_messages.append((event_id, text))
            return "$edit"

        process_method = AgentBot._process_and_respond

        with (
            patch("mindroom.bot.ai_response") as mock_ai,
            patch("mindroom.bot.edit_message", new=AsyncMock(side_effect=mock_gateway_edit_message)),
        ):
            error_msg = "[test_agent] 🔴 Authentication failed. Please check your API key configuration."
            mock_ai.return_value = error_msg

            await process_method(
                bot,
                _response_request(existing_event_id="$thinking_msg"),
            )

            assert len(edited_messages) == 1
            event_id, text = edited_messages[0]
            assert event_id == "$thinking_msg"
            assert "Authentication failed" in text
            assert "API key" in text

    @pytest.mark.asyncio
    async def test_streaming_error_updates_message(self, tmp_path: Path) -> None:
        """Test that when streaming AI fails, the message is updated with the error."""
        bot = _mock_bot(tmp_path)
        bot.matrix_id = MagicMock()
        bot.matrix_id.domain = "localhost"

        # Mock the _edit_message method to track what gets edited
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
                    _response_request(existing_event_id="$thinking_msg"),
                )

                # Verify send_streaming_response was called with the error stream
                mock_send_streaming.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation_shows_cancelled_message(self, tmp_path: Path) -> None:
        """Test that when a response is cancelled, it shows a cancellation message."""
        bot = _mock_bot(tmp_path)

        # Mock the _edit_message method to track what gets edited
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

        # Create the actual _process_and_respond method bound to our mock bot
        process_method = AgentBot._process_and_respond

        # Mock ai_response to raise CancelledError
        with patch("mindroom.bot.ai_response") as mock_ai:
            mock_ai.side_effect = asyncio.CancelledError()

            # Call the method and expect it to raise CancelledError
            with pytest.raises(asyncio.CancelledError):
                await process_method(
                    bot,
                    _response_request(existing_event_id="$thinking_msg"),
                )

            # Verify the thinking message was edited with cancellation message
            assert len(edited_messages) == 1
            event_id, text = edited_messages[0]
            assert event_id == "$thinking_msg"
            assert "Response cancelled by user" in text

    @pytest.mark.asyncio
    async def test_various_error_messages_are_user_friendly(self, tmp_path: Path) -> None:
        """Test that various error types result in user-friendly messages."""
        bot = _mock_bot(tmp_path)

        edited_messages = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,  # noqa: ARG001
            content: dict[str, object],  # noqa: ARG001
            text: str,
        ) -> str:
            edited_messages.append(text)
            return "$edit"

        process_method = AgentBot._process_and_respond

        error_messages = [
            "[test_agent] 🔴 Authentication failed. Please check your API key configuration.",
            "[test_agent] 🔴 Rate limited. Please wait before trying again.",
            "[test_agent] 🔴 Request timed out. Please try again.",
            "[test_agent] 🔴 Service temporarily unavailable. Please try again later.",
            "[test_agent] 🔴 Error: Invalid model specified. Please check your configuration.",
        ]

        for error_msg in error_messages:
            edited_messages.clear()

            with (
                patch("mindroom.bot.ai_response") as mock_ai,
                patch("mindroom.bot.edit_message", new=AsyncMock(side_effect=mock_gateway_edit_message)),
            ):
                mock_ai.return_value = error_msg

                await process_method(
                    bot,
                    _response_request(
                        prompt="Help me",
                        existing_event_id=f"$thinking_{error_messages.index(error_msg)}",
                    ),
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

    @pytest.mark.asyncio
    async def test_knowledge_init_failure_falls_back_to_response_without_knowledge(self, tmp_path: Path) -> None:
        """Matrix reply paths should continue when request-scoped knowledge init fails."""
        bot = _mock_bot(tmp_path)
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._ensure_request_knowledge_managers = AgentBot._ensure_request_knowledge_managers.__get__(bot, AgentBot)

        process_method = AgentBot._process_and_respond

        with (
            patch(
                "mindroom.bot.ensure_request_knowledge_managers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("knowledge init failed"),
            ),
            patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.bot.send_message", new=AsyncMock(return_value="$response_id")),
        ):
            mock_ai.return_value = "Response without knowledge"

            delivery = await process_method(
                bot,
                _response_request(),
            )

        assert delivery.event_id == "$response_id"
        assert mock_ai.call_args.kwargs["knowledge"] is None
        bot.logger.exception.assert_called_once()
