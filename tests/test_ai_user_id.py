"""Test that user_id is passed through to agent.arun() for Agno learning."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.media import File
from agno.models.metrics import Metrics
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.run.agent import (
    ModelRequestCompletedEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
)
from agno.run.base import RunStatus

from mindroom.ai import (
    _prepare_agent_and_prompt,
    ai_response,
    append_inline_media_fallback_prompt,
    build_matrix_run_metadata,
    should_retry_without_inline_media,
    stream_agent_response,
)
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history import PreparedHistoryState
from mindroom.hooks import HookContextSupport, HookRegistry
from mindroom.hooks.registry import HookRegistryState
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsSupport
from mindroom.response_coordinator import ResponseCoordinator, ResponseCoordinatorDeps, ResponseRequest
from mindroom.tool_system.runtime_context import ToolRuntimeSupport, get_tool_runtime_context
from tests.conftest import bind_runtime_paths, resolve_response_thread_root_for_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _runtime_paths(tmp_path: Path, *, config_path: Path | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=config_path or tmp_path / "config.yaml",
        storage_path=tmp_path,
    )


def _config() -> Config:
    return Config(
        agents={"general": AgentConfig(display_name="General")},
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )


def _prepared_prompt_result(
    agent: object,
    *,
    prompt: str = "test prompt",
) -> tuple[object, str, list[str], PreparedHistoryState]:
    return agent, prompt, [], PreparedHistoryState()


def _build_response_coordinator(
    bot: MagicMock,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_path: Path,
    requester_id: str,  # noqa: ARG001
) -> ResponseCoordinator:
    """Build a real response coordinator for one bot-shaped test double."""
    bot.matrix_id = MagicMock(full_id="@mindroom_general:localhost", domain="localhost")
    bot.enable_streaming = True
    bot.show_tool_calls = False
    bot.orchestrator = None
    bot._conversation_resolver = MagicMock()
    bot._conversation_resolver.build_message_target = MagicMock(
        return_value=MessageTarget.resolve("!test:localhost", None, "$user_msg", room_mode=True),
    )
    bot._conversation_resolver.resolve_response_thread_root = MagicMock(
        side_effect=resolve_response_thread_root_for_test,
    )
    bot._conversation_state_writer = MagicMock()
    bot._conversation_state_writer.create_history_scope_storage = MagicMock(return_value=MagicMock())
    bot._conversation_state_writer.create_team_history_storage = MagicMock(return_value=MagicMock())
    bot._conversation_state_writer.persist_response_event_id_in_session_run = MagicMock()
    bot._conversation_state_writer.history_session_type = MagicMock(return_value=SessionType.AGENT)
    bot._request_with_resolved_thread_target = AgentBot._request_with_resolved_thread_target.__get__(
        bot,
        AgentBot,
    )
    bot._edit_message = AsyncMock(return_value=True)
    delivery_gateway = MagicMock()
    delivery_gateway.deliver_final = AsyncMock(
        return_value=MagicMock(
            event_id="$response_id",
            response_text="Hello!",
            delivery_kind="sent",
        ),
    )
    delivery_gateway.deliver_stream = AsyncMock(return_value=("$msg_id", "Hello!"))
    runtime = SimpleNamespace(
        client=bot.client,
        config=config,
        enable_streaming=bot.enable_streaming,
        orchestrator=bot.orchestrator,
        event_cache=None,
    )
    hook_context = HookContextSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        agent_name=bot.agent_name,
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )
    tool_runtime = ToolRuntimeSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        storage_path=storage_path,
        agent_name=bot.agent_name,
        matrix_id=bot.matrix_id,
        resolver=bot._conversation_resolver,
        hook_context=hook_context,
    )

    post_response_effects = PostResponseEffectsSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        delivery_gateway=delivery_gateway,
    )
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))

    return ResponseCoordinator(
        ResponseCoordinatorDeps(
            runtime=runtime,
            logger=bot.logger,
            stop_manager=bot.stop_manager,
            runtime_paths=runtime_paths,
            storage_path=storage_path,
            agent_name=bot.agent_name,
            matrix_full_id=bot.matrix_id.full_id,
            resolver=bot._conversation_resolver,
            tool_runtime=tool_runtime,
            knowledge_access=bot._knowledge_access_support,
            delivery_gateway=delivery_gateway,
            post_response_effects=post_response_effects,
            state_writer=bot._conversation_state_writer,
        ),
    )


def _response_request(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$user_msg",
    thread_id: str | None = None,
    prompt: str = "Hello",
    user_id: str | None = None,
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        thread_history=(),
        prompt=prompt,
        user_id=user_id,
    )


class TestUserIdPassthrough:
    """Test that user_id reaches agent.arun() in both streaming and non-streaming paths."""

    @pytest.mark.asyncio
    async def test_non_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond passes user_id through to ai_response."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(_config(), runtime_paths)
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.storage_path = tmp_path
        bot.config = config
        bot.runtime_paths = runtime_paths
        bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response_id")
        with (
            patch("mindroom.response_coordinator.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
            patch("mindroom.response_coordinator.ai_response") as mock_ai,
        ):
            coordinator = _build_response_coordinator(
                bot,
                config=config,
                runtime_paths=runtime_paths,
                storage_path=tmp_path,
                requester_id="@alice:localhost",
            )

            async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
                context = get_tool_runtime_context()
                assert context is not None
                assert context.room_id == "!test:localhost"
                assert context.thread_id is None
                assert context.requester_id == "@alice:localhost"
                return "Hello!"

            mock_ai.side_effect = fake_ai_response

            await coordinator.process_and_respond(
                _response_request(prompt="Hello", user_id="@alice:localhost"),
            )

            mock_ai.assert_called_once()
            assert mock_ai.call_args.kwargs["user_id"] == "@alice:localhost"
            assert callable(mock_ai.call_args.kwargs["run_id_callback"])

    @pytest.mark.asyncio
    async def test_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond_streaming passes user_id through to stream_agent_response."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(_config(), runtime_paths)
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
        bot.runtime_paths = runtime_paths
        bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()
        with (
            patch("mindroom.response_coordinator.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
            patch("mindroom.response_coordinator.stream_agent_response") as mock_stream,
        ):
            coordinator = _build_response_coordinator(
                bot,
                config=config,
                runtime_paths=runtime_paths,
                storage_path=tmp_path,
                requester_id="@bob:localhost",
            )

            async def consume_delivery(request: object) -> tuple[str, str]:
                response_stream = request.response_stream
                chunks = [chunk async for chunk in response_stream]
                return "$msg_id", "".join(chunks)

            coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery

            def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
                async def fake_stream() -> AsyncIterator[str]:
                    context = get_tool_runtime_context()
                    assert context is not None
                    assert context.room_id == "!test:localhost"
                    assert context.thread_id is None
                    assert context.requester_id == "@bob:localhost"
                    yield "Hello!"

                return fake_stream()

            mock_stream.side_effect = fake_stream_agent_response

            await coordinator.process_and_respond_streaming(
                _response_request(prompt="Hello", user_id="@bob:localhost"),
            )

            mock_stream.assert_called_once()
            assert mock_stream.call_args.kwargs["user_id"] == "@bob:localhost"
            assert callable(mock_stream.call_args.kwargs["run_id_callback"])

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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                user_id="@user:localhost",
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] == "@user:localhost"

    @pytest.mark.asyncio
    async def test_ai_response_passes_run_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Non-streaming cancellation needs an explicit run_id threaded to Agno."""
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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                run_id="run-123",
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["run_id"] == "run-123"

    @pytest.mark.asyncio
    async def test_prepare_agent_and_prompt_threads_config_path_to_create_agent(self, tmp_path: Path) -> None:
        """The shared agent-build helper should preserve an explicit orchestrator config path."""
        config = _config()
        config_path = tmp_path / "custom-config.yaml"
        mock_agent = MagicMock()

        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.build_prompt_with_thread_history", return_value="enhanced"),
            patch("mindroom.ai.create_agent", return_value=mock_agent) as mock_create_agent,
        ):
            agent, full_prompt, unseen_event_ids, prepared_history = await _prepare_agent_and_prompt(
                agent_name="general",
                prompt="test",
                runtime_paths=_runtime_paths(tmp_path, config_path=config_path),
                config=config,
            )

        assert agent is mock_agent
        assert full_prompt == "enhanced"
        assert unseen_event_ids == []
        assert prepared_history.compaction_outcomes == []
        assert prepared_history.replays_persisted_history is False
        assert prepared_history.replay_plan is not None
        assert prepared_history.replay_plan.mode == "configured"
        assert "runtime_paths" not in mock_create_agent.call_args.kwargs

    @pytest.mark.asyncio
    async def test_ai_response_passes_config_path_to_prepare_agent(self, tmp_path: Path) -> None:
        """Non-streaming replies should build agents against the orchestrator-owned config file."""
        config_path = tmp_path / "custom-config.yaml"
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path, config_path=config_path),
                config=_config(),
            )

        assert mock_prepare.call_args.args[2].config_path == config_path

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_config_path_to_prepare_agent(self, tmp_path: Path) -> None:
        """Streaming replies should build agents against the orchestrator-owned config file."""
        config_path = tmp_path / "custom-config.yaml"
        mock_agent = MagicMock()

        async def _empty_stream() -> AsyncIterator[str]:
            if False:
                yield ""

        mock_agent.arun = MagicMock(return_value=_empty_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            _ = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path, config_path=config_path),
                    config=_config(),
                )
            ]

        assert mock_prepare.call_args.args[2].config_path == config_path

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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            # Consume the async generator to trigger the agent.arun call.
            _chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    user_id="@user:localhost",
                )
            ]

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] == "@user:localhost"

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_run_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Streaming cancellation needs an explicit run_id threaded to Agno."""
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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            _chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    run_id="run-456",
                )
            ]

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["run_id"] == "run-456"

    @pytest.mark.asyncio
    async def test_ai_response_raises_cancelled_error_for_cancelled_runs(self, tmp_path: Path) -> None:
        """Gracefully cancelled Agno runs should surface as task cancellation to the bot."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Run run-123 was cancelled"
        mock_run_output.tools = None
        mock_run_output.status = RunStatus.cancelled
        mock_run_output.run_id = "run-123"
        mock_run_output.session_id = "session1"
        mock_run_output.model = "test-model"
        mock_run_output.model_provider = "openai"
        mock_run_output.metrics = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                )

    @pytest.mark.asyncio
    async def test_ai_response_returns_friendly_error_for_error_status(self, tmp_path: Path) -> None:
        """Errored Agno RunOutput values must not be surfaced as successful replies."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "validation failed in agno"
        mock_run_output.status = RunStatus.error
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
            patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly-error") as mock_friendly_error,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )

        assert response == "friendly-error"
        mock_friendly_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_ai_response_passes_all_files_for_vertex_claude(self, tmp_path: Path) -> None:
        """Vertex Claude path should not silently drop non-PDF file media."""
        mock_agent = MagicMock()
        mock_agent.model = VertexAIClaude(id="claude-sonnet-4@20250514")
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        pdf_file = File(filepath=str(tmp_path / "report.pdf"), filename="report.pdf", mime_type="application/pdf")
        zip_file = File(filepath=str(tmp_path / "archive.zip"), filename="archive.zip")

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[pdf_file, zip_file]),
            )

        mock_agent.arun.assert_called_once()
        sent_files = list(mock_agent.arun.call_args.kwargs["files"])
        assert sent_files == [pdf_file, zip_file]

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_all_files_for_vertex_claude(self, tmp_path: Path) -> None:
        """Streaming path should not silently drop non-PDF files for Vertex Claude."""
        mock_agent = MagicMock()
        mock_agent.model = VertexAIClaude(id="claude-sonnet-4@20250514")
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="chunk")

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        pdf_file = File(filepath=str(tmp_path / "report.pdf"), filename="report.pdf", mime_type="application/pdf")
        zip_file = File(filepath=str(tmp_path / "archive.zip"), filename="archive.zip")

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            _chunks = [
                _chunk
                async for _chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[pdf_file, zip_file]),
                )
            ]

        mock_agent.arun.assert_called_once()
        sent_files = list(mock_agent.arun.call_args.kwargs["files"])
        assert sent_files == [pdf_file, zip_file]

    @pytest.mark.asyncio
    async def test_ai_response_retries_without_media_on_validation_error(self, tmp_path: Path) -> None:
        """When inline media is rejected, non-streaming should retry once without media."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Recovered response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(
            side_effect=[
                Exception(
                    "litellm.BadRequestError: invalid_request_error: "
                    "document.source.base64.media_type: Input should be 'application/pdf'",
                ),
                mock_run_output,
            ],
        )

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[document_file]),
            )

        assert response == "Recovered response"
        assert mock_agent.arun.await_count == 2
        first_call = mock_agent.arun.await_args_list[0]
        second_call = mock_agent.arun.await_args_list[1]
        assert list(first_call.kwargs["files"]) == [document_file]
        assert list(second_call.kwargs["files"]) == []
        assert "Inline media unavailable for this model" in second_call.args[0]

    @pytest.mark.asyncio
    async def test_ai_response_retries_errored_run_output_with_fresh_run_id(self, tmp_path: Path) -> None:
        """Inline-media retries must use a fresh Agno run_id after an errored run output."""
        mock_agent = MagicMock()
        error_output = MagicMock()
        error_output.content = "Error code: 500 - audio input is not supported"
        error_output.status = RunStatus.error
        error_output.tools = None

        success_output = MagicMock()
        success_output.content = "Recovered response"
        success_output.status = RunStatus.completed
        success_output.tools = None

        seen_run_ids: list[str | None] = []
        callback_run_ids: list[str] = []
        responses = [error_output, success_output]

        async def fake_run(*_args: object, **kwargs: object) -> MagicMock:
            seen_run_ids.append(kwargs["run_id"])
            run_id_callback = kwargs["run_id_callback"]
            if run_id_callback is not None and kwargs["run_id"] is not None:
                run_id_callback(kwargs["run_id"])
            return responses.pop(0)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", side_effect=fake_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                run_id="run-123",
                run_id_callback=callback_run_ids.append,
                media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            )

        assert response == "Recovered response"
        assert seen_run_ids[0] == "run-123"
        assert seen_run_ids[1] is not None
        assert seen_run_ids[1] != "run-123"
        assert callback_run_ids == [run_id for run_id in seen_run_ids if run_id is not None]

    @pytest.mark.asyncio
    async def test_stream_agent_response_retries_without_media_on_validation_error(self, tmp_path: Path) -> None:
        """When inline media is rejected, streaming should retry once without media."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def failing_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(
                content=(
                    "litellm.BadRequestError: invalid_request_error: "
                    "document.source.base64.media_type: Input should be 'application/pdf'"
                ),
            )

        async def successful_stream() -> AsyncIterator[object]:
            yield RunContentEvent(content="Recovered stream")

        mock_agent.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[document_file]),
                )
            ]

        assert mock_agent.arun.call_count == 2
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        assert list(first_call.kwargs["files"]) == [document_file]
        assert list(second_call.kwargs["files"]) == []
        assert "Inline media unavailable for this model" in second_call.args[0]
        assert any(isinstance(chunk, RunContentEvent) and chunk.content == "Recovered stream" for chunk in chunks)

    @pytest.mark.asyncio
    async def test_stream_agent_response_retries_with_fresh_run_id(self, tmp_path: Path) -> None:
        """Streaming inline-media retries must not reuse the cancelled attempt's run_id."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def failing_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(content="Error code: 500 - audio input is not supported")

        async def successful_stream() -> AsyncIterator[object]:
            yield RunContentEvent(content="Recovered stream")

        callback_run_ids: list[str] = []
        mock_agent.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    run_id="run-456",
                    run_id_callback=callback_run_ids.append,
                    media=MediaInputs(audio=[MagicMock(name="audio_input")]),
                )
            ]

        assert any(isinstance(chunk, RunContentEvent) and chunk.content == "Recovered stream" for chunk in chunks)
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        assert first_call.kwargs["run_id"] == "run-456"
        assert second_call.kwargs["run_id"] is not None
        assert second_call.kwargs["run_id"] != "run-456"
        assert callback_run_ids == [first_call.kwargs["run_id"], second_call.kwargs["run_id"]]

    @pytest.mark.parametrize(
        ("error_text", "expected"),
        [
            (
                "invalid_request_error: messages.1.content.0.document.source.base64.media_type: Input should be 'application/pdf'",
                True,
            ),
            (
                "invalid_request_error: messages.8.content.1.image.source.base64: The image was specified using the image/jpeg media type, but the image appears to be a image/png image",
                True,
            ),
            ("Error code: 500 - audio input is not supported", True),
            ("invalid_request_error: max_tokens must be <= 4096", False),
            ("Rate limit exceeded", False),
        ],
    )
    def test_should_retry_without_inline_media_error_matching(self, error_text: str, expected: bool) -> None:
        """Retry matcher should target inline-media validation and unsupported-input failures."""
        assert should_retry_without_inline_media(error_text, MediaInputs(images=(object(),))) is expected

    def test_append_inline_media_fallback_prompt_is_idempotent(self) -> None:
        """Fallback marker should only be appended once across retries."""
        initial_prompt = "Inspect this attachment."
        first = append_inline_media_fallback_prompt(initial_prompt)
        second = append_inline_media_fallback_prompt(first)
        assert first == second

    @pytest.mark.asyncio
    async def test_ai_response_does_not_retry_without_media_validation_match(self, tmp_path: Path) -> None:
        """Non-media failures should not trigger inline-media retry even when media is present."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False
        mock_agent.arun = AsyncMock(side_effect=Exception("invalid_request_error: max_tokens must be <= 4096"))

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly") as mock_friendly_error,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[document_file]),
            )

        assert response == "friendly"
        assert mock_agent.arun.await_count == 1
        mock_friendly_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_agent_response_retries_only_once_on_repeated_media_validation_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming should attempt exactly one inline-media fallback retry."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def media_validation_error_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(
                content=(
                    "invalid_request_error: "
                    "messages.3.content.0.document.source.base64.media_type: Input should be 'application/pdf'"
                ),
            )

        mock_agent.arun = MagicMock(
            side_effect=[media_validation_error_stream(), media_validation_error_stream()],
        )

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly-error") as mock_friendly_error,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[document_file]),
                )
            ]

        assert mock_agent.arun.call_count == 2
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        assert list(first_call.kwargs["files"]) == [document_file]
        assert list(second_call.kwargs["files"]) == []
        assert second_call.args[0].count("Inline media unavailable for this model") == 1
        assert chunks == ["friendly-error"]
        mock_friendly_error.assert_called_once()

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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            # Call without user_id
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] is None

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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            tool_trace: list[object] = []
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
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

    def test_build_matrix_run_metadata_merges_coalesced_source_event_ids(self) -> None:
        """Run metadata should mark every source event in a coalesced batch as seen."""
        metadata = build_matrix_run_metadata(
            "$primary",
            ["$unseen"],
            extra_metadata={
                MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$primary"],
                MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$first": "first", "$primary": "primary"},
            },
        )

        assert metadata == {
            MATRIX_EVENT_ID_METADATA_KEY: "$primary",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$primary", "$first", "$unseen"],
            MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$primary"],
            MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$first": "first", "$primary": "primary"},
        }

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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
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
    async def test_ai_response_metadata_uses_room_resolved_runtime_model(self, tmp_path: Path) -> None:
        """Non-streaming metadata should report the room-resolved runtime model."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(
            Config(
                agents={"general": AgentConfig(display_name="General", model="default")},
                room_models={"lobby": "large"},
                models={
                    "default": ModelConfig(provider="openai", id="default-model", context_window=2000),
                    "large": ModelConfig(provider="openai", id="large-model", context_window=48000),
                },
            ),
            runtime_paths,
        )
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.run_id = "run-room"
        mock_run_output.session_id = "session1"
        mock_run_output.status = RunStatus.completed
        mock_run_output.model = "large-model"
        mock_run_output.model_provider = "openai"
        mock_run_output.metrics = Metrics(input_tokens=800, output_tokens=50, total_tokens=850, duration=1.2)
        mock_run_output.tools = None
        mock_run_output.content = "Response"

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
            patch("mindroom.matrix.rooms.get_room_alias_from_id", return_value="lobby"),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                room_id="!test:localhost",
                runtime_paths=runtime_paths,
                config=config,
                run_metadata_collector=run_metadata,
            )

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["model"]["config"] == "large"
        assert payload["model"]["id"] == "large-model"
        assert payload["context"]["window_tokens"] == 48000

    @pytest.mark.asyncio
    async def test_stream_agent_response_metadata_uses_room_resolved_runtime_model(self, tmp_path: Path) -> None:
        """Streaming metadata should report the room-resolved runtime model."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(
            Config(
                agents={"general": AgentConfig(display_name="General", model="default")},
                room_models={"lobby": "large"},
                models={
                    "default": ModelConfig(provider="openai", id="default-model", context_window=1000),
                    "large": ModelConfig(provider="openai", id="large-model", context_window=32000),
                },
            ),
            runtime_paths,
        )
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "large-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="hello")
            yield ModelRequestCompletedEvent(
                model="large-model",
                model_provider="openai",
                input_tokens=500,
                output_tokens=60,
                total_tokens=560,
                time_to_first_token=0.33,
            )
            yield RunCompletedEvent(
                run_id="run-room-stream",
                session_id="session1",
                metrics=Metrics(
                    input_tokens=500,
                    output_tokens=60,
                    total_tokens=560,
                    duration=2.4,
                ),
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.matrix.rooms.get_room_alias_from_id", return_value="lobby"),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                room_id="!test:localhost",
                runtime_paths=runtime_paths,
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["model"]["config"] == "large"
        assert payload["model"]["id"] == "large-model"
        assert payload["context"]["window_tokens"] == 32000

    @pytest.mark.asyncio
    async def test_stream_agent_response_raises_cancelled_error_for_run_cancelled_event(self, tmp_path: Path) -> None:
        """Graceful stream cancellation should preserve metadata and end as CancelledError."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="partial")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=100,
                output_tokens=25,
                total_tokens=125,
            )
            yield RunCancelledEvent(
                run_id="run-3",
                session_id="session1",
                reason="Run run-3 was cancelled",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            with pytest.raises(asyncio.CancelledError):
                async for _chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=config,
                    run_metadata_collector=run_metadata,
                ):
                    pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["run_id"] == "run-3"
        assert payload["status"] == "cancelled"
        assert payload["usage"]["input_tokens"] == 100
        assert payload["usage"]["output_tokens"] == 25
        assert payload["usage"]["total_tokens"] == 125

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
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
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

    @pytest.mark.asyncio
    async def test_stream_agent_response_uses_latest_request_tokens_for_context(self, tmp_path: Path) -> None:
        """Streaming context metadata should reflect the latest request, not cumulative run usage."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="step one")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=700,
                output_tokens=50,
                total_tokens=750,
            )
            yield RunContentEvent(content="step two")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=120,
                output_tokens=20,
                total_tokens=140,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["status"] == "completed"
        assert payload["usage"]["input_tokens"] == 820
        assert payload["usage"]["output_tokens"] == 70
        assert payload["usage"]["total_tokens"] == 890
        assert payload["context"]["input_tokens"] == 120
        assert payload["context"]["window_tokens"] == 1000
