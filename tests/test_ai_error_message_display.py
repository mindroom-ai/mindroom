"""Test that AI errors are properly displayed to users in the Matrix room."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.history.types import HistoryScope
from mindroom.hooks import HookRegistry
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.orchestration.runtime import SYNC_RESTART_CANCEL_MSG
from mindroom.response_runner import ResponseRequest
from mindroom.streaming import build_restart_interrupted_body
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    replace_delivery_gateway_deps,
    replace_response_runner_deps,
    resolve_response_thread_root_for_test,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _runtime_bound_config() -> Config:
    """Return a minimal runtime-bound config for bot error-display tests."""
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", rooms=["!room:localhost"])},
        ),
        test_runtime_paths(Path(tempfile.mkdtemp())),
    )


def _mock_bot(tmp_path: Path) -> AgentBot:
    """Create a bot test instance with explicit mocked collaborators."""
    config = _runtime_bound_config()
    bot = AgentBot(
        AgentMatrixUser(
            agent_name="test_agent",
            password=TEST_PASSWORD,
            display_name="Test Agent",
            user_id="@mindroom_test_agent:localhost",
        ),
        tmp_path,
        config,
        runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.logger = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.client.user_id = "@mindroom_test_agent:localhost"
    bot.hook_registry = HookRegistry.empty()
    bot.enable_streaming = True
    bot.orchestrator = None
    bot._conversation_resolver = MagicMock()
    bot._conversation_resolver.build_message_target = MagicMock(
        return_value=MessageTarget.resolve("!room:localhost", None, None, room_mode=True),
    )
    bot._conversation_resolver.resolve_response_thread_root = MagicMock(
        side_effect=resolve_response_thread_root_for_test,
    )
    bot._conversation_state_writer = MagicMock()
    bot._conversation_state_writer.create_storage = MagicMock(return_value=MagicMock())
    bot._conversation_state_writer.persist_response_event_id_in_session_run = MagicMock()
    bot._conversation_state_writer.history_scope = MagicMock(
        return_value=HistoryScope(kind="agent", scope_id=bot.agent_name),
    )
    bot._conversation_state_writer.team_history_scope = MagicMock(
        return_value=HistoryScope(kind="team", scope_id=bot.agent_name),
    )
    bot._conversation_state_writer.session_type_for_scope = MagicMock(return_value=SessionType.AGENT)
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    return bot


def _build_response_runner(bot: AgentBot) -> None:
    """Rebuild extracted collaborators after tests replace bot-facing dependencies."""
    replace_delivery_gateway_deps(
        bot,
        logger=bot.logger,
        resolver=bot._conversation_resolver,
    )
    replace_response_runner_deps(
        bot,
        logger=bot.logger,
        resolver=bot._conversation_resolver,
        knowledge_access=bot._knowledge_access_support,
        state_writer=bot._conversation_state_writer,
    )


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

        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch("mindroom.delivery_gateway.edit_message", new=AsyncMock(side_effect=mock_gateway_edit_message)),
        ):
            _build_response_runner(bot)
            error_msg = "[test_agent] 🔴 Authentication failed. Please check your API key configuration."
            mock_ai.return_value = error_msg

            await bot._response_runner.process_and_respond(
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

        # Mock stream_agent_response to yield an error message
        with patch("mindroom.response_runner.stream_agent_response") as mock_stream:

            async def error_stream() -> AsyncIterator[str]:
                yield "[test_agent] 🔴 Rate limited. Please wait before trying again."

            mock_stream.return_value = error_stream()

            # Mock send_streaming_response to return the accumulated text
            with patch("mindroom.delivery_gateway.send_streaming_response") as mock_send_streaming:
                _build_response_runner(bot)
                error_text = "[test_agent] 🔴 Rate limited. Please wait before trying again."
                mock_send_streaming.return_value = ("$msg_id", error_text)

                # Call the method with an existing_event_id
                await bot._response_runner.process_and_respond_streaming(
                    _response_request(existing_event_id="$thinking_msg"),
                )

                # Verify send_streaming_response was called with the error stream
                mock_send_streaming.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation_shows_cancelled_message(self, tmp_path: Path) -> None:
        """Test that when a response is cancelled, it shows a cancellation message."""
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

        # Mock ai_response to raise CancelledError
        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch("mindroom.delivery_gateway.edit_message", new=AsyncMock(side_effect=mock_gateway_edit_message)),
        ):
            _build_response_runner(bot)
            mock_ai.side_effect = asyncio.CancelledError()

            # Call the method and expect it to raise CancelledError
            with pytest.raises(asyncio.CancelledError):
                await bot._response_runner.process_and_respond(
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
                patch("mindroom.response_runner.ai_response") as mock_ai,
                patch("mindroom.delivery_gateway.edit_message", new=AsyncMock(side_effect=mock_gateway_edit_message)),
            ):
                _build_response_runner(bot)
                mock_ai.return_value = error_msg

                await bot._response_runner.process_and_respond(
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
    async def test_non_streaming_sync_restart_edits_thinking_message_with_restart_status(
        self,
        tmp_path: Path,
    ) -> None:
        """Sync restarts should not render as user-initiated cancellation."""
        bot = _mock_bot(tmp_path)

        edited_messages: list[tuple[str, dict[str, object], str]] = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> str:
            edited_messages.append((event_id, content, text))
            return "$edit"

        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch("mindroom.delivery_gateway.edit_message", new=AsyncMock(side_effect=mock_gateway_edit_message)),
        ):
            _build_response_runner(bot)
            mock_ai.side_effect = asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)

            with pytest.raises(asyncio.CancelledError):
                await bot._response_runner.process_and_respond(
                    _response_request(existing_event_id="$thinking_msg"),
                )

        assert len(edited_messages) == 1
        event_id, content, text = edited_messages[0]
        assert event_id == "$thinking_msg"
        assert text == build_restart_interrupted_body("")
        assert content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR

    @pytest.mark.asyncio
    async def test_knowledge_init_failure_falls_back_to_response_without_knowledge(self, tmp_path: Path) -> None:
        """Matrix reply paths should continue when request-scoped knowledge init fails."""
        bot = _mock_bot(tmp_path)
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)

        with (
            patch(
                "mindroom.response_runner.ensure_request_knowledge_managers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("knowledge init failed"),
            ),
            patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.delivery_gateway.send_message", new=AsyncMock(return_value="$response_id")),
        ):
            _build_response_runner(bot)
            mock_ai.return_value = "Response without knowledge"

            delivery = await bot._response_runner.process_and_respond(
                _response_request(),
            )

        assert delivery.event_id == "$response_id"
        assert mock_ai.call_args.kwargs["knowledge"] is None
        bot.logger.exception.assert_called_once()
