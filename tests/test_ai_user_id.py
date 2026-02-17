"""Test that user_id is passed through to agent.arun() for Agno learning."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import ai_response, stream_agent_response
from mindroom.bot import AgentBot
from mindroom.config import Config
from mindroom.openclaw_context import get_openclaw_tool_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class TestUserIdPassthrough:
    """Test that user_id reaches agent.arun() in both streaming and non-streaming paths."""

    @pytest.mark.asyncio
    async def test_non_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond passes user_id through to ai_response."""
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.storage_path = tmp_path
        bot.config = Config.from_yaml()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response_id")
        bot._build_openclaw_tool_context = AgentBot._build_openclaw_tool_context.__get__(bot, AgentBot)

        process_method = AgentBot._process_and_respond

        with patch("mindroom.bot.ai_response") as mock_ai:

            async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
                context = get_openclaw_tool_context()
                assert context is not None
                assert context.room_id == "!test:localhost"
                assert context.thread_id is None
                assert context.requester_id == "@alice:localhost"
                return "Hello!"

            mock_ai.side_effect = fake_ai_response

            await process_method(
                bot,
                room_id="!test:localhost",
                prompt="Hello",
                reply_to_event_id="$user_msg",
                thread_id=None,
                thread_history=[],
                user_id="@alice:localhost",
            )

            mock_ai.assert_called_once()
            assert mock_ai.call_args.kwargs["user_id"] == "@alice:localhost"

    @pytest.mark.asyncio
    async def test_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond_streaming passes user_id through to stream_agent_response."""
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.matrix_id = MagicMock()
        bot.matrix_id.domain = "localhost"
        bot.config = Config.from_yaml()
        bot.storage_path = tmp_path
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()
        bot._build_openclaw_tool_context = AgentBot._build_openclaw_tool_context.__get__(bot, AgentBot)

        streaming_method = AgentBot._process_and_respond_streaming

        with patch("mindroom.bot.stream_agent_response") as mock_stream:

            def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
                context = get_openclaw_tool_context()
                assert context is not None
                assert context.room_id == "!test:localhost"
                assert context.thread_id is None
                assert context.requester_id == "@bob:localhost"

                async def fake_stream() -> AsyncIterator[str]:
                    yield "Hello!"

                return fake_stream()

            mock_stream.side_effect = fake_stream_agent_response

            with patch("mindroom.bot.send_streaming_response") as mock_send_streaming:
                mock_send_streaming.return_value = ("$msg_id", "Hello!")

                await streaming_method(
                    bot,
                    room_id="!test:localhost",
                    prompt="Hello",
                    reply_to_event_id="$user_msg",
                    thread_id=None,
                    thread_history=[],
                    user_id="@bob:localhost",
                )

                mock_stream.assert_called_once()
                assert mock_stream.call_args.kwargs["user_id"] == "@bob:localhost"

    @pytest.mark.asyncio
    async def test_ai_response_passes_user_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Test that ai_response passes user_id all the way to agent.arun()."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt")

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                storage_path=tmp_path,
                config=Config.from_yaml(),
                user_id="@user:localhost",
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] == "@user:localhost"

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_user_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Test that stream_agent_response passes user_id all the way to agent.arun()."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            yield "chunk"

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt")

            # Consume the async generator to trigger the agent.arun call.
            _chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    storage_path=tmp_path,
                    config=Config.from_yaml(),
                    user_id="@user:localhost",
                )
            ]

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] == "@user:localhost"

    @pytest.mark.asyncio
    async def test_user_id_none_when_not_provided(self, tmp_path: Path) -> None:
        """Test that user_id defaults to None when not provided (backward compatibility)."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt")

            # Call without user_id
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                storage_path=tmp_path,
                config=Config.from_yaml(),
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] is None
