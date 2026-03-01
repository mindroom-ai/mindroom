"""Test that user_id is passed through to agent.arun() for Agno learning."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.metrics import Metrics
from agno.run.agent import ModelRequestCompletedEvent, RunCompletedEvent, RunContentEvent
from agno.run.base import RunStatus

from mindroom.ai import ai_response, stream_agent_response
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.openclaw_context import get_openclaw_tool_context
from mindroom.tool_runtime_context import ToolRuntimeContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class TestUserIdPassthrough:
    """Test that user_id reaches agent.arun() in both streaming and non-streaming paths."""

    @pytest.mark.asyncio
    async def test_non_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond passes user_id through to ai_response."""
        config = Config.from_yaml()
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.storage_path = tmp_path
        bot.config = config
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response_id")
        bot._build_tool_runtime_context = MagicMock(
            return_value=ToolRuntimeContext(
                agent_name="general",
                room_id="!test:localhost",
                thread_id=None,
                resolved_thread_id=None,
                requester_id="@alice:localhost",
                client=bot.client,
                config=config,
                storage_path=tmp_path,
            ),
        )

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
        config = Config.from_yaml()
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.matrix_id = MagicMock()
        bot.matrix_id.domain = "localhost"
        bot.config = config
        bot.storage_path = tmp_path
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()
        bot._build_tool_runtime_context = MagicMock(
            return_value=ToolRuntimeContext(
                agent_name="general",
                room_id="!test:localhost",
                thread_id=None,
                resolved_thread_id=None,
                requester_id="@bob:localhost",
                client=bot.client,
                config=config,
                storage_path=tmp_path,
            ),
        )

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
            mock_prepare.return_value = (mock_agent, "test prompt", [])

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
            mock_prepare.return_value = (mock_agent, "test prompt", [])

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
            mock_prepare.return_value = (mock_agent, "test prompt", [])

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

    @pytest.mark.asyncio
    async def test_stream_cache_key_respects_show_tool_calls(self, tmp_path: Path) -> None:
        """Streaming cache key should include tool-call visibility mode."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False
        mock_agent.arun = MagicMock()

        mock_cache = MagicMock()
        mock_cache.get.return_value = MagicMock(content="cached-response")

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=mock_cache),
            patch("mindroom.ai._build_cache_key", return_value="cache-key") as mock_build_cache_key,
        ):
            mock_prepare.return_value = (mock_agent, "test prompt", [])

            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    storage_path=tmp_path,
                    config=Config.from_yaml(),
                    show_tool_calls=False,
                )
            ]

        assert chunks == ["cached-response"]
        assert mock_build_cache_key.call_args.kwargs["show_tool_calls"] is False

    @pytest.mark.asyncio
    async def test_ai_response_collects_tool_trace_when_tool_calls_hidden(self, tmp_path: Path) -> None:
        """Non-streaming path should still surface structured tool metadata."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        tool = MagicMock()
        tool.tool_name = "read_file"
        tool.tool_args = {"path": "README.md"}
        tool.result = "ok"

        mock_run_output = MagicMock()
        mock_run_output.content = "Done."
        mock_run_output.tools = [tool]
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt", [])
            tool_trace: list[object] = []
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                storage_path=tmp_path,
                config=Config.from_yaml(),
                show_tool_calls=False,
                tool_trace_collector=tool_trace,
            )

        assert response == "Done."
        assert "<tool>" not in response
        assert len(tool_trace) == 1

    @pytest.mark.asyncio
    async def test_ai_response_collects_run_metadata(self, tmp_path: Path) -> None:
        """Non-streaming path should expose model/token/context metadata."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = []
        mock_run_output.run_id = "run-1"
        mock_run_output.session_id = "session1"
        mock_run_output.status = RunStatus.completed
        mock_run_output.model = "test-model"
        mock_run_output.model_provider = "openai"
        mock_run_output.metrics = Metrics(
            input_tokens=800,
            output_tokens=120,
            total_tokens=920,
            time_to_first_token=0.42,
            duration=1.75,
        )
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=2000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt", [])
            run_metadata: dict[str, object] = {}
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                storage_path=tmp_path,
                config=config,
                run_metadata_collector=run_metadata,
            )

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["version"] == 1
        assert payload["run_id"] == "run-1"
        assert payload["status"] == "completed"
        assert payload["usage"]["input_tokens"] == 800
        assert payload["context"]["input_tokens"] == 800
        assert payload["context"]["window_tokens"] == 2000
        assert "utilization_pct" not in payload["context"]
        assert payload["tools"]["count"] == 0

    @pytest.mark.asyncio
    async def test_stream_agent_response_collects_run_metadata(self, tmp_path: Path) -> None:
        """Streaming path should expose run metadata from completion events."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="hello")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=500,
                output_tokens=60,
                total_tokens=560,
                time_to_first_token=0.33,
            )
            yield RunCompletedEvent(
                run_id="run-2",
                session_id="session1",
                metrics=Metrics(
                    input_tokens=500,
                    output_tokens=60,
                    total_tokens=560,
                    duration=2.4,
                ),
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt", [])
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                storage_path=tmp_path,
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["version"] == 1
        assert payload["run_id"] == "run-2"
        assert payload["usage"]["total_tokens"] == 560
        assert payload["context"]["input_tokens"] == 500
        assert payload["context"]["window_tokens"] == 1000
        assert "utilization_pct" not in payload["context"]

    @pytest.mark.asyncio
    async def test_stream_agent_response_uses_request_metrics_fallback(self, tmp_path: Path) -> None:
        """Streaming metadata should fall back to model request metrics when needed."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="ok")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=12,
                output_tokens=3,
                time_to_first_token=0.12,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=100)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_cache", return_value=None),
        ):
            mock_prepare.return_value = (mock_agent, "test prompt", [])
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                storage_path=tmp_path,
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["status"] == "completed"
        assert payload["usage"]["input_tokens"] == 12
        assert payload["usage"]["output_tokens"] == 3
        assert payload["usage"]["total_tokens"] == 15
        assert payload["usage"]["time_to_first_token"] == format(0.12, ".12g")
        assert payload["context"]["input_tokens"] == 12
        assert payload["context"]["window_tokens"] == 100
        assert "utilization_pct" not in payload["context"]
