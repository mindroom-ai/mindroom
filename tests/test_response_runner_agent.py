"""Agent response lifecycle through the ResponseRunner seam: process_and_respond, delivery, and generate_response."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import nio
import pytest
from agno.session.agent import AgentSession

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    TOOL_TRACE_CONTENT_KEY,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.delivery_gateway import (
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    ResponseIdentity,
)
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.handled_turns import TurnRecord
from mindroom.history.storage import write_scope_state
from mindroom.history.types import CompactionLifecycleStart, HistoryScope, HistoryScopeState
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    AfterResponseContext,
    BeforeResponseContext,
    EnrichmentItem,
    HookRegistry,
    MessageEnvelope,
    hook,
)
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import _response_outcome_label
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparer
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
    _merge_response_extra_content,
    _ResponseGenerationOutcome,
)
from mindroom.streaming import StreamingDeliveryError
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from tests.bot_helpers import (
    AgentBotTestBase,
    _empty_full_thread_history,
    _handled_response_event_id,
    _hook_envelope,
    _hook_plugin,
    _install_runtime_cache_support,
    _make_matrix_client_mock,
    _noop_typing_indicator,
    _outcome,
    _response_request,
    _room_send_response,
    _runtime_bound_config,
    _set_knowledge_for_agent,
    _set_turn_store_tracker,
    _stream_outcome,
    _visible_message,
    _visible_response_event_id,
    make_mock_agent_user,
)
from tests.conftest import (
    delivered_matrix_event,
    delivered_matrix_side_effect,
    message_origin,
    patch_response_runner_module,
    replace_delivery_gateway_deps,
    request_envelope,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
    from pathlib import Path

    from mindroom.matrix.client import DeliveredMatrixEvent
    from mindroom.matrix.users import AgentMatrixUser
    from mindroom.post_response_effects import ResponseOutcome
    from mindroom.response_turn import ResponseTurnContext


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


def _single_matrix_target_item(ctx: ResponseTurnContext) -> EnrichmentItem:
    """Return the single stable Matrix-target item carried by one turn context."""
    items = [item for item in ctx.system_enrichment_items if item.key == "matrix_message_target"]
    assert len(items) == 1
    return items[0]


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_process_and_respond_includes_matrix_metadata_when_tool_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agents with matrix_message should receive one stable Matrix-target system item."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            generation = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event123",
                        prompt="Please send an update",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert generation.delivery.event_id == "$response"
        assert mock_ai.call_args.kwargs["prompt"] == "Please send an update"
        assert "[Matrix metadata for tool calls]" not in mock_ai.call_args.kwargs["model_prompt"]
        item = _single_matrix_target_item(mock_ai.call_args.args[0])
        assert item.cache_policy == "stable"
        assert "!test:localhost" in item.text
        assert "do not pass `thread_id`" in item.text
        assert "$event123" not in item.text

    @pytest.mark.asyncio
    async def test_process_and_respond_includes_matrix_metadata_when_openclaw_compat_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """openclaw_compat agents should receive room/thread/event ids in the model prompt."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["openclaw_compat"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            generation = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event123",
                        prompt="Please send an update",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert generation.delivery.event_id == "$response"
        assert mock_ai.call_args.kwargs["prompt"] == "Please send an update"
        assert "[Matrix metadata for tool calls]" not in mock_ai.call_args.kwargs["model_prompt"]
        item = _single_matrix_target_item(mock_ai.call_args.args[0])
        assert "!test:localhost" in item.text
        assert "do not pass `thread_id`" in item.text

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_includes_matrix_metadata_when_tool_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming path should inject Matrix ids for agents with matrix messaging tools."""

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock()

        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                generation = await bot._response_runner.process_and_respond_streaming(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please reply in thread",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event456",
                            prompt="Please reply in thread",
                            user_id="@user:localhost",
                        ),
                    ),
                )

        assert generation.delivery.event_id == "$response"
        assert mock_stream_agent_response.call_args.kwargs["prompt"] == "Please reply in thread"
        assert "[Matrix metadata for tool calls]" not in mock_stream_agent_response.call_args.kwargs["model_prompt"]
        item = _single_matrix_target_item(mock_stream_agent_response.call_args.args[0])
        assert item.cache_policy == "stable"
        assert "!test:localhost" in item.text
        assert "do not pass `thread_id`" in item.text
        assert "$event456" not in item.text

    @pytest.mark.asyncio
    async def test_process_and_respond_uses_safe_thread_root_for_prompt_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Prompt metadata should prefer the stable thread root over plain reply event IDs."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$reply_plain:localhost",
            thread_start_root_event_id="$thread_root:localhost",
        )

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$reply_plain:localhost",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$reply_plain:localhost",
                        prompt="Continue",
                        user_id="@user:localhost",
                        target=target,
                    ),
                ),
            )

        item = _single_matrix_target_item(mock_ai.call_args.args[0])
        assert "thread $thread_root:localhost" in item.text
        assert "as `target`" in item.text
        assert "$reply_plain:localhost" not in item.text

    @pytest.mark.asyncio
    async def test_process_and_respond_keeps_thread_root_metadata_when_reply_anchor_is_root(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thread-root replies should preserve the canonical thread id in tool-call metadata."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$thread_root:localhost",
            thread_start_root_event_id="$thread_root:localhost",
        )

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$thread_root:localhost",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$thread_root:localhost",
                        prompt="Continue",
                        user_id="@user:localhost",
                        target=target,
                    ),
                ),
            )

        item = _single_matrix_target_item(mock_ai.call_args.args[0])
        assert "thread $thread_root:localhost" in item.text
        assert "as `target`" in item.text

    @pytest.mark.asyncio
    async def test_matrix_target_context_is_identical_across_source_events(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Two turns with different source events must produce byte-identical target text."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            for reply_to_event_id in ("$first_event:localhost", "$second_event:localhost"):
                target = MessageTarget.resolve(
                    room_id="!test:localhost",
                    thread_id=None,
                    reply_to_event_id=reply_to_event_id,
                    thread_start_root_event_id="$thread_root:localhost",
                )
                await bot._response_runner.process_and_respond(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Continue",
                        reply_to_event_id=reply_to_event_id,
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id=reply_to_event_id,
                            prompt="Continue",
                            user_id="@user:localhost",
                            target=target,
                        ),
                    ),
                )

        first_item, second_item = (_single_matrix_target_item(call.args[0]) for call in mock_ai.call_args_list)
        assert first_item.text == second_item.text
        assert "$first_event:localhost" not in first_item.text
        assert "$second_event:localhost" not in second_item.text

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_resolves_knowledge_once(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming should resolve knowledge only inside the request-scoped context."""

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                generation = await bot._response_runner.process_and_respond_streaming(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Hello",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event456",
                            prompt="Hello",
                            user_id="@user:localhost",
                        ),
                    ),
                )

        assert generation.delivery.event_id == "$response"
        bot._knowledge_access_support.resolve_for_agent.assert_called_once()
        args, kwargs = bot._knowledge_access_support.resolve_for_agent.call_args
        assert args == ("calculator",)
        assert kwargs["execution_identity"] is not None

    @pytest.mark.asyncio
    async def test_process_and_respond_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming responses should persist attachment IDs in message metadata."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}
            return "Handled"

        mock_ai = AsyncMock(side_effect=fake_ai_response)
        attachment_ids = ["att_image", "att_zip"]
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please inspect attachments",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    attachment_ids=attachment_ids,
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event123",
                        prompt="Please inspect attachments",
                        user_id="@user:localhost",
                        attachment_ids=tuple(attachment_ids),
                    ),
                ),
            )

        sent_extra_content = bot.client.room_send.await_args.kwargs["content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming responses should persist attachment IDs in message metadata."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        captured_collector: dict[str, Any] = {}

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            captured_collector.update({"ref": kwargs["run_metadata_collector"]})

            async def _gen() -> AsyncGenerator[str, None]:
                yield "chunk"
                # Populate metadata during iteration, matching production ordering
                # where ai.py populates metadata after streaming completes.
                kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}

            return _gen()

        async def _consuming_send_streaming(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            stream = args[4]
            async for _ in stream:
                pass
            return StreamTransportOutcome(
                last_physical_stream_event_id="$response",
                terminal_status="completed",
                rendered_body="chunk",
                visible_body_state="visible_body",
            )

        attachment_ids = ["att_image", "att_zip"]
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please inspect attachments",
                    reply_to_event_id="$event456",
                    thread_history=[],
                    user_id="@user:localhost",
                    attachment_ids=attachment_ids,
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event456",
                        prompt="Please inspect attachments",
                        user_id="@user:localhost",
                        attachment_ids=tuple(attachment_ids),
                    ),
                ),
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        # Metadata was populated during generator iteration (not synchronously),
        # proving the mutable reference is preserved through _merge_response_extra_content.
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1
        # The extra_content dict IS the same object as the collector
        assert sent_extra_content is captured_collector["ref"]

    def test_merge_response_extra_content_preserves_mutable_reference(self) -> None:
        """_merge_response_extra_content must return the SAME dict object when extra_content is provided."""
        collector: dict[str, Any] = {}
        result = _merge_response_extra_content(collector, None)
        assert result is collector

    def test_merge_response_extra_content_returns_none_when_both_absent(self) -> None:
        """_merge_response_extra_content returns None when no extra_content and no attachment_ids."""
        assert _merge_response_extra_content(None, None) is None
        assert _merge_response_extra_content(None, []) is None

    def test_merge_response_extra_content_merges_attachment_ids(self) -> None:
        """_merge_response_extra_content merges attachment_ids into extra_content."""
        collector: dict[str, Any] = {}
        result = _merge_response_extra_content(collector, ["att_1"])
        assert result is collector
        assert result[ATTACHMENT_IDS_KEY] == ["att_1"]

    def test_merge_response_extra_content_creates_dict_for_attachment_ids_only(self) -> None:
        """_merge_response_extra_content creates a dict when only attachment_ids are provided."""
        result = _merge_response_extra_content(None, ["att_1"])
        assert result is not None
        assert result[ATTACHMENT_IDS_KEY] == ["att_1"]

    @pytest.mark.asyncio
    async def test_streaming_metadata_propagation_through_mutable_reference(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Metadata populated during generator iteration must appear in extra_content via mutable reference."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            async def _gen() -> AsyncGenerator[str, None]:
                yield "hello"
                # Populate after first yield, mimicking production ai.py ordering
                kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {
                    "version": 1,
                    "model": "test-model",
                    "tokens": {"input": 10, "output": 5},
                }

            return _gen()

        async def _consuming_send_streaming(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            stream = args[4]
            async for _ in stream:
                pass
            return StreamTransportOutcome(
                last_physical_stream_event_id="$response",
                terminal_status="completed",
                rendered_body="hello",
                visible_body_state="visible_body",
            )

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Hello",
                    reply_to_event_id="$event789",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event789",
                        prompt="Hello",
                        user_id="@user:localhost",
                    ),
                ),
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content is not None
        ai_run = sent_extra_content["io.mindroom.ai_run"]
        assert ai_run["version"] == 1
        assert ai_run["model"] == "test-model"
        assert ai_run["tokens"] == {"input": 10, "output": 5}

    @pytest.mark.asyncio
    async def test_streaming_cancelled_response_preserves_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """CancelledError during streaming must still carry io.mindroom.ai_run in extra_content."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            async def _gen() -> AsyncGenerator[str, None]:
                kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}
                yield "partial"
                raise asyncio.CancelledError

            return _gen()

        captured_extra_content_ref: list[dict[str, Any] | None] = [None]

        async def _consuming_send_streaming(*args: object, **kwargs: object) -> StreamTransportOutcome:
            captured_extra_content_ref[0] = kwargs.get("extra_content")
            stream = args[4]
            try:
                async for _ in stream:
                    pass
            except asyncio.CancelledError:
                pass
            # In production, send_streaming_response catches CancelledError,
            # sends the final edit, then re-raises. We simulate the re-raise.
            raise asyncio.CancelledError

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ),
            pytest.raises(asyncio.CancelledError),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Cancel me",
                    reply_to_event_id="$event_cancel",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event_cancel",
                        prompt="Cancel me",
                        user_id="@user:localhost",
                    ),
                ),
            )

        # The extra_content dict (mutable reference) was populated during iteration
        extra = captured_extra_content_ref[0]
        assert extra is not None
        assert "io.mindroom.ai_run" in extra
        assert extra["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_preserves_terminal_event_id_on_error(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming failures should preserve the terminal event id after finalizing the visible message."""

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=StreamingDeliveryError(
                    RuntimeError("boom"),
                    event_id="$terminal",
                    accumulated_text="partial\n\n**[Response interrupted by an error: boom]**",
                    tool_trace=[],
                    transport_outcome=_stream_outcome(
                        "$terminal",
                        "partial\n\n**[Response interrupted by an error: boom]**",
                        terminal_status="error",
                        failure_reason="boom",
                    ),
                ),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ),
        ):
            generation = await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please continue",
                    reply_to_event_id="$event-error",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event-error",
                        prompt="Please continue",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert _visible_response_event_id(generation.delivery) == "$terminal"
        assert _handled_response_event_id(generation.delivery) == "$terminal"
        assert generation.delivery.delivery_kind is None
        assert "Response interrupted by an error" in generation.delivery.response_text

    @pytest.mark.asyncio
    async def test_process_and_respond_applies_before_and_after_hooks_non_streaming(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct non-streaming delivery still applies before_response before lifecycle finalization."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            generation = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=_hook_envelope(body="Please send an update", source_event_id="$event123"),
                    correlation_id="corr-hook",
                ),
            )

        assert generation.delivery.event_id == "$response"
        assert before_calls == 1
        assert bot.client.room_send.await_args.kwargs["content"]["body"] == "Handled [hooked]"
        assert after_results == []

    @pytest.mark.asyncio
    async def test_process_and_respond_passes_active_response_event_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming AI calls should receive only live tracked event IDs for the room."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        running_task = asyncio.create_task(asyncio.sleep(60))
        done_task = asyncio.create_task(asyncio.sleep(0))
        other_room_task = asyncio.create_task(asyncio.sleep(60))
        await done_task
        bot.stop_manager.set_current("$active", MessageTarget.resolve("!test:localhost", None, "$active"), running_task)
        bot.stop_manager.set_current("$done", MessageTarget.resolve("!test:localhost", None, "$done"), done_task)
        bot.stop_manager.set_current(
            "$other-room",
            MessageTarget.resolve("!other:localhost", None, "$other-room"),
            other_room_task,
        )

        try:
            mock_ai_response = AsyncMock(return_value="Handled")
            with patch_response_runner_module(
                typing_indicator=noop_typing_indicator,
                ai_response=mock_ai_response,
            ):
                await bot._response_runner.process_and_respond(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please continue",
                        reply_to_event_id="$event123",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event123",
                            prompt="Please continue",
                            user_id="@user:localhost",
                        ),
                    ),
                )

            assert mock_ai_response.call_args.args[0].active_event_ids == frozenset({"$active"})
        finally:
            running_task.cancel()
            other_room_task.cancel()
            await asyncio.gather(running_task, other_room_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_ignores_post_visible_before_response_mutation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct streamed delivery keeps before_response off the post-visible path before lifecycle finalization."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"
            ctx.draft.suppress = True

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
        ):
            mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                generation = await bot._response_runner.process_and_respond_streaming(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please reply in thread",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=_hook_envelope(body="Please reply in thread", source_event_id="$event456"),
                        correlation_id="corr-stream",
                    ),
                )

        assert generation.delivery.event_id == "$response"
        assert before_calls == 0
        mock_edit_message.assert_not_awaited()
        assert after_results == []

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_passes_active_response_event_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming AI calls should receive only live tracked event IDs for the room."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        running_task = asyncio.create_task(asyncio.sleep(60))
        done_task = asyncio.create_task(asyncio.sleep(0))
        other_room_task = asyncio.create_task(asyncio.sleep(60))
        await done_task
        bot.stop_manager.set_current("$active", MessageTarget.resolve("!test:localhost", None, "$active"), running_task)
        bot.stop_manager.set_current("$done", MessageTarget.resolve("!test:localhost", None, "$done"), done_task)
        bot.stop_manager.set_current(
            "$other-room",
            MessageTarget.resolve("!other:localhost", None, "$other-room"),
            other_room_task,
        )

        try:
            mock_stream = MagicMock(return_value=mock_streaming_response())
            with patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response:
                mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
                with patch_response_runner_module(
                    typing_indicator=noop_typing_indicator,
                    stream_agent_response=mock_stream,
                ):
                    await bot._response_runner.process_and_respond_streaming(
                        _response_request(
                            room_id="!test:localhost",
                            prompt="Please continue",
                            reply_to_event_id="$event456",
                            thread_history=[],
                            user_id="@user:localhost",
                            response_envelope=request_envelope(
                                room_id="!test:localhost",
                                reply_to_event_id="$event456",
                                prompt="Please continue",
                                user_id="@user:localhost",
                            ),
                        ),
                    )

            assert mock_stream.call_args.args[0].active_event_ids == frozenset({"$active"})
        finally:
            running_task.cancel()
            other_room_task.cancel()
            await asyncio.gather(running_task, other_room_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_deliver_generated_response_redacts_suppressed_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a placeholder-backed response should redact the provisional event."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock(return_value=True)
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Handled",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        delivery = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_text="Handled",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-suppress",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert delivery.suppressed is True
        assert delivery.event_id is None
        redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )
        assert _handled_response_event_id(delivery) is None

    @pytest.mark.asyncio
    async def test_deliver_generated_response_suppressed_existing_event_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a non-placeholder edit should keep the prior visible event retryable."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock()
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Handled",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        delivery = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
                response_text="Handled",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-existing-suppress",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert delivery.suppressed is True
        assert delivery.event_id == "$existing"
        redact_message_event.assert_not_awaited()
        assert _handled_response_event_id(delivery) is None

    @pytest.mark.asyncio
    async def test_deliver_generated_response_raises_when_suppressed_placeholder_redaction_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A failed placeholder redaction should stay inside the typed terminal contract."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock(return_value=False)
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Handled",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_text="Handled",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-suppress-fail",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "error"
        assert _visible_response_event_id(outcome) == "$placeholder"
        assert _handled_response_event_id(outcome) is None
        assert outcome.mark_handled is False
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

        redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("hook_action"), ["rewrite", "suppress"])
    async def test_streamed_before_response_no_longer_mutates_post_visible_success(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        hook_action: str,
    ) -> None:
        """message:before_response must not mutate or suppress once streamed text is already visible."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            if hook_action == "rewrite":
                ctx.draft.response_text = "updated text"
            else:
                ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock(return_value=True)
        gateway = replace_delivery_gateway_deps(bot, redact_message_event=redact_message_event)
        mock_deliver_final = AsyncMock(
            return_value=FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$streaming",
                is_visible_response=True,
                final_visible_body="updated text",
                delivery_kind="edited",
            ),
        )
        object.__setattr__(gateway, "deliver_final", mock_deliver_final)

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=_stream_outcome("$streaming", "chunk"),
                initial_delivery_kind="sent",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-finalize-stream-visible-failure",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "completed"
        assert outcome.final_visible_event_id == "$streaming"
        assert outcome.final_visible_body == "chunk"
        mock_deliver_final.assert_not_awaited()
        redact_message_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finalize_streamed_response_cancelled_placeholder_only_stream_cleans_up_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Interrupted terminal finalization must redact a placeholder-only stream instead of leaking Thinking...."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=True),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=_stream_outcome(
                    "$thinking",
                    "Thinking...",
                    terminal_status="cancelled",
                    visible_body_state="placeholder_only",
                    failure_reason="terminal_update_cancelled",
                ),
                initial_delivery_kind="edited",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-finalize-stream-cancelled-placeholder",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "cancelled"
        gateway.deps.redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$thinking",
            reason="Completed placeholder-only streamed response",
        )
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finalize_streamed_response_placeholder_cleanup_failure_is_unhandled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Failed placeholder-only cleanup should leave the user turn retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=False),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=_stream_outcome(
                    "$thinking",
                    "Thinking...",
                    terminal_status="cancelled",
                    visible_body_state="placeholder_only",
                    failure_reason="terminal_update_cancelled",
                ),
                initial_delivery_kind="edited",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-finalize-stream-placeholder-cleanup-failed",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "error"
        assert outcome.event_id == "$thinking"
        assert outcome.is_visible_response is False
        assert outcome.mark_handled is False

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_cancelled_visible_note_survives(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Visible cancellation artifacts must not mark the source as handled."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()

        room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=bot.matrix_id)
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                    thread_start_root_event_id=event.event_id,
                )
            ),
            correlation_id="corr-visible-cancel-note",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        with (
            patch.object(
                bot._response_runner,
                "generate_response",
                new=AsyncMock(return_value="$cancelled"),
            ),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )
        tracker.record_handled_turn.assert_called_once_with(
            replace(TurnRecord.create([event.event_id]), response_event_id="$cancelled"),
        )

    @pytest.mark.asyncio
    async def test_streamed_regeneration_against_an_existing_visible_reply_preserves_linkage_when_no_new_body_lands(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A no-op streamed regeneration should keep the prior visible reply linked."""

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            if False:
                yield "chunk"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        with (
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=MagicMock(return_value=mock_streaming_response()),
            ),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
        ):
            mock_send_streaming_response.return_value = StreamTransportOutcome(
                last_physical_stream_event_id="$existing",
                terminal_status="completed",
                rendered_body=None,
                visible_body_state="none",
            )
            generation = await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please reply in thread",
                    reply_to_event_id="$event456",
                    thread_id=None,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=_hook_envelope(
                        body="Please reply in thread",
                        source_event_id="$event456",
                    ),
                    correlation_id="corr-stream-regenerate-noop",
                    existing_event_id="$existing",
                    existing_event_is_placeholder=False,
                ),
            )

        assert generation.delivery.terminal_status == "completed"
        assert _visible_response_event_id(generation.delivery) == "$existing"
        assert _handled_response_event_id(generation.delivery) == "$existing"
        assert generation.delivery.mark_handled is True

    def test_response_outcome_prefers_terminal_status_over_delivery_kind(self) -> None:
        """Pipeline outcome summaries must not report cancelled or error states as plain send/edit success."""
        assert (
            _response_outcome_label(
                _outcome(
                    terminal_status="cancelled",
                    final_visible_event_id="$cancelled",
                    visible_response_event_id="$cancelled",
                    turn_completion_event_id="$cancelled",
                    final_visible_body="Cancelled.",
                    delivery_kind="edited",
                ),
            )
            == "cancelled"
        )
        assert (
            _response_outcome_label(
                _outcome(
                    terminal_status="error",
                    final_visible_event_id="$error",
                    visible_response_event_id="$error",
                    final_visible_body="boom",
                ),
            )
            == "error"
        )

    @pytest.mark.asyncio
    async def test_non_streaming_hidden_tool_calls_do_not_send_tool_trace(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Hidden tool calls should not propagate structured tool metadata."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        show_tool_calls=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            collector = kwargs["tool_trace_collector"]
            collector.append(
                ToolTraceEntry(
                    type="tool_call_completed",
                    tool_name="read_file",
                    args_preview="path=README.md",
                ),
            )
            return "Hidden tool call output"

        mock_ai = AsyncMock(side_effect=fake_ai_response)
        with patch_response_runner_module(
            typing_indicator=noop_typing_indicator,
            ai_response=mock_ai,
        ):
            generation = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Summarize README",
                    reply_to_event_id="$event",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Summarize README",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert generation.delivery.event_id == "$response"
        assert mock_ai.call_args.kwargs["show_tool_calls"] is False
        assert mock_ai.call_args.kwargs["collect_streamed_response"] is False
        assert "io.mindroom.tool_trace" not in bot.client.room_send.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_non_streaming_visible_tool_calls_are_passed_to_ai_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Offline/non-streaming runs should let ai_response use the stream-equivalent path."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        show_tool_calls=True,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert kwargs["show_tool_calls"] is True
            assert kwargs["collect_streamed_response"] is True
            collector = kwargs["tool_trace_collector"]
            collector.append(
                ToolTraceEntry(
                    type="tool_call_completed",
                    tool_name="run_shell_command",
                    args_preview="cmd=git status",
                    result_preview="clean",
                ),
            )
            return "Final answer"

        with patch_response_runner_module(
            typing_indicator=noop_typing_indicator,
            ai_response=AsyncMock(side_effect=fake_ai_response),
        ):
            generation = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Check status",
                    reply_to_event_id="$event",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check status",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert generation.delivery.event_id == "$response"
        content = bot.client.room_send.await_args.kwargs["content"]
        assert content["body"] == "Final answer"
        assert content[TOOL_TRACE_CONTENT_KEY]["events"] == [
            {
                "type": "tool_call_completed",
                "tool_name": "run_shell_command",
                "args_preview": "cmd=git status",
                "result_preview": "clean",
            },
        ]

    @pytest.mark.asyncio
    async def test_generate_response_passes_structured_user_turn_time(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Top-level response generation should preserve current-turn time as request data."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function(None)
            return "$response"

        scheduled_tasks: list[asyncio.Task[None]] = []

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        config.timezone = "America/Los_Angeles"
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        prior_user_time = datetime(2026, 3, 10, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        prior_agent_time = datetime(2026, 3, 10, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        current_turn_time = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
        thread_history = [
            _visible_message(
                sender="@alice:localhost",
                body="Earlier user question",
                timestamp=int(prior_user_time.timestamp() * 1000),
                event_id="$user1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Existing agent reply",
                timestamp=int(prior_agent_time.timestamp() * 1000),
                event_id="$agent1",
            ),
        ]

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$response",
                            is_visible_response=True,
                            final_visible_body="ok",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="What time is it?",
                    thread_history=thread_history,
                    user_id="@alice:localhost",
                    current_timestamp_ms=int(current_turn_time.timestamp() * 1000),
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="What time is it?",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        request = mock_process.await_args.args[0]
        assert request.prompt == "What time is it?"
        # model_prompt carries only model-side additions past the raw prompt.
        assert request.model_prompt == ""
        assert request.current_timestamp_ms == int(current_turn_time.timestamp() * 1000)
        assert request.thread_history[0].body == "Earlier user question"
        assert request.thread_history[1].body == "Existing agent reply"

    @pytest.mark.asyncio
    async def test_generate_response_keeps_memory_inputs_unprefixed(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Memory storage should receive the raw conversation, not the model-prefixed version."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function(None)
            return "$response"

        scheduled_tasks: list[asyncio.Task[None]] = []
        stored_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def fake_store_conversation_memory(*args: object, **kwargs: object) -> None:
            stored_calls.append((args, kwargs))

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        config.memory.backend = "mem0"
        config.timezone = "America/Los_Angeles"
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        bob_time = datetime(2026, 3, 10, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        alice_time = datetime(2026, 3, 10, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        agent_time = datetime(2026, 3, 10, 8, 14, tzinfo=ZoneInfo("America/Los_Angeles"))
        current_turn_time = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
        thread_history = [
            _visible_message(
                sender="@bob:localhost",
                body="Bob question",
                timestamp=int(bob_time.timestamp() * 1000),
                event_id="$bob1",
            ),
            _visible_message(
                sender="@alice:localhost",
                body="Alice earlier",
                timestamp=int(alice_time.timestamp() * 1000),
                event_id="$alice1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Existing agent reply",
                timestamp=int(agent_time.timestamp() * 1000),
                event_id="$agent1",
            ),
        ]

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$response",
                            is_visible_response=True,
                            final_visible_body="ok",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                create_background_task=schedule_background_task,
                store_conversation_memory=fake_store_conversation_memory,
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="What time is it?",
                    thread_history=thread_history,
                    user_id="@alice:localhost",
                    current_timestamp_ms=int(current_turn_time.timestamp() * 1000),
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="What time is it?",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        request = mock_process.await_args.args[0]
        assert request.prompt == "What time is it?"
        # model_prompt carries only model-side additions past the raw prompt.
        assert request.model_prompt == ""
        assert request.current_timestamp_ms == int(current_turn_time.timestamp() * 1000)
        assert request.thread_history[0].body == "Bob question"
        assert request.thread_history[1].body == "Alice earlier"
        assert request.thread_history[2].body == "Existing agent reply"

        assert len(stored_calls) == 1
        store_args, _ = stored_calls[0]
        assert store_args[0] == "What time is it?"
        assert store_args[6] == thread_history
        assert store_args[7] == "@alice:localhost"

    @pytest.mark.asyncio
    async def test_generate_response_marks_fresh_thinking_message_as_adopted_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming generation should flag fresh thinking placeholders for adoption."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function("$thinking")
            return "$thinking"

        scheduled_tasks: list[asyncio.Task[None]] = []

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond_streaming",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$thinking",
                            is_visible_response=True,
                            final_visible_body="",
                            delivery_kind="edited",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                create_background_task=schedule_background_task,
                store_conversation_memory=fake_store_conversation_memory,
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Continue",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        request = mock_process.await_args.args[0]
        assert request.existing_event_id == "$thinking"
        assert request.existing_event_is_placeholder is True

    @pytest.mark.asyncio
    async def test_generate_response_refreshes_thread_history_after_lock(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Queued turns should replace stale pending history with a fresh post-lock snapshot."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_function = cast("Callable[[str | None], Awaitable[None]]", kwargs["response_function"])
            await response_function(None)
            return "$response"

        stale_history = [
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Thinking...",
                event_id="$stale",
                timestamp=1,
                content={"body": "Thinking...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
            ),
        ]
        fresh_history = [
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Completed",
                event_id="$stale",
                timestamp=1,
                content={"body": "Completed", STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
        ]

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        async def cached_history_refresh(
            _room_id: str,
            _thread_id: str,
            *,
            caller_label: str,
        ) -> ThreadHistoryResult:
            assert caller_label == "dispatch_post_lock_refresh"
            return ThreadHistoryResult(fresh_history, is_full_history=True)

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$response",
                            is_visible_response=True,
                            final_visible_body="ok",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(
                bot._conversation_cache,
                "get_strict_thread_history",
                new=AsyncMock(side_effect=cached_history_refresh),
            ) as mock_get_thread_history,
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                reprioritize_auto_flush_sessions=MagicMock(),
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            async with bot._conversation_resolver.turn_thread_cache_scope():
                resolution = await bot._response_runner.generate_response(
                    ResponseRequest(
                        prompt="Continue",
                        thread_history=stale_history,
                        user_id="@alice:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event",
                            thread_id="$thread",
                            prompt="Continue",
                            user_id="@alice:localhost",
                            agent_name=bot.agent_name,
                        ),
                    ),
                )

        assert _handled_response_event_id(resolution) == "$response"
        mock_get_thread_history.assert_awaited_once_with(
            "!test:localhost",
            "$thread",
            caller_label="dispatch_post_lock_refresh",
        )
        request = mock_process.await_args.args[0]
        assert list(request.thread_history) == fresh_history
        assert request.thread_history[0].stream_status == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_generate_response_uses_resolved_thread_root_for_thinking_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thinking placeholders should use the canonical thread root from the response envelope."""
        scheduled_tasks: list[asyncio.Task[None]] = []
        sent_contents: list[dict[str, object]] = []

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        async def record_send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> DeliveredMatrixEvent:
            sent_contents.append(content)
            return delivered_matrix_event("$thinking", content)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id=None,
                reply_to_event_id="$reply_plain:localhost",
                thread_start_root_event_id="$thread_root:localhost",
            ),
            body="Continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=mock_agent_user.agent_name,
            origin=message_origin(
                sender_id="@alice:localhost",
                requester_id="@alice:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$thinking",
                            is_visible_response=True,
                            final_visible_body="ok",
                            delivery_kind="edited",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new=AsyncMock(return_value="$latest:localhost"),
            ),
            patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=record_send)),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=envelope,
                    correlation_id="$request:localhost",
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert len(sent_contents) == 1
        content = sent_contents[0]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"

    @pytest.mark.asyncio
    async def test_generate_response_queues_thread_summary_for_threaded_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Threaded agent replies should queue summary generation once the threshold is reached."""

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            kwargs["turn_recorder"].mark_completed()
            return "ok"

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._knowledge_access_support.resolve_for_agent = MagicMock(return_value=_KnowledgeResolution(knowledge=None))
        thread_history = [
            _visible_message(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                event_id=f"$message{i}",
                timestamp=i,
            )
            for i in range(4)
        ]

        with (
            patch("mindroom.response_runner.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.ai_response", new_callable=AsyncMock, side_effect=fake_ai_response),
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$response")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ) as mock_get_thread_history,
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="Summarize this thread",
                    thread_history=thread_history,
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="Summarize this thread",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert mock_get_thread_history.await_count >= 1
        assert all(
            await_args.args == ("!test:localhost", "$thread") for await_args in mock_get_thread_history.await_args_list
        )
        mock_thread_summary.assert_awaited_once_with(
            client=bot.client,
            room_id="!test:localhost",
            thread_id="$thread",
            config=config,
            runtime_paths=bot.runtime_paths,
            conversation_cache=bot._conversation_cache,
            entity_name=bot.agent_name,
        )
        assert "thread_summary_!test:localhost_$thread" in scheduled_names

    @pytest.mark.asyncio
    async def test_generate_response_keeps_first_turn_follow_up_effects_in_new_thread(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """First-turn threaded replies should keep compaction notices and summaries in the resolved thread."""

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            await kwargs["compaction_lifecycle"].start(
                CompactionLifecycleStart(
                    mode="auto",
                    session_id="session-1",
                    scope="agent:test_agent",
                    summary_model="summary-model",
                    before_tokens=30_000,
                    history_budget_tokens=100_000,
                    runs_before=20,
                ),
            )
            kwargs["turn_recorder"].mark_completed()
            return "ok"

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = self._config_for_storage(tmp_path)
        config.defaults.thread_summary_first_threshold = 1
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._knowledge_access_support.resolve_for_agent = MagicMock(return_value=_KnowledgeResolution(knowledge=None))
        root_event_id = "$root_event"
        resolved_target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id=root_event_id,
        ).with_thread_root(root_event_id)
        scope = HistoryScope(kind="agent", scope_id=bot.agent_name)
        storage = bot._conversation_state_writer.create_storage(None, scope=scope)
        try:
            session = AgentSession(session_id=resolved_target.session_id, created_at=1, updated_at=1)
            write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
            storage.upsert_session(session)
        finally:
            storage.close()
        response_envelope = replace(_hook_envelope(source_event_id=root_event_id), target=resolved_target)

        with (
            patch("mindroom.response_runner.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.ai_response", new_callable=AsyncMock, side_effect=fake_ai_response),
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$response")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.send_compaction_lifecycle_start",
                new=AsyncMock(return_value="$notice"),
            ) as mock_send_compaction_lifecycle_start,
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="Start a thread here",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=response_envelope,
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        mock_thread_summary.assert_awaited_once_with(
            client=bot.client,
            room_id="!test:localhost",
            thread_id=root_event_id,
            config=config,
            runtime_paths=bot.runtime_paths,
            conversation_cache=bot._conversation_cache,
            entity_name=bot.agent_name,
        )
        mock_send_compaction_lifecycle_start.assert_awaited_once()
        compaction_notice_kwargs = mock_send_compaction_lifecycle_start.await_args.kwargs
        assert compaction_notice_kwargs["target"].resolved_thread_id == root_event_id
        assert compaction_notice_kwargs["reply_to_event_id"] == root_event_id
        assert "thread_summary_!test:localhost_$root_event" in scheduled_names

    @pytest.mark.asyncio
    async def test_generate_response_marks_non_streaming_model_error_unsuccessful_for_post_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delivered non-streaming Matrix error reply should not be a successful run outcome."""
        captured_outcomes: list[ResponseOutcome] = []

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert {"post" + "_response_compaction_checks_collector"}.isdisjoint(kwargs)
            return "friendly-error"

        async def fake_apply_post_response_effects(
            _delivery: FinalDeliveryOutcome,
            outcome: ResponseOutcome,
            _deps: object,
        ) -> None:
            captured_outcomes.append(outcome)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
            ai_response=AsyncMock(side_effect=fake_ai_response),
            apply_post_response_effects=AsyncMock(side_effect=fake_apply_post_response_effects),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="Please answer",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Please answer",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        assert len(captured_outcomes) == 1

    @pytest.mark.asyncio
    async def test_generate_response_marks_streaming_model_error_unsuccessful_for_post_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delivered streaming Matrix error reply should not be a successful run outcome."""
        captured_outcomes: list[ResponseOutcome] = []

        async def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            assert {"post" + "_response_compaction_checks_collector"}.isdisjoint(kwargs)
            yield "friendly-error"

        async def fake_send_streaming_response(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            response_stream = cast("AsyncGenerator[object, None]", args[4])
            body_parts = [str(chunk) async for chunk in response_stream]
            return _stream_outcome("$response", "".join(body_parts))

        async def fake_apply_post_response_effects(
            _delivery: FinalDeliveryOutcome,
            outcome: ResponseOutcome,
            _deps: object,
        ) -> None:
            captured_outcomes.append(outcome)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(side_effect=fake_send_streaming_response),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=True),
                stream_agent_response=MagicMock(side_effect=fake_stream_agent_response),
                apply_post_response_effects=AsyncMock(side_effect=fake_apply_post_response_effects),
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    prompt="Please answer",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Please answer",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        assert len(captured_outcomes) == 1

    @pytest.mark.asyncio
    async def test_generate_response_runs_post_effects_after_cancellable_wrapper(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Late cancellation should not skip agent post-response cleanup after delivery."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_function = cast("Callable[[str | None], Awaitable[None]]", kwargs["response_function"])
            await response_function(None)
            return "$response"

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_post_effects(*_args: object, **_kwargs: object) -> None:
            started.set()
            await release.wait()

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        history = _empty_full_thread_history()

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$response",
                            is_visible_response=True,
                            final_visible_body="ok",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ),
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.response_lifecycle.apply_post_response_effects",
                new=AsyncMock(side_effect=fake_post_effects),
            ),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        prompt="Summarize this thread",
                        thread_history=[],
                        user_id="@alice:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event",
                            thread_id="$thread",
                            prompt="Summarize this thread",
                            user_id="@alice:localhost",
                            agent_name=bot.agent_name,
                        ),
                    ),
                ),
            )
            await started.wait()
            task.cancel()
            release.set()
            resolution = await task

        assert _handled_response_event_id(resolution) == "$response"
