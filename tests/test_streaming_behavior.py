"""Comprehensive unit tests for streaming behavior with agent edits."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest
from agno.models.response import ToolExecution
from agno.run.agent import ToolCallCompletedEvent, ToolCallStartedEvent
from pydantic import ValidationError

from mindroom import interactive
from mindroom.bot import AgentBot
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, USER_STOP_CANCEL_MSG, CancelSource
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig, StreamingConfig
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
)
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    FinalizeStreamedResponseRequest,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history.interrupted_replay import (
    _INTERRUPTED_RESPONSE_MARKER,
    InterruptedReplaySnapshot,
    render_interrupted_replay_content,
)
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.large_messages import _clear_oversized_nonterminal_streaming_edit_rate_limits
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.response_lifecycle import ResponseLifecycle, ResponseLifecycleDeps
from mindroom.response_runner import ResponseRequest
from mindroom.streaming import (
    CANCELLED_RESPONSE_NOTE,
    INTERRUPTED_RESPONSE_NOTE,
    PROGRESS_PLACEHOLDER,
    ReplacementStreamingResponse,
    StreamingDeliveryError,
    StreamingResponse,
    build_restart_interrupted_body,
    clean_partial_reply_text,
    is_interrupted_partial_reply,
    send_streaming_response,
)
from mindroom.streaming_delivery import (
    _consume_streaming_chunks as _consume_streaming_chunks_impl,
)
from mindroom.streaming_delivery import (
    _DeliveryRequest,
    _drive_stream_delivery,
    _flush_phase_boundary_if_needed,
    _shutdown_stream_delivery,
    _StreamDeliveryShutdownTimeoutError,
)
from mindroom.timing import DispatchPipelineTiming
from mindroom.tool_system.runtime_context import WorkerProgressEvent, get_worker_progress_pump
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    patch_response_runner_module,
    replace_response_runner_deps,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

IN_PROGRESS_MARKER = " ⋯"


async def _aiter(*events: object) -> AsyncIterator[object]:
    for event in events:
        yield event


def _render_cleaned_interrupted_replay(body: str) -> str:
    return render_interrupted_replay_content(
        InterruptedReplaySnapshot(
            user_message="",
            partial_text=clean_partial_reply_text(body),
            completed_tools=(),
            interrupted_tools=(),
            seen_event_ids=(),
            source_event_id=None,
            source_event_ids=(),
            source_event_prompts=(),
            response_event_id=None,
        ),
    )


def _make_matrix_client_mock() -> AsyncMock:
    client = make_matrix_client_mock(user_id="@mindroom_streaming:localhost")
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    return client


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def _make_bot_with_shared_knowledge(
    *,
    tmp_path: Path,
    agent: AgentMatrixUser,
    base_id: str,
) -> AgentBot:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="HelperAgent",
                    rooms=["!test:localhost"],
                    knowledge_bases=[base_id],
                ),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
            knowledge_bases={base_id: {"path": f"./{base_id}"}},
        ),
        runtime_paths,
    )
    bot = AgentBot(
        agent,
        tmp_path,
        rooms=["!test:localhost"],
        enable_streaming=True,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_cache_support(bot)
    bot.client = _make_matrix_client_mock()
    return bot


async def _consume_streaming_chunks_for_test(
    client: nio.AsyncClient,
    response_stream: AsyncIterator[object],
    streaming: StreamingResponse,
) -> None:
    delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
    delivery_task = asyncio.create_task(_drive_stream_delivery(client, streaming, delivery_queue))
    try:
        await _consume_streaming_chunks_impl(response_stream, streaming, delivery_queue)
        shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
        delivery_task = None
        if shutdown_error is not None:
            raise shutdown_error
    finally:
        if delivery_task is not None:
            cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            if cleanup_error is not None:
                raise cleanup_error


@pytest.fixture
def mock_helper_agent() -> AgentMatrixUser:
    """Create a mock helper agent user."""
    return AgentMatrixUser(
        agent_name="helper",
        password=TEST_PASSWORD,
        display_name="HelperAgent",
        user_id="@mindroom_helper:localhost",
    )


@pytest.fixture
def mock_calculator_agent() -> AgentMatrixUser:
    """Create a mock calculator agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password=TEST_PASSWORD,
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


class TestStreamingBehavior:
    """Test the complete streaming behavior including agent interactions."""

    def setup_method(self) -> None:
        """Set up test config."""
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        self.config = bind_runtime_paths(
            Config(
                agents={
                    "helper": AgentConfig(display_name="HelperAgent", rooms=["!test:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            runtime_paths,
        )

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.ai_response")
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.response_runner.should_use_streaming")
    async def test_streaming_agent_mentions_another_agent(  # noqa: PLR0915
        self,
        mock_should_use_streaming: AsyncMock,
        mock_stream_agent_response: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_helper_agent: AgentMatrixUser,
        mock_calculator_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test complete flow of one agent streaming and mentioning another."""

        # Configure streaming - helper will stream, calculator won't
        def side_effect(
            client: object,
            room_id: str,
            requester_user_id: str | None = None,
            enable_streaming: bool = True,
        ) -> bool:
            _ = (client, room_id, enable_streaming)
            # Helper streams when mentioned by user
            return requester_user_id == "@user:localhost"

        mock_should_use_streaming.side_effect = side_effect

        # Set up helper bot (the one that will stream)
        config = self.config

        helper_bot = AgentBot(
            mock_helper_agent,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=True,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        install_runtime_cache_support(helper_bot)
        helper_bot.client = _make_matrix_client_mock()

        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        helper_bot.orchestrator = mock_orchestrator

        # Set up calculator bot (the one that will be mentioned)
        config = self.config

        calc_bot = AgentBot(
            mock_calculator_agent,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        install_runtime_cache_support(calc_bot)
        calc_bot.client = _make_matrix_client_mock()

        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        calc_bot.orchestrator = mock_orchestrator

        # Mock successful room_send responses
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$helper_response_123"
        helper_bot.client.room_send.return_value = mock_send_response
        calc_bot.client.room_send.return_value = mock_send_response

        # Mock AI responses
        mock_ai_response.return_value = "4"

        # Create a generator that yields the streaming response
        async def streaming_generator() -> AsyncIterator[str]:
            yield "Let me help with that calculation. "
            yield "@mindroom_calculator:localhost what's 2+2?"

        mock_stream_agent_response.return_value = streaming_generator()

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
            },
        }

        # Mock that we're mentioned
        with patch("mindroom.conversation_resolver.check_agent_mentioned") as mock_check:
            mock_check.return_value = ([MatrixID.parse("@mindroom_helper:localhost")], True, False)

            # Process message with helper bot - it should stream a response
            await helper_bot._on_message(mock_room, user_event)

        # Verify helper bot sent initial message and edit
        assert helper_bot.client.room_send.call_count >= 1  # At least initial message

        # Simulate the initial message from helper while the stream is still active.
        initial_event = MagicMock(spec=nio.RoomMessageText)
        initial_event.sender = "@mindroom_helper:localhost"
        initial_event.body = "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?"
        initial_event.event_id = "$helper_response_123"
        initial_event.server_timestamp = 1234567890
        initial_event.source = {
            "content": {
                "body": "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
            },
        }

        # Process initial message - calculator should NOT respond while the stream is active.
        with patch("mindroom.conversation_resolver.check_agent_mentioned") as mock_check:
            mock_check.return_value = ([MatrixID.parse("@mindroom_calculator:localhost")], True, False)

            # Debug: let's see what happens
            calc_bot.logger.info("processing_initial_message", body=initial_event.body)

            # Add more logging to understand the flow
            with patch("mindroom.bot.extract_agent_name") as mock_extract:
                # Make extract_agent_name return 'helper' for the sender
                mock_extract.return_value = "helper"

                await calc_bot._on_message(mock_room, initial_event)

        assert calc_bot.client.room_send.call_count == 0
        assert mock_ai_response.call_count == 0  # Calculator didn't process anything

        # Now simulate the final message
        final_event = MagicMock(spec=nio.RoomMessageText)
        final_event.sender = "@mindroom_helper:localhost"
        final_event.body = "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?"
        final_event.event_id = "$helper_final"
        final_event.server_timestamp = 1234567891
        final_event.source = {
            "content": {
                "body": "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Process final message - calculator SHOULD respond now
        with patch("mindroom.conversation_resolver.check_agent_mentioned") as mock_check:
            mock_check.return_value = ([MatrixID.parse("@mindroom_calculator:localhost")], True, False)
            with patch("mindroom.bot.extract_agent_name") as mock_extract:
                # Make extract_agent_name return 'helper' for the sender
                mock_extract.return_value = "helper"
                await calc_bot._on_message(mock_room, final_event)

        assert calc_bot.client.room_send.call_count == 2  # thinking + final
        assert mock_ai_response.call_count == 1

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.ai_response")
    async def test_agent_responds_only_to_final_message(
        self,
        mock_ai_response: AsyncMock,
        mock_helper_agent: AgentMatrixUser,  # noqa: ARG002
        mock_calculator_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents respond to the final complete message, not edits."""
        # Set up calculator bot
        config = self.config

        calc_bot = AgentBot(
            mock_calculator_agent,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        install_runtime_cache_support(calc_bot)
        calc_bot.client = _make_matrix_client_mock()

        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        calc_bot.orchestrator = mock_orchestrator

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
            },
        }

        # Process initial message - calculator SHOULD respond
        await calc_bot._on_message(mock_room, initial_event)
        assert calc_bot.client.room_send.call_count == 2  # thinking + final
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
            },
        }

        # Process edit - calculator should NOT respond again
        await calc_bot._on_message(mock_room, edit_event)
        assert calc_bot.client.room_send.call_count == 0
        assert mock_ai_response.call_count == 0

    @pytest.mark.asyncio
    async def test_streaming_response_flow(
        self,
        mock_helper_agent: AgentMatrixUser,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test the StreamingResponse class behavior."""
        # Create a mock client
        mock_client = _make_matrix_client_mock()
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$stream_123"
        mock_client.room_send.return_value = mock_send_response

        # Create streaming response
        config = self.config
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
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

        # Simulate time passing (lower interval to speed up test)
        streaming.update_interval = 0.05
        await asyncio.sleep(0.06)

        # Add more content after delay
        await streaming.update_content("!", mock_client)
        assert streaming.accumulated_text == "Hello world!"
        # NOW it should send an edit
        assert mock_client.room_send.call_count == 2

        # Force finalize
        await streaming.finalize(mock_client)
        # Should send final edit
        assert mock_client.room_send.call_count >= 2

        # Check the final content
        assert streaming.accumulated_text == "Hello world!"

        # Check the edit content
        last_call = mock_client.room_send.call_args_list[-1]
        content = last_call[1]["content"]
        assert content["m.relates_to"]["rel_type"] == "m.replace"
        assert content["m.relates_to"]["event_id"] == "$stream_123"

    @pytest.mark.asyncio
    async def test_oversized_nonterminal_sidecar_edits_are_rate_limited(self) -> None:
        """Oversized in-progress edits should not burst sidecar uploads while final still sends."""
        _clear_oversized_nonterminal_streaming_edit_rate_limits()
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id="$thread_123",
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.event_id = "$stream_123"
        streaming.accumulated_text = "x" * 40000

        monotonic_values = iter([100.0, 101.0, 106.0])

        async def delivered_edit(
            _client: nio.AsyncClient,
            _room_id: str,
            _event_id: str,
            new_content: dict[str, object],
            _new_text: str,
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

        with (
            patch(
                "mindroom.matrix.large_messages.monotonic",
                side_effect=lambda: next(monotonic_values),
            ),
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=delivered_edit)) as mock_edit,
        ):
            assert await streaming._send_or_edit_message(mock_client)
            assert mock_edit.await_count == 1

            streaming.accumulated_text += "y"
            streaming._mark_nonadditive_text_mutation()
            assert await streaming._send_or_edit_message(mock_client)
            assert mock_edit.await_count == 1

            streaming.accumulated_text += "z"
            streaming._mark_nonadditive_text_mutation()
            assert await streaming._send_or_edit_message(mock_client)
            assert mock_edit.await_count == 2

            streaming.accumulated_text += " final"
            assert await streaming._send_or_edit_message(
                mock_client,
                is_final=True,
                stream_status=STREAM_STATUS_COMPLETED,
            )
            assert mock_edit.await_count == 3

    @pytest.mark.asyncio
    async def test_rate_limited_oversized_nonterminal_edit_resolves_capture_completion(self) -> None:
        """Skipping an oversized in-progress edit should still unblock capture waiters."""
        _clear_oversized_nonterminal_streaming_edit_rate_limits()
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id="$thread_123",
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.event_id = "$stream_123"
        streaming.accumulated_text = "x" * 40000
        streaming._send_content = AsyncMock(return_value=True)
        capture_completion = asyncio.get_running_loop().create_future()

        with patch("mindroom.matrix.large_messages.monotonic", side_effect=[100.0, 101.0]):
            assert await streaming._send_or_edit_message(mock_client)
            streaming.accumulated_text += "y"
            streaming._mark_nonadditive_text_mutation()
            assert await streaming._send_or_edit_message(
                mock_client,
                capture_completions=(capture_completion,),
            )

        assert streaming._send_content.await_count == 1
        assert capture_completion.done()
        assert capture_completion.result() is None

    def test_streaming_update_interval_starts_fast_then_slows(self) -> None:
        """Test progressive throttling: frequent edits first, slower later."""
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            min_update_interval=0.5,
            interval_ramp_seconds=15.0,
        )
        streaming.stream_started_at = 100.0

        start = streaming._current_update_interval(100.0)
        mid = streaming._current_update_interval(107.5)
        end = streaming._current_update_interval(115.0)
        after = streaming._current_update_interval(130.0)

        assert start == pytest.approx(0.5)
        assert mid == pytest.approx(2.75)
        assert end == pytest.approx(5.0)
        assert after == pytest.approx(5.0)

    def test_streaming_char_threshold_starts_small_then_grows(self) -> None:
        """Character trigger should ramp from a low threshold to steady-state."""
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_char_threshold=180,
            min_update_char_threshold=30,
            interval_ramp_seconds=15.0,
        )
        streaming.stream_started_at = 100.0

        start = streaming._current_char_threshold(100.0)
        mid = streaming._current_char_threshold(107.5)
        end = streaming._current_char_threshold(115.0)
        after = streaming._current_char_threshold(130.0)

        assert start == 30
        assert mid == 105
        assert end == 180
        assert after == 180

    def test_replacement_streaming_tracks_chars_since_last_update(self) -> None:
        """Replacement streams should still advance char-trigger counters."""
        streaming = ReplacementStreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )

        streaming._update("abc")
        streaming._update("abcdef")

        assert streaming.accumulated_text == "abcdef"
        assert streaming.chars_since_last_update == 9

    def test_stream_started_at_not_set_before_first_send(self) -> None:
        """Test that stream_started_at is None until first _throttled_send."""
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        assert streaming.stream_started_at is None
        # Before stream starts, ramp is inactive so steady-state interval is returned
        assert streaming._current_update_interval(999.0) == streaming.update_interval

    def test_is_interrupted_partial_reply_detects_terminal_markers(self) -> None:
        """Interrupted partial-reply detection should recognize shared cancelled/error notes."""
        assert is_interrupted_partial_reply(f"Draft answer\n\n{CANCELLED_RESPONSE_NOTE}")
        assert is_interrupted_partial_reply("Draft answer\n\n**[Response interrupted by an error: boom]**")
        assert not is_interrupted_partial_reply("Finished answer")
        assert not is_interrupted_partial_reply(None)

    def test_is_interrupted_partial_reply_recognises_all_three_variants(self) -> None:
        """Interrupted partial-reply detection should recognize every live cancel note."""
        assert is_interrupted_partial_reply(f"Draft answer\n\n{CANCELLED_RESPONSE_NOTE}")
        assert is_interrupted_partial_reply(f"Draft answer\n\n{INTERRUPTED_RESPONSE_NOTE}")
        assert is_interrupted_partial_reply(build_restart_interrupted_body("Draft answer"))

    def test_clean_partial_reply_text_strips_shared_markers(self) -> None:
        """Shared partial-reply cleanup should normalize cancelled/error/placeholder bodies."""
        assert clean_partial_reply_text(f"Draft answer\n\n{CANCELLED_RESPONSE_NOTE}") == "Draft answer"
        assert (
            clean_partial_reply_text("Draft answer\n\n**[Response interrupted by an error: boom]**") == "Draft answer"
        )
        assert clean_partial_reply_text(PROGRESS_PLACEHOLDER) == ""
        assert clean_partial_reply_text("...") == ""

    def test_clean_partial_reply_text_normalises_user_stop_label_to_interrupted_marker(self) -> None:
        """User-stop labels should collapse to the canonical interrupted replay marker."""
        assert _render_cleaned_interrupted_replay(f"Draft answer\n\n{CANCELLED_RESPONSE_NOTE}") == (
            f"Draft answer\n\n{_INTERRUPTED_RESPONSE_MARKER}"
        )

    def test_clean_partial_reply_text_normalises_restart_label_to_interrupted_marker(self) -> None:
        """Restart labels should collapse to the canonical interrupted replay marker."""
        assert _render_cleaned_interrupted_replay(build_restart_interrupted_body("Draft answer")) == (
            f"Draft answer\n\n{_INTERRUPTED_RESPONSE_MARKER}"
        )

    def test_clean_partial_reply_text_normalises_new_interrupted_label_to_interrupted_marker(self) -> None:
        """Generic interruption labels should collapse to the canonical interrupted replay marker."""
        assert _render_cleaned_interrupted_replay(f"Draft answer\n\n{INTERRUPTED_RESPONSE_NOTE}") == (
            f"Draft answer\n\n{_INTERRUPTED_RESPONSE_MARKER}"
        )

    @pytest.mark.asyncio
    async def test_throttled_send_uses_ramp_interval(self) -> None:
        """Integration test: _throttled_send respects the ramped interval."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$stream_456"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            min_update_interval=0.5,
            interval_ramp_seconds=15.0,
        )
        streaming.accumulated_text = "hello"

        # First call sets stream_started_at and sends immediately (last_update=0)
        await streaming._throttled_send(mock_client)
        assert streaming.stream_started_at is not None
        assert mock_client.room_send.call_count == 1

    @pytest.mark.asyncio
    async def test_char_threshold_can_trigger_before_time_interval(self) -> None:
        """Large enough text chunks should trigger an update even before time interval elapses."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$stream_char_1"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            update_char_threshold=5,
            min_update_char_threshold=5,
            min_char_update_interval=0.0,
        )
        streaming.last_update = time.time()

        await streaming.update_content("hello", mock_client)

        assert mock_client.room_send.call_count == 1
        assert streaming.event_id == "$stream_char_1"

    @pytest.mark.asyncio
    async def test_pre_tool_flush_fires_on_immediate_tool_start(self) -> None:
        """Tool-start phase boundaries should flush buffered text immediately in both visible and hidden modes."""

        async def run_case(*, show_tool_calls: bool) -> list[str]:
            mock_client = _make_matrix_client_mock()
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = "$stream_tool_1"
            mock_client.room_send.return_value = mock_response

            streaming = StreamingResponse(
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                update_interval=10.0,
                min_update_interval=10.0,
                interval_ramp_seconds=0.0,
                max_idle=0.25,
                show_tool_calls=show_tool_calls,
            )
            streaming.last_update = 100.0
            streaming.stream_started_at = 100.0

            async def response_stream() -> AsyncIterator[object]:
                yield "Hello, this is a partial wo"
                yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))

            with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0, 100.0, 100.0, 100.0]):
                await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)
            await streaming.finalize(mock_client)

            assert mock_client.room_send.call_count == 2
            return [
                call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
                for call in mock_client.room_send.call_args_list
            ]

        visible_bodies = await run_case(show_tool_calls=True)
        assert visible_bodies[0].startswith("Hello, this is a partial wo")
        assert "search_web" in visible_bodies[0]
        assert visible_bodies[1].startswith("Hello, this is a partial wo")
        assert "search_web" in visible_bodies[1]

        hidden_bodies = await run_case(show_tool_calls=False)
        assert hidden_bodies[0] == "Hello, this is a partial wo"
        assert hidden_bodies[1] == "Hello, this is a partial wo\n\n"

    @pytest.mark.asyncio
    async def test_pre_tool_flush_does_not_double_emit_after_idle_gap(self) -> None:
        """Tool-start after an idle gap should not emit an extra edit before finalization."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$stream_tool_gap_1"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=48,
            min_update_char_threshold=48,
            min_char_update_interval=0.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def response_stream() -> AsyncIterator[object]:
            yield "x" * 40
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0, 105.0, 105.0, 105.0]):
            await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)
        await streaming.finalize(mock_client)

        assert mock_client.room_send.call_count == 2
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert bodies[0].startswith("x" * 40)
        assert "search_web" in bodies[0]
        assert "search_web" in bodies[1]

    @pytest.mark.asyncio
    async def test_visible_tool_start_does_not_block_subsequent_stream_events_on_matrix_send(self) -> None:
        """Visible tool-start refreshes must not backpressure later stream events on Matrix I/O."""
        mock_client = _make_matrix_client_mock()
        first_send_started = asyncio.Event()
        allow_send_to_finish = asyncio.Event()
        second_yield_started = asyncio.Event()

        async def slow_room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            _ = (args, kwargs)
            first_send_started.set()
            await allow_send_to_finish.wait()
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = "$tool_start_backpressure"
            return mock_response

        mock_client.room_send.side_effect = slow_room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))
            second_yield_started.set()
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        consume_task = asyncio.create_task(_consume_streaming_chunks_impl(response_stream(), streaming, delivery_queue))

        await first_send_started.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert second_yield_started.is_set()

        allow_send_to_finish.set()
        await consume_task
        shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
        assert shutdown_error is None

    @pytest.mark.asyncio
    async def test_visible_tool_start_burst_coalesces_to_one_nonterminal_send(self) -> None:
        """Rapid visible tool-start bursts should not emit one Matrix edit per tool."""
        mock_client = _make_matrix_client_mock()
        first_send_started = asyncio.Event()
        allow_send_to_finish = asyncio.Event()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$tool_start_burst"

        async def slow_room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            _ = (args, kwargs)
            first_send_started.set()
            await allow_send_to_finish.wait()
            return mock_response

        mock_client.room_send.side_effect = slow_room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            min_char_update_interval=10.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        consume_task = asyncio.create_task(_consume_streaming_chunks_impl(response_stream(), streaming, delivery_queue))

        await first_send_started.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        allow_send_to_finish.set()
        await consume_task
        shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
        assert shutdown_error is None

        assert mock_client.room_send.call_count == 1
        assert "search_web" in streaming.accumulated_text
        assert "save_file" in streaming.accumulated_text

    @pytest.mark.asyncio
    async def test_visible_tool_start_arriving_during_inflight_refresh_gets_sent_before_finalize(self) -> None:
        """A later visible tool start must not wait for finalization when the earlier refresh is still in flight."""
        mock_client = _make_matrix_client_mock()
        first_send_started = asyncio.Event()
        allow_first_send_to_finish = asyncio.Event()
        send_count = 0

        async def slow_room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            nonlocal send_count
            _ = (args, kwargs)
            send_count += 1
            if send_count == 1:
                first_send_started.set()
                await allow_first_send_to_finish.wait()
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = f"$tool_start_inflight_{send_count}"
            return mock_response

        mock_client.room_send.side_effect = slow_room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=10.0,
            progress_update_interval=10.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))
            await first_send_started.wait()
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        consume_task = asyncio.create_task(_consume_streaming_chunks_impl(response_stream(), streaming, delivery_queue))

        await first_send_started.wait()
        await asyncio.sleep(0)
        allow_first_send_to_finish.set()
        await consume_task
        shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
        assert shutdown_error is None

        assert mock_client.room_send.call_count == 2
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert "search_web" in bodies[0]
        assert "save_file" in bodies[1]

    @pytest.mark.asyncio
    async def test_back_to_back_visible_tool_starts_refresh_immediately_without_intervening_text(self) -> None:
        """Back-to-back visible tool starts should both surface before finalization."""
        mock_client = _make_matrix_client_mock()
        send_count = 0

        async def room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            nonlocal send_count
            _ = (args, kwargs)
            send_count += 1
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = f"$tool_start_post_delta_{send_count}"
            return mock_response

        mock_client.room_send.side_effect = room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=10.0,
            progress_update_interval=10.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def first_response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))

        with (
            patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0]),
            patch("mindroom.streaming_delivery.time.time", return_value=100.0),
        ):
            await _consume_streaming_chunks_for_test(mock_client, first_response_stream(), streaming)

        async def second_response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        with (
            patch("mindroom.streaming.time.time", side_effect=[100.1, 100.1]),
            patch("mindroom.streaming_delivery.time.time", return_value=100.1),
        ):
            await _consume_streaming_chunks_for_test(mock_client, second_response_stream(), streaming)

        assert mock_client.room_send.call_count == 2
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert "search_web" in bodies[0]
        assert "save_file" in bodies[1]

    @pytest.mark.asyncio
    async def test_visible_tool_start_after_intervening_edit_refreshes_immediately(self) -> None:
        """A later visible tool start should refresh immediately once another edit has already landed."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$tool_start_after_text"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=1,
            min_update_char_threshold=1,
            min_char_update_interval=0.0,
            progress_update_interval=10.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = float("-inf")

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            yield "x"
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)

        assert mock_client.room_send.call_count == 3
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert "search_web" in bodies[0]
        assert "save_file" not in bodies[1]
        assert "save_file" in bodies[2]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("show_tool_calls", "streaming_cls", "event_id"),
        [
            pytest.param(False, StreamingResponse, "$hidden_tool_start_backpressure", id="hidden"),
            pytest.param(True, ReplacementStreamingResponse, "$visible_tool_start_backpressure", id="visible"),
        ],
    )
    async def test_tool_start_does_not_block_subsequent_stream_events_on_matrix_send(
        self,
        show_tool_calls: bool,
        streaming_cls: type[StreamingResponse],
        event_id: str,
    ) -> None:
        """Tool-start capture waits should not block later stream events while Matrix I/O is in flight."""
        mock_client = _make_matrix_client_mock()
        first_send_started = asyncio.Event()
        allow_send_to_finish = asyncio.Event()
        second_yield_started = asyncio.Event()

        async def slow_room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            _ = (args, kwargs)
            first_send_started.set()
            await allow_send_to_finish.wait()
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = event_id
            return mock_response

        mock_client.room_send.side_effect = slow_room_send

        streaming = streaming_cls(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=1,
            min_update_char_threshold=1,
            min_char_update_interval=0.0,
            max_idle=10.0,
            show_tool_calls=show_tool_calls,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def response_stream() -> AsyncIterator[object]:
            yield "x"
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))
            second_yield_started.set()
            yield "after"

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        consume_task = asyncio.create_task(_consume_streaming_chunks_impl(response_stream(), streaming, delivery_queue))

        await first_send_started.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert second_yield_started.is_set()

        allow_send_to_finish.set()
        await consume_task
        shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
        assert shutdown_error is None

    @pytest.mark.asyncio
    async def test_visible_tool_start_after_unsent_text_refreshes_immediately(self) -> None:
        """A same-tick visible tool start should still bypass the boundary refresh gate."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$tool_start_after_unsent_text"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=10.0,
            progress_update_interval=10.0,
            max_idle=10.0,
            show_tool_calls=True,
        )
        streaming.event_id = "$existing_event"
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0
        streaming.last_boundary_refresh_at = 100.0
        streaming.accumulated_text = "\n\n🔧 `search_web` [1] ⏳\nx\n\n🔧 `save_file` [2] ⏳\n"
        streaming.chars_since_last_update = len(streaming.accumulated_text)

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        delivery_queue.put_nowait(
            _DeliveryRequest(
                boundary_refresh=True,
                prior_delta_at=100.0,
                boundary_refresh_prior_delta_at=100.0,
            ),
        )

        with (
            patch("mindroom.streaming.time.time", return_value=100.0),
            patch("mindroom.streaming_delivery.time.time", return_value=100.0),
        ):
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert mock_client.room_send.call_count == 1
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert "search_web" in bodies[-1]
        assert "save_file" in bodies[-1]

    @pytest.mark.asyncio
    async def test_idle_flush_fires_on_tool_completion_after_pause(self) -> None:
        """A paused tool completion should emit before finalization via the idle trigger."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$stream_tool_complete_1"
        first_send_seen = asyncio.Event()

        async def room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            _ = (args, kwargs)
            first_send_seen.set()
            return mock_response

        mock_client.room_send.side_effect = room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.0,
            max_idle=0.25,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        tool = ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}, result="ok")

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=tool)
            await first_send_seen.wait()
            yield ToolCallCompletedEvent(tool=tool, content="ok")

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0, 100.0, 100.5, 100.5, 100.5]):
            await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)

        assert mock_client.room_send.call_count == 2
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert "⏳" in bodies[0]
        assert "⏳" not in bodies[1]

    @pytest.mark.asyncio
    async def test_idle_flush_fires_on_text_after_pause(self) -> None:
        """A long gap before the next text chunk should trigger an idle flush on that text event."""
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.0,
            max_idle=0.25,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0
        streaming._send_content = AsyncMock(return_value=True)

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0]):
            await streaming.update_content("first", mock_client)

        assert streaming._send_content.await_count == 0

        with patch("mindroom.streaming.time.time", side_effect=[105.0, 105.0, 105.0]):
            await streaming.update_content(" second", mock_client)

        assert streaming._send_content.await_count == 1
        assert streaming.last_delta_at == 105.0
        assert streaming.chars_since_last_update == 0

    @pytest.mark.asyncio
    async def test_phase_boundary_flush_ignores_whitespace_only_buffer(self) -> None:
        """Whitespace-only pre-tool flushes should not consume the first visible send window."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$stream_whitespace_1"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=10.0,
            max_idle=0.25,
        )
        streaming.last_update = float("-inf")

        with patch("mindroom.streaming.time.time", return_value=100.0):
            streaming._update("\n")

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        await _flush_phase_boundary_if_needed(streaming, delivery_queue)

        with (
            patch("mindroom.streaming.time.time", return_value=101.0),
            patch("mindroom.streaming_delivery.time.time", return_value=101.0),
        ):
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert mock_client.room_send.call_count == 0
        assert streaming.last_update == float("-inf")
        assert streaming.chars_since_last_update == 1

        with patch("mindroom.streaming.time.time", side_effect=[102.0, 102.0, 102.0]):
            await streaming.update_content("Hi", mock_client)

        assert mock_client.room_send.call_count == 1

    @pytest.mark.asyncio
    async def test_stale_nonterminal_send_does_not_strand_newer_text(self) -> None:
        """A stale send must not reset throttling state for a newer buffered chunk."""
        mock_client = _make_matrix_client_mock()
        first_send_started = asyncio.Event()
        allow_first_send_to_finish = asyncio.Event()
        second_yield_started = asyncio.Event()
        send_count = 0

        async def room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            nonlocal send_count
            _ = (args, kwargs)
            send_count += 1
            if send_count == 1:
                first_send_started.set()
                await allow_first_send_to_finish.wait()
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = f"$stale_send_{send_count}"
            return mock_response

        mock_client.room_send.side_effect = room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_char_threshold=1,
            min_char_update_interval=0.35,
            max_idle=0.25,
        )
        streaming.last_update = float("-inf")

        async def response_stream() -> AsyncIterator[object]:
            yield "A"
            await first_send_started.wait()
            second_yield_started.set()
            yield "B"

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        streaming_times = iter([100.0, 100.0, 100.1, 100.2, 100.2, 100.2])

        with patch("mindroom.streaming.time.time", side_effect=lambda: next(streaming_times, 100.2)):
            consume_task = asyncio.create_task(
                _consume_streaming_chunks_impl(response_stream(), streaming, delivery_queue),
            )
            await first_send_started.wait()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert second_yield_started.is_set()
            allow_first_send_to_finish.set()
            await consume_task
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert mock_client.room_send.call_count == 2
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert bodies[0] == "A"
        assert bodies[1] == "AB"

    @pytest.mark.asyncio
    async def test_boundary_refresh_initializes_stream_started_at(self) -> None:
        """Boundary-refresh sends should anchor the ramp window before later throttled edits."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$boundary_refresh_start"
        mock_client.room_send.return_value = mock_response
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.accumulated_text = "hello"
        streaming.chars_since_last_update = len(streaming.accumulated_text)

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        delivery_queue.put_nowait(_DeliveryRequest(boundary_refresh=True))

        with (
            patch("mindroom.streaming.time.time", return_value=101.0),
            patch(
                "mindroom.streaming_delivery.time.time",
                return_value=101.0,
            ),
        ):
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert streaming.stream_started_at == 101.0

    @pytest.mark.asyncio
    async def test_idle_flush_followed_by_small_chunk_does_not_double_emit(self) -> None:
        """An idle flush should reset the idle baseline before the next small chunk arrives."""
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.0,
            max_idle=0.25,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0
        streaming._send_content = AsyncMock(return_value=True)

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0]):
            await streaming.update_content("partial", mock_client)

        with patch("mindroom.streaming.time.time", return_value=100.3):
            await streaming._throttled_send(mock_client)

        assert streaming._send_content.await_count == 1

        with patch("mindroom.streaming.time.time", side_effect=[100.35, 100.35, 100.35]):
            await streaming.update_content("!", mock_client)

        assert streaming._send_content.await_count == 1

    @pytest.mark.asyncio
    async def test_idle_flush_does_not_fire_during_steady_streaming(self) -> None:
        """Small inter-delta gaps should not inflate edit cadence via the idle trigger."""
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=0.5,
            min_update_interval=0.5,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.0,
            max_idle=0.25,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0
        streaming._send_content = AsyncMock(return_value=True)

        clock = iter(100.0 + 0.03 * step for step in range(1, 80))
        with patch("mindroom.streaming.time.time", side_effect=lambda: next(clock)):
            for _ in range(20):
                await streaming.update_content("a", mock_client)

        assert streaming._send_content.await_count == 2

    @pytest.mark.asyncio
    async def test_idle_flush_triggers_after_delta_gap(self) -> None:
        """A single buffered chunk should flush once the idle gap exceeds max_idle."""
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.0,
            max_idle=0.25,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0
        streaming._send_content = AsyncMock(return_value=True)

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.05]):
            await streaming.update_content("partial", mock_client)

        assert streaming._send_content.await_count == 0
        assert streaming.last_delta_at == 100.0

        with patch("mindroom.streaming.time.time", return_value=100.3):
            await streaming._throttled_send(mock_client)

        assert streaming._send_content.await_count == 1
        assert streaming.chars_since_last_update == 0

    @pytest.mark.asyncio
    async def test_pre_tool_flush_does_not_hide_tool_marker_below_threshold(self) -> None:
        """A failed phase-boundary flush should preserve buffered chars so the marker can still surface."""
        mock_client = _make_matrix_client_mock()
        sent_bodies: list[str] = []
        send_results = iter((False, True))

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=48,
            min_update_char_threshold=48,
            min_char_update_interval=0.0,
            max_idle=10.0,
            show_tool_calls=True,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0

        async def record_send(*args: object, **kwargs: object) -> bool:
            _ = (args, kwargs)
            sent_bodies.append(streaming.accumulated_text)
            return next(send_results)

        streaming._send_or_edit_message = AsyncMock(side_effect=record_send)

        async def response_stream() -> AsyncIterator[object]:
            yield "x" * 40
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0, 100.1, 100.1, 100.1]):
            await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)
        await streaming.finalize(mock_client)

        assert streaming._send_or_edit_message.await_count == 2
        assert sent_bodies[0].startswith("x" * 40)
        assert "search_web" in sent_bodies[0]
        assert sent_bodies[1].startswith("x" * 40)
        assert "search_web" in sent_bodies[1]

    @pytest.mark.asyncio
    async def test_idle_does_not_fire_below_min_char_interval(self) -> None:
        """Idle-triggered edits should still respect the anti-spam minimum interval floor."""
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.35,
            max_idle=0.25,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0
        streaming._send_content = AsyncMock(return_value=True)

        with patch("mindroom.streaming.time.time", side_effect=[100.0, 100.0]):
            await streaming.update_content("first", mock_client)

        with patch("mindroom.streaming.time.time", side_effect=[100.3, 100.3, 100.3]):
            await streaming.update_content(" second", mock_client)

        assert streaming._send_content.await_count == 0

    @pytest.mark.asyncio
    async def test_progress_hint_uses_shorter_interval(self) -> None:
        """Tool progress hints should allow faster keepalive edits than steady-state interval."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$stream_progress_1"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            progress_update_interval=0.2,
        )
        streaming.event_id = "$existing_event"
        streaming.accumulated_text = "working"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0

        with patch("mindroom.streaming.time.time", return_value=100.25):
            await streaming._throttled_send(mock_client, progress_hint=False)
        assert mock_client.room_send.call_count == 0

        with patch("mindroom.streaming.time.time", return_value=100.25):
            await streaming._throttled_send(mock_client, progress_hint=True)
        assert mock_client.room_send.call_count == 1

    @pytest.mark.asyncio
    async def test_progress_hint_can_update_existing_message_before_text(self) -> None:
        """Hidden tool calls should keep an existing thinking message visibly alive."""
        mock_client = _make_matrix_client_mock()

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            progress_update_interval=0.2,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0

        with (
            patch("mindroom.streaming.time.time", return_value=100.25),
            patch(
                "mindroom.streaming.edit_message_result",
                new=AsyncMock(
                    return_value=DeliveredMatrixEvent(
                        event_id="$existing_event",
                        content_sent={"body": PROGRESS_PLACEHOLDER},
                    ),
                ),
            ) as mock_edit,
        ):
            await streaming._throttled_send(mock_client, progress_hint=True)

        assert mock_edit.await_count == 1
        edit_args = mock_edit.await_args.args
        assert edit_args[3]["body"].startswith(PROGRESS_PLACEHOLDER)
        assert IN_PROGRESS_MARKER not in edit_args[3]["body"]

    @pytest.mark.asyncio
    async def test_progress_hint_creates_initial_message_on_cold_start(self) -> None:
        """Tool-first streams with hidden tool calls should create an initial placeholder message."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$cold_start_1"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            progress_update_interval=0.2,
        )
        # No event_id set — simulates tool-first cold start
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0

        with patch("mindroom.streaming.time.time", return_value=100.25):
            await streaming._throttled_send(mock_client, progress_hint=True)

        assert mock_client.room_send.call_count == 1
        assert streaming.event_id == "$cold_start_1"
        sent_content = mock_client.room_send.call_args[1]["content"]
        assert sent_content["body"].startswith(PROGRESS_PLACEHOLDER)
        assert IN_PROGRESS_MARKER not in sent_content["body"]

    @pytest.mark.asyncio
    async def test_placeholder_progress_hint_does_not_resend_after_commit(self) -> None:
        """Whitespace-only placeholder sends should advance throttle state after committing."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$placeholder_progress_1"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            progress_update_interval=0.2,
            show_tool_calls=False,
        )
        streaming.accumulated_text = "\n\n"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0

        with patch("mindroom.streaming.time.time", return_value=100.25):
            await streaming._throttled_send(mock_client, progress_hint=True)
        with patch("mindroom.streaming.time.time", return_value=100.26):
            await streaming._throttled_send(mock_client, progress_hint=True)

        assert mock_client.room_send.call_count == 1

    @pytest.mark.asyncio
    async def test_finalize_strips_marker_from_placeholder_only_stream(self) -> None:
        """Finalize should edit out the in-progress marker even when no text was ever emitted."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$placeholder_msg"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=5.0,
            progress_update_interval=0.2,
        )
        # Simulate cold-start: progress hint created the initial placeholder
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0

        with patch("mindroom.streaming.time.time", return_value=100.25):
            await streaming._throttled_send(mock_client, progress_hint=True)

        assert streaming.event_id == "$placeholder_msg"
        assert mock_client.room_send.call_count == 1

        # Verify the initial message has no in-progress marker (frontend shows animated indicator via stream_status)
        initial_content = mock_client.room_send.call_args[1]["content"]
        assert IN_PROGRESS_MARKER not in initial_content["body"]

        # Now finalize with no text ever emitted
        with patch(
            "mindroom.streaming.edit_message_result",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$placeholder_msg",
                    content_sent={"body": PROGRESS_PLACEHOLDER},
                ),
            ),
        ) as mock_edit:
            await streaming.finalize(mock_client)

        assert mock_edit.await_count == 1
        final_body = mock_edit.await_args.args[3]["body"]
        assert final_body == PROGRESS_PLACEHOLDER
        assert IN_PROGRESS_MARKER not in final_body

    @pytest.mark.asyncio
    async def test_finalize_does_not_overwrite_existing_message_without_placeholder(self) -> None:
        """Finalize should not force a placeholder onto arbitrary existing messages."""
        mock_client = _make_matrix_client_mock()

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        # Existing event from edit/ack flows, but no placeholder progress was sent.
        streaming.event_id = "$existing_msg"

        with patch(
            "mindroom.streaming.edit_message_result",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$existing_msg",
                    content_sent={"body": PROGRESS_PLACEHOLDER},
                ),
            ),
        ) as mock_edit:
            await streaming.finalize(mock_client)

        assert mock_edit.await_count == 0

    @pytest.mark.asyncio
    async def test_send_streaming_response_finalizes_adopted_placeholder_without_chunks(self) -> None:
        """Adopted thinking placeholders should still get a terminal edit when no text arrives."""
        mock_client = _make_matrix_client_mock()
        edited_contents: list[tuple[dict[str, object], str]] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_contents.append((new_content, new_text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

        async def empty_stream() -> AsyncIterator[str]:
            if False:
                yield ""
            return

        with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
            outcome = await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=empty_stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
            )

        event_id = outcome.last_physical_stream_event_id
        accumulated = outcome.rendered_body
        assert event_id == "$thinking_123"
        assert accumulated is not None
        assert len(edited_contents) == 1
        final_content, final_text = edited_contents[0]
        assert final_text == PROGRESS_PLACEHOLDER
        assert final_content["body"] == PROGRESS_PLACEHOLDER
        assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED
        assert final_content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$original_123"}}

    @pytest.mark.asyncio
    async def test_send_streaming_response_records_outbound_send_and_edit(self) -> None:
        """Streaming delivery should write through both the initial send and later edit."""
        mock_client = _make_matrix_client_mock()
        conversation_cache = AsyncMock()
        conversation_cache.notify_outbound_message = Mock()

        async def one_chunk_stream() -> AsyncIterator[str]:
            yield "Hello from stream"

        async def record_send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent(event_id="$stream-send", content_sent=dict(content))

        async def record_edit(
            _client: object,
            _room_id: str,
            event_id: str,
            new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent(
                event_id="$stream-edit",
                content_sent=build_edit_event_content(
                    event_id=event_id,
                    new_content=dict(new_content),
                    new_text=new_text,
                ),
            )

        with (
            patch("mindroom.streaming.send_message_result", new=AsyncMock(side_effect=record_send)),
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
        ):
            outcome = await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id="$thread_root",
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=one_chunk_stream(),
                conversation_cache=conversation_cache,
                latest_thread_event_id="$original_123",
            )

        event_id = outcome.last_physical_stream_event_id
        accumulated = outcome.rendered_body
        assert event_id == "$stream-send"
        assert accumulated is not None
        assert accumulated == "Hello from stream"
        assert conversation_cache.notify_outbound_message.call_count == 2
        first_call = conversation_cache.notify_outbound_message.call_args_list[0].args
        second_call = conversation_cache.notify_outbound_message.call_args_list[1].args
        assert first_call[:2] == ("!test:localhost", "$stream-send")
        assert first_call[2]["body"] == "Hello from stream"
        assert second_call[:2] == ("!test:localhost", "$stream-edit")
        assert second_call[2]["m.relates_to"]["rel_type"] == "m.replace"
        assert second_call[2]["m.relates_to"]["event_id"] == "$stream-send"

    @pytest.mark.asyncio
    async def test_streaming_first_send_uses_resolved_thread_root(
        self,
        mock_helper_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming without a preexisting placeholder should keep the original thread root."""

        @asynccontextmanager
        async def noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
            yield

        async def response_stream() -> AsyncIterator[str]:
            yield "Hello from the original thread"

        async def no_latest_thread_event(
            _client: object,
            _room_id: str,
            _thread_id: str | None,
            _reply_to_event_id: str | None,
            _existing_event_id: str | None = None,
            event_cache: object | None = None,
        ) -> None:
            _ = event_cache

        sent_contents: list[dict[str, object]] = []
        config = self.config
        bot = AgentBot(
            mock_helper_agent,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=True,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        install_runtime_cache_support(bot)
        bot.client = MagicMock(rooms={})
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        replace_response_runner_deps(
            bot,
            knowledge_access=bot._knowledge_access_support,
        )
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            room_id="!test:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id=None,
                reply_to_event_id="$reply_plain:localhost",
                thread_start_root_event_id="$thread_root:localhost",
            ),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="Continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="helper",
            source_kind="message",
        )

        async def record_send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
        ) -> DeliveredMatrixEvent:
            sent_contents.append(content)
            return DeliveredMatrixEvent(event_id="$stream_1", content_sent=dict(content))

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            _new_text: str,
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent(event_id="$stream_1", content_sent={})

        with (
            patch("mindroom.streaming.send_message_result", new=record_send),
            patch("mindroom.streaming.edit_message_result", new=record_edit),
            patch_response_runner_module(
                stream_agent_response=MagicMock(return_value=response_stream()),
                typing_indicator=noop_typing,
            ),
        ):
            delivery = await bot._response_runner.process_and_respond_streaming(
                ResponseRequest(
                    room_id="!test:localhost",
                    reply_to_event_id="$reply_plain:localhost",
                    thread_id=None,
                    thread_history=[],
                    prompt="Continue",
                    user_id="@user:localhost",
                    response_envelope=envelope,
                    correlation_id="$request:localhost",
                ),
            )

        assert delivery.event_id == "$stream_1"
        assert sent_contents
        first_content = sent_contents[0]
        assert first_content["m.relates_to"]["rel_type"] == "m.thread"
        assert first_content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert first_content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"

    @pytest.mark.asyncio
    async def test_streaming_target_syncs_scalar_fields_before_send(self) -> None:
        """Canonical target values must override stale scalar fields before delivery."""
        sent_messages: list[tuple[str, dict[str, object]]] = []
        target = MessageTarget.resolve(
            room_id="!canonical:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$reply:localhost",
        )

        async def record_send(
            _client: object,
            room_id: str,
            content: dict[str, object],
        ) -> DeliveredMatrixEvent:
            sent_messages.append((room_id, content))
            return DeliveredMatrixEvent(event_id="$stream_1", content_sent=dict(content))

        with patch("mindroom.streaming.send_message_result", new=record_send):
            streaming = StreamingResponse(
                room_id="!stale:localhost",
                reply_to_event_id="$stale_reply:localhost",
                thread_id="$stale_thread:localhost",
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                target=target,
            )

            assert streaming.room_id == "!canonical:localhost"
            assert streaming.thread_id == "$thread:localhost"
            assert streaming.reply_to_event_id == "$reply:localhost"

            await streaming.update_content("Hello world", AsyncMock())

        assert sent_messages
        room_id, content = sent_messages[0]
        assert room_id == "!canonical:localhost"
        assert isinstance(content["m.relates_to"], dict)
        assert content["m.relates_to"]["event_id"] == "$thread:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply:localhost"

    @pytest.mark.asyncio
    async def test_chat_turn_does_not_block_on_repo_change(
        self,
        mock_helper_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A missing knowledge snapshot must not block one streamed Matrix turn."""
        base_id = "repo_change_docs"
        bot = _make_bot_with_shared_knowledge(
            tmp_path=tmp_path,
            agent=mock_helper_agent,
            base_id=base_id,
        )
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$thinking"
        bot.client.room_send.return_value = mock_send_response
        pipeline_timing = DispatchPipelineTiming(source_event_id="$request", room_id="!test:localhost")

        async def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            system_enrichment_items = kwargs["system_enrichment_items"]
            assert len(system_enrichment_items) == 1
            assert (
                f"Knowledge base `{base_id}` is initializing and unavailable for semantic search this turn."
                in system_enrichment_items[0].text
            )
            yield "stream chunk"

        async def fake_send_streaming_response(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            response_stream = args[7]
            chunks = [str(chunk) async for chunk in response_stream]
            body = "".join(chunks)
            return StreamTransportOutcome(
                last_physical_stream_event_id="$response",
                terminal_status="completed",
                rendered_body=body,
                visible_body_state="visible_body",
            )

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(side_effect=fake_send_streaming_response),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                stream_agent_response=fake_stream_agent_response,
                typing_indicator=_noop_typing_indicator,
            ),
        ):
            event_id = await asyncio.wait_for(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id=None,
                        thread_history=[],
                        prompt="Please check the docs",
                        user_id="@user:localhost",
                        pipeline_timing=pipeline_timing,
                    ),
                ),
                timeout=0.5,
            )

        assert event_id == "$response"
        assert "placeholder_sent" in pipeline_timing.marks
        assert "streaming_complete" in pipeline_timing.marks
        assert pipeline_timing.marks["placeholder_sent"] <= pipeline_timing.marks["streaming_complete"]

    @pytest.mark.asyncio
    async def test_fresh_turn_does_zero_freshness_work_on_request_path(
        self,
        mock_helper_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A live turn should not call knowledge freshness lifecycle functions."""
        bot = _make_bot_with_shared_knowledge(
            tmp_path=tmp_path,
            agent=mock_helper_agent,
            base_id="fresh_turn_docs",
        )

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            system_enrichment_items = kwargs["system_enrichment_items"]
            assert len(system_enrichment_items) == 1
            assert "Knowledge base `fresh_turn_docs` is initializing" in system_enrichment_items[0].text
            return "handled"

        with (
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.deliver_final",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        final_visible_body="handled",
                        is_visible_response=True,
                        delivery_kind="sent",
                    ),
                ),
            ),
            patch(
                "mindroom.knowledge.manager.KnowledgeManager.sync_git_source",
                new=AsyncMock(),
            ) as sync_git_source,
            patch(
                "mindroom.knowledge.manager.KnowledgeManager.reindex_all",
                new=AsyncMock(),
            ) as reindex_all,
            patch_response_runner_module(
                ai_response=AsyncMock(side_effect=fake_ai_response),
                typing_indicator=_noop_typing_indicator,
            ),
        ):
            delivery = await bot._response_runner.process_and_respond(
                ResponseRequest(
                    room_id="!test:localhost",
                    reply_to_event_id="$event",
                    thread_id=None,
                    thread_history=[],
                    prompt="Please check the docs",
                    user_id="@user:localhost",
                ),
            )

        assert delivery.event_id == "$response"
        assert sync_git_source.await_count == 0
        assert reindex_all.await_count == 0

    @pytest.mark.asyncio
    async def test_streaming_in_progress_marker(
        self,
        mock_helper_agent: AgentMatrixUser,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test that in-progress marker is shown during streaming but not in final message."""
        # Create a mock client
        mock_client = _make_matrix_client_mock()
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$stream_123"
        mock_client.room_send.return_value = mock_send_response

        # Create streaming response
        config = self.config
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

        # Stream some content
        await streaming.update_content("Hello world", mock_client)

        # Check that the sent message includes the in-progress marker
        first_call = mock_client.room_send.call_args_list[0]
        content = first_call[1]["content"]
        # The body should NOT contain the static in-progress marker (frontend uses stream_status metadata)
        assert IN_PROGRESS_MARKER not in content["body"]
        assert "Hello world" in content["body"]

        # Finalize the message
        await streaming.finalize(mock_client)

        # Check the final message has no in-progress marker
        final_call = mock_client.room_send.call_args_list[-1]
        final_content = final_call[1]["content"]
        assert IN_PROGRESS_MARKER not in final_content["body"]
        assert "Hello world" in final_content["body"]

    @pytest.mark.asyncio
    async def test_cancelled_stream_preserves_partial_text_with_suffix(self) -> None:
        """Cancellation should keep streamed text and append a stop marker."""
        mock_client = _make_matrix_client_mock()
        edited_texts: list[str] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_texts.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def cancelling_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            raise asyncio.CancelledError(USER_STOP_CANCEL_MSG)

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError) as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=cancelling_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
            )

        assert len(edited_texts) == 2
        assert IN_PROGRESS_MARKER not in edited_texts[0]
        assert edited_texts[-1] == f"Partial answer\n\n{CANCELLED_RESPONSE_NOTE}"
        assert exc_info.value.transport_outcome.terminal_status == "cancelled"
        assert exc_info.value.transport_outcome.failure_reason == "cancelled_by_user"

    @pytest.mark.asyncio
    async def test_interrupted_stream_preserves_partial_text_with_suffix(self) -> None:
        """Generic interruptions should not render as user-cancelled."""
        mock_client = _make_matrix_client_mock()
        edited_texts: list[str] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_texts.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def interrupted_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            raise asyncio.CancelledError

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError),
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=interrupted_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
            )

        assert len(edited_texts) == 2
        assert edited_texts[-1] == f"Partial answer\n\n{INTERRUPTED_RESPONSE_NOTE}"

    @pytest.mark.asyncio
    async def test_cancelled_stream_reports_existing_event_id_to_callback(self) -> None:
        """Cancellation should report the visible placeholder event ID immediately."""
        mock_client = _make_matrix_client_mock()
        visible_event_ids: list[str] = []

        async def cancelling_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            raise asyncio.CancelledError

        with (
            patch(
                "mindroom.streaming.edit_message_result",
                new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$edit", content_sent={})),
            ),
            pytest.raises(StreamingDeliveryError),
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=cancelling_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
                visible_event_id_callback=visible_event_ids.append,
            )

        assert visible_event_ids == ["$thinking_123"]

    @pytest.mark.asyncio
    async def test_cancelled_stream_reports_new_event_id_to_callback(self) -> None:
        """Cancellation should report the first newly-created visible event ID."""
        mock_client = _make_matrix_client_mock()
        visible_event_ids: list[str] = []

        async def cancelling_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            raise asyncio.CancelledError

        with (
            patch(
                "mindroom.streaming.send_message_result",
                new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$stream-123", content_sent={})),
            ),
            patch(
                "mindroom.streaming.edit_message_result",
                new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$edit", content_sent={})),
            ),
            pytest.raises(StreamingDeliveryError),
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=cancelling_stream(),
                room_mode=True,
                visible_event_id_callback=visible_event_ids.append,
            )

        assert visible_event_ids == ["$stream-123"]

    @pytest.mark.asyncio
    async def test_sync_restart_stream_preserves_partial_text_with_restart_suffix(self) -> None:
        """Sync-restart cancellation should keep streamed text and append the restart marker."""
        mock_client = _make_matrix_client_mock()
        edited_messages: list[tuple[dict[str, object], str]] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((new_content, new_text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

        async def cancelling_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            raise asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError) as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=cancelling_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
            )

        assert len(edited_messages) == 2
        final_content, final_text = edited_messages[-1]
        assert final_text == build_restart_interrupted_body("Partial answer")
        assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
        assert exc_info.value.transport_outcome.terminal_status == "cancelled"
        assert exc_info.value.transport_outcome.failure_reason == "sync_restart_cancelled"

    @pytest.mark.asyncio
    async def test_cancel_sources_reuse_compatible_terminal_stream_statuses(self) -> None:
        """Generic interruptions must not introduce a new wire-level stream status."""
        mock_client = _make_matrix_client_mock()
        observed_statuses: dict[str, str] = {}

        cases = (
            ("user_stop", USER_STOP_CANCEL_MSG, STREAM_STATUS_CANCELLED),
            ("interrupted", None, STREAM_STATUS_ERROR),
            ("sync_restart", SYNC_RESTART_CANCEL_MSG, STREAM_STATUS_ERROR),
        )

        for label, cancel_message, expected_status in cases:
            edited_messages: list[dict[str, object]] = []

            async def record_edit(
                _client: object,
                _room_id: str,
                _event_id: str,
                new_content: dict[str, object],
                _new_text: str,
                *,
                _edited_messages: list[dict[str, object]] = edited_messages,
            ) -> DeliveredMatrixEvent:
                _edited_messages.append(new_content)
                return DeliveredMatrixEvent(event_id="$edit", content_sent=new_content)

            async def cancelling_stream(
                *,
                _cancel_message: str | None = cancel_message,
            ) -> AsyncIterator[str]:
                yield "Partial answer"
                if _cancel_message is None:
                    raise asyncio.CancelledError
                raise asyncio.CancelledError(_cancel_message)

            with (
                patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
                pytest.raises(StreamingDeliveryError),
            ):
                await send_streaming_response(
                    client=mock_client,
                    room_id="!test:localhost",
                    reply_to_event_id="$original_123",
                    thread_id=None,
                    sender_domain="localhost",
                    config=self.config,
                    runtime_paths=runtime_paths_for(self.config),
                    response_stream=cancelling_stream(),
                    existing_event_id="$thinking_123",
                    room_mode=True,
                )

            observed_statuses[label] = str(edited_messages[-1][STREAM_STATUS_KEY])
            assert observed_statuses[label] == expected_status

        assert set(observed_statuses.values()) == {STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR}

    @pytest.mark.asyncio
    async def test_stream_error_preserves_partial_text_and_appends_error_hint(self) -> None:
        """Stream failures should remove pending state and append an error hint."""
        mock_client = _make_matrix_client_mock()
        edited_texts: list[str] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_texts.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def failing_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            msg = "model backend disconnected"
            raise RuntimeError(msg)

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="model backend disconnected") as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=failing_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
            )

        assert isinstance(exc_info.value.error, RuntimeError)
        assert str(exc_info.value.error) == "model backend disconnected"
        assert exc_info.value.event_id == "$thinking_123"
        assert len(edited_texts) == 2
        assert IN_PROGRESS_MARKER not in edited_texts[0]
        final_text = edited_texts[-1]
        assert exc_info.value.accumulated_text == final_text
        assert final_text.startswith("Partial answer\n\n**[Response interrupted by an error:")
        assert "model backend disconnected" in final_text
        assert IN_PROGRESS_MARKER not in final_text

    @pytest.mark.asyncio
    async def test_stream_error_replaces_placeholder_when_no_text_arrives(self) -> None:
        """Stream failures before first chunk should replace a thinking placeholder with an error hint."""
        mock_client = _make_matrix_client_mock()
        edited_texts: list[str] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_texts.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def failing_stream() -> AsyncIterator[str]:
            if False:
                yield ""
            msg = "provider stream failed"
            raise RuntimeError(msg)

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="provider stream failed") as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=failing_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
            )

        assert isinstance(exc_info.value.error, RuntimeError)
        assert str(exc_info.value.error) == "provider stream failed"
        assert exc_info.value.event_id == "$thinking_123"
        assert len(edited_texts) == 1
        final_text = edited_texts[0]
        assert exc_info.value.accumulated_text == final_text
        assert final_text.startswith("**[Response interrupted by an error:")
        assert "provider stream failed" in final_text
        assert IN_PROGRESS_MARKER not in final_text

    @pytest.mark.asyncio
    async def test_worker_progress_delivery_failure_raises_streaming_delivery_error(self) -> None:
        """Worker-progress delivery errors should follow the normal streaming error contract."""
        mock_client = _make_matrix_client_mock()

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            if "Preparing isolated worker" in new_text:
                msg = "edit blew up"
                raise RuntimeError(msg)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def stream() -> AsyncIterator[str]:
            pump = get_worker_progress_pump()
            assert pump is not None
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await asyncio.sleep(0)
            yield "hello"

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="edit blew up") as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
            )

        assert isinstance(exc_info.value.error, RuntimeError)
        assert str(exc_info.value.error) == "edit blew up"
        assert exc_info.value.event_id == "$thinking_123"

    @pytest.mark.asyncio
    async def test_worker_progress_delivery_failure_still_writes_terminal_status(self) -> None:
        """A failed warmup edit should still be followed by a terminal error-status edit."""
        mock_client = _make_matrix_client_mock()
        terminal_statuses: list[str] = []
        edited_texts: list[str] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            terminal_statuses.append(str(new_content[STREAM_STATUS_KEY]))
            edited_texts.append(new_text)
            if "Preparing isolated worker" in new_text:
                msg = "edit blew up"
                raise RuntimeError(msg)
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

        async def stream() -> AsyncIterator[str]:
            pump = get_worker_progress_pump()
            assert pump is not None
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await asyncio.sleep(0)
            yield "hello"

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="edit blew up"),
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
            )

        assert terminal_statuses[-1] == STREAM_STATUS_ERROR
        assert edited_texts[-1].startswith("**[Response interrupted by an error:")
        assert "hello" not in edited_texts[-1]

    @pytest.mark.asyncio
    async def test_worker_progress_delivery_failure_stops_chunk_consumption_immediately(self) -> None:
        """A warmup delivery failure should cancel chunk consumption before later chunks are reached."""
        mock_client = _make_matrix_client_mock()
        later_chunk_reached = False
        edited_texts: list[str] = []

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_texts.append(new_text)
            if "Preparing isolated worker" in new_text:
                msg = "edit blew up"
                raise RuntimeError(msg)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def stream() -> AsyncIterator[str]:
            nonlocal later_chunk_reached
            pump = get_worker_progress_pump()
            assert pump is not None
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await asyncio.sleep(0.2)
            later_chunk_reached = True
            yield "hello"

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="edit blew up"),
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
            )

        assert later_chunk_reached is False
        assert all("hello" not in text for text in edited_texts)

    @pytest.mark.asyncio
    async def test_worker_progress_shutdown_timeout_does_not_fail_successful_stream(self) -> None:
        """A timed-out progress-drain shutdown is cleanup-only and must not fail a successful stream."""
        mock_client = _make_matrix_client_mock()

        async def record_send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent(event_id="$event123", content_sent=dict(content))

        async def lingering_drain(
            _client: object,
            _streaming: object,
            _queue: object,
            _pump: object,
        ) -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await asyncio.sleep(0.6)
                raise

        async def stream() -> AsyncIterator[str]:
            yield "hello"

        with (
            patch("mindroom.streaming.send_message_result", new=AsyncMock(side_effect=record_send)),
            patch(
                "mindroom.streaming.edit_message_result",
                new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$edit123", content_sent={})),
            ),
            patch("mindroom.streaming._drain_worker_progress_events", new=lingering_drain),
        ):
            outcome = await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
            )

        assert outcome.last_physical_stream_event_id == "$event123"
        assert outcome.terminal_status == "completed"
        assert outcome.rendered_body == "hello"

    @pytest.mark.asyncio
    async def test_worker_progress_delivery_failure_does_not_leak_undelivered_buffered_text(self) -> None:
        """Terminal error text should not include chunks that were buffered but never delivered."""
        mock_client = _make_matrix_client_mock()
        warmup_edit_started = asyncio.Event()
        edited_texts: list[str] = []

        class ImmediateStreamingResponse(StreamingResponse):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                self.update_interval = 0.0
                self.min_update_interval = 0.0
                self.progress_update_interval = 0.0
                self.min_char_update_interval = 0.0
                self.update_char_threshold = 1
                self.min_update_char_threshold = 1

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            edited_texts.append(new_text)
            if "Preparing isolated worker" in new_text and "hello" not in new_text and "world" not in new_text:
                warmup_edit_started.set()
                await asyncio.sleep(0)
                msg = "edit blew up"
                raise RuntimeError(msg)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def stream() -> AsyncIterator[str]:
            pump = get_worker_progress_pump()
            assert pump is not None
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await warmup_edit_started.wait()
            yield "hello"
            yield " world"

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="edit blew up") as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
                streaming_cls=ImmediateStreamingResponse,
            )

        assert exc_info.value.accumulated_text == edited_texts[-1]
        assert exc_info.value.accumulated_text.startswith("**[Response interrupted by an error:")
        assert "hello" not in exc_info.value.accumulated_text
        assert "world" not in exc_info.value.accumulated_text

    @pytest.mark.asyncio
    async def test_late_delivery_failure_after_stream_completion_restores_last_delivered_state(self) -> None:
        """A delivery failure surfaced only during shutdown should still roll back undelivered buffered text."""
        mock_client = _make_matrix_client_mock()
        stream_finished = asyncio.Event()

        class ImmediateStreamingResponse(StreamingResponse):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                self.update_interval = 0.0
                self.min_update_interval = 0.0
                self.progress_update_interval = 0.0
                self.min_char_update_interval = 0.0
                self.update_char_threshold = 1
                self.min_update_char_threshold = 1

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            _new_text: str,
        ) -> DeliveredMatrixEvent:
            await stream_finished.wait()
            if _new_content.get("io.mindroom.stream_status") == "streaming":
                msg = "late edit blew up"
                raise RuntimeError(msg)
            return DeliveredMatrixEvent(event_id="$terminal", content_sent={})

        async def stream() -> AsyncIterator[str]:
            yield "hello"
            stream_finished.set()

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="late edit blew up") as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
                streaming_cls=ImmediateStreamingResponse,
            )

        assert exc_info.value.accumulated_text.startswith("**[Response interrupted by an error:")
        assert "hello" not in exc_info.value.accumulated_text

    @pytest.mark.asyncio
    async def test_streaming_edit_returning_none_raises_delivery_error(self) -> None:
        """A Matrix edit failure returning None must not be treated as a successful non-terminal send."""
        mock_client = _make_matrix_client_mock()

        class ImmediateStreamingResponse(StreamingResponse):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                self.update_interval = 0.0
                self.min_update_interval = 0.0
                self.progress_update_interval = 0.0
                self.min_char_update_interval = 0.0
                self.update_char_threshold = 1
                self.min_update_char_threshold = 1

        terminal_texts: list[str] = []
        edit_results = [None, DeliveredMatrixEvent(event_id="$terminal", content_sent={})]

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent | None:
            terminal_texts.append(new_text)
            return edit_results.pop(0)

        async def stream() -> AsyncIterator[str]:
            yield "hello"

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="Failed to edit streaming message") as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
                streaming_cls=ImmediateStreamingResponse,
            )

        assert terminal_texts[-1].startswith("**[Response interrupted by an error:")
        assert "hello" not in exc_info.value.accumulated_text

    @pytest.mark.asyncio
    async def test_send_or_edit_message_commits_frozen_preawait_snapshot(self) -> None:
        """Successful delivery should commit the exact pre-await stream state, not later buffered mutations."""
        mock_client = _make_matrix_client_mock()
        runtime_paths = runtime_paths_for(self.config)
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$original_123",
            room_mode=True,
        )
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths,
            target=target,
            latest_thread_event_id=None,
            room_mode=True,
            show_tool_calls=True,
            extra_content=None,
        )
        streaming.event_id = "$thinking_123"
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=1.0,
                ),
            ),
        )

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            _new_text: str,
        ) -> DeliveredMatrixEvent:
            streaming.accumulated_text = "hello"
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
            await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        assert streaming._last_delivered_text == ""

    @pytest.mark.asyncio
    async def test_shutdown_stream_delivery_reports_timeout_when_task_does_not_stop(self) -> None:
        """A delivery task that ignores cancellation must surface a timeout instead of reporting clean shutdown."""
        delivery_queue: asyncio.Queue[object | None] = asyncio.Queue()
        task_started = asyncio.Event()
        release_task = asyncio.Event()

        async def stuck_delivery() -> None:
            task_started.set()
            while not release_task.is_set():
                try:
                    await asyncio.wait_for(release_task.wait(), timeout=3600)
                except asyncio.CancelledError:
                    continue

        delivery_task = asyncio.create_task(stuck_delivery())
        await task_started.wait()

        shutdown_error = await _shutdown_stream_delivery(
            delivery_queue,
            delivery_task,
            drain_timeout_seconds=0.01,
            cancel_timeout_seconds=0.01,
        )

        assert isinstance(shutdown_error, TimeoutError)
        release_task.set()
        await delivery_task

    @pytest.mark.asyncio
    async def test_shutdown_stream_delivery_allows_slow_but_finishing_delivery_task(self) -> None:
        """A slow delivery task should be allowed to finish without a synthetic timeout failure."""
        delivery_queue: asyncio.Queue[object | None] = asyncio.Queue()
        task_started = asyncio.Event()
        release_task = asyncio.Event()

        async def slow_delivery() -> None:
            task_started.set()
            while not release_task.is_set():
                try:
                    await asyncio.wait_for(release_task.wait(), timeout=3600)
                except asyncio.CancelledError:
                    continue

        delivery_task = asyncio.create_task(slow_delivery())
        await task_started.wait()
        asyncio.get_running_loop().call_later(0.05, release_task.set)

        shutdown_error = await _shutdown_stream_delivery(
            delivery_queue,
            delivery_task,
            drain_timeout_seconds=0.1,
            cancel_timeout_seconds=0.05,
        )

        assert shutdown_error is None
        await delivery_task

    @pytest.mark.asyncio
    async def test_drive_stream_delivery_preserves_oldest_prior_delta_at_for_idle_flush(self) -> None:
        """Merged requests must retain the oldest unsent delta so idle flush still fires."""
        mock_client = _make_matrix_client_mock()
        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            update_interval=10.0,
            min_update_interval=10.0,
            interval_ramp_seconds=0.0,
            update_char_threshold=10_000,
            min_update_char_threshold=10_000,
            min_char_update_interval=0.0,
            max_idle=0.25,
        )
        streaming.event_id = "$existing_event"
        streaming.stream_started_at = 100.0
        streaming.last_update = 100.0
        streaming.last_delta_at = 105.0
        streaming.accumulated_text = "first second"
        streaming.chars_since_last_update = len(streaming.accumulated_text)
        streaming._send_content = AsyncMock(return_value=True)

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        delivery_queue.put_nowait(_DeliveryRequest(prior_delta_at=100.0))
        delivery_queue.put_nowait(_DeliveryRequest(prior_delta_at=105.0))

        with patch("mindroom.streaming.time.time", return_value=105.02):
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert streaming._send_content.await_count == 1
        assert streaming.chars_since_last_update == 0

    @pytest.mark.asyncio
    async def test_force_refresh_merge_preserves_boundary_refresh_capture_completion(self) -> None:
        """Merged force-refresh sends must still resolve boundary-refresh capture waiters."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$merged_force_refresh"
        mock_client.room_send.return_value = mock_response

        streaming = ReplacementStreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.accumulated_text = "latest body"
        streaming.chars_since_last_update = len(streaming.accumulated_text)

        capture_completion = asyncio.get_running_loop().create_future()
        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        delivery_queue.put_nowait(_DeliveryRequest(force_refresh=True))
        delivery_queue.put_nowait(
            _DeliveryRequest(boundary_refresh=True, capture_completion=capture_completion),
        )

        with patch("mindroom.streaming.time.time", return_value=101.0):
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert capture_completion.done()
        assert capture_completion.result() is None
        assert streaming.last_boundary_refresh_at == 101.0

    @pytest.mark.asyncio
    async def test_send_streaming_response_skips_terminal_finalize_when_delivery_shutdown_times_out(self) -> None:
        """Terminal finalization must not run when the delivery owner refuses to stop."""
        mock_client = _make_matrix_client_mock()
        finalize_calls: list[Exception | None] = []

        class TrackingStreamingResponse(StreamingResponse):
            async def finalize(
                self,
                client: nio.AsyncClient,
                *,
                cancelled: bool = False,
                restart_interrupted: bool = False,
                cancel_source: CancelSource | None = None,
                error: Exception | None = None,
            ) -> StreamTransportOutcome:
                del client, cancelled, restart_interrupted, cancel_source
                finalize_calls.append(error)
                return StreamTransportOutcome(
                    last_physical_stream_event_id=self.event_id,
                    terminal_status="completed",
                    rendered_body=None,
                    visible_body_state="none",
                )

        async def fail_stream_supervision(
            response_stream: AsyncIterator[object],
            streaming: StreamingResponse,
            progress_task: asyncio.Task[None] | None,
            delivery_task: asyncio.Task[None] | None,
            delivery_queue: asyncio.Queue[object | None],
        ) -> None:
            del response_stream, streaming, progress_task, delivery_task, delivery_queue
            msg = "stream blew up"
            raise RuntimeError(msg)

        async def timeout_shutdown(
            delivery_queue: asyncio.Queue[object | None],
            delivery_task: asyncio.Task[None] | None,
        ) -> Exception | None:
            del delivery_queue
            if delivery_task is not None:
                delivery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery_task
            return _StreamDeliveryShutdownTimeoutError("Timed out shutting down stream delivery controller")

        with (
            patch("mindroom.streaming._consume_stream_with_progress_supervision", new=fail_stream_supervision),
            patch("mindroom.streaming._shutdown_stream_delivery", new=timeout_shutdown),
            pytest.raises(
                StreamingDeliveryError,
                match="Timed out shutting down stream delivery controller",
            ) as exc_info,
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=_aiter(),
                streaming_cls=TrackingStreamingResponse,
            )

        assert isinstance(exc_info.value.error, TimeoutError)
        assert finalize_calls == []

    @pytest.mark.asyncio
    async def test_worker_progress_and_content_updates_do_not_overlap_edits(self) -> None:
        """Warmup refreshes and content refreshes should not edit the same event concurrently."""
        mock_client = _make_matrix_client_mock()
        first_warmup_edit_started = asyncio.Event()
        release_warmup_edit = asyncio.Event()
        max_in_flight = 0
        in_flight = 0

        class ImmediateStreamingResponse(StreamingResponse):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                self.update_interval = 0.0
                self.min_update_interval = 0.0
                self.progress_update_interval = 0.0
                self.min_char_update_interval = 0.0
                self.update_char_threshold = 1
                self.min_update_char_threshold = 1

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                if "Preparing isolated worker" in new_text and "hello" not in new_text:
                    first_warmup_edit_started.set()
                    await release_warmup_edit.wait()
                return DeliveredMatrixEvent(event_id="$edit", content_sent={})
            finally:
                in_flight -= 1

        async def stream() -> AsyncIterator[str]:
            pump = get_worker_progress_pump()
            assert pump is not None
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await first_warmup_edit_started.wait()
            asyncio.get_running_loop().call_later(0.05, release_warmup_edit.set)
            yield "hello"

        with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
                streaming_cls=ImmediateStreamingResponse,
            )

        assert max_in_flight == 1

    @pytest.mark.asyncio
    async def test_worker_warmup_suffix_renders_and_clears_without_touching_accumulated_text(self) -> None:
        """Warmup notices should render as side-band text and disappear on ready."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_123"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=2.0,
                ),
            ),
        )

        await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        content = mock_client.room_send.call_args.kwargs["content"]
        first_body = content["body"]
        assert first_body == "Thinking...\n\n⏳ Preparing isolated worker for `shell.run`..."
        assert (
            content["formatted_body"]
            == "<p>Thinking...</p>\n<p>⏳ Preparing isolated worker for <code>shell.run</code>...</p>"
        )
        assert streaming.accumulated_text == ""

        streaming.accumulated_text = "hello"
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="ready",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=8.0,
                ),
            ),
        )

        await streaming._send_or_edit_message(mock_client)

        second_content = mock_client.room_send.call_args.kwargs["content"]
        second_body = second_content.get("m.new_content", second_content)["body"]
        assert "Preparing isolated worker" not in second_body
        assert second_body.endswith("hello")
        assert streaming.accumulated_text == "hello"

    @pytest.mark.asyncio
    async def test_send_streaming_response_keeps_warmup_side_band_out_of_accumulated_text(self) -> None:
        """Returned accumulated text should never contain worker warmup notices."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_stream_123"
        mock_client.room_send.return_value = mock_response

        async def stream() -> AsyncIterator[str]:
            pump = get_worker_progress_pump()
            assert pump is not None
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await asyncio.sleep(0)
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="ready",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=8.0,
                    ),
                ),
            )
            await asyncio.sleep(0.4)
            yield "x" * 300

        outcome = await send_streaming_response(
            client=mock_client,
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            response_stream=stream(),
        )

        assert outcome.last_physical_stream_event_id == "$warmup_stream_123"
        assert outcome.rendered_body == "x" * 300
        assert "Preparing isolated worker" not in outcome.rendered_body

    @pytest.mark.asyncio
    async def test_hidden_tool_mode_worker_warmup_uses_generic_copy(self) -> None:
        """Hidden-tool mode should never expose worker tool labels in warmup text."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_hidden_tools"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            show_tool_calls=False,
        )
        for tool_name, function_name in (("shell", "run"), ("python", "execute")):
            streaming.apply_worker_progress_event(
                WorkerProgressEvent(
                    tool_name=tool_name,
                    function_name=function_name,
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )

        await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        body = mock_client.room_send.call_args.kwargs["content"]["body"]
        assert "Preparing isolated worker..." in body
        assert "shell.run" not in body
        assert "python.execute" not in body

    @pytest.mark.asyncio
    async def test_worker_warmup_suffix_renders_outside_partial_markdown(self) -> None:
        """Warmup notices should append outside partially open markdown blocks."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_markdown"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.accumulated_text = "```python\nprint('hello')"
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=2.0,
                ),
            ),
        )

        await streaming._send_or_edit_message(mock_client)

        content = mock_client.room_send.call_args.kwargs["content"]
        warmup_text = "⏳ Preparing isolated worker for `shell.run`..."
        assert content["body"].endswith(f"\n\n{warmup_text}")
        assert "<pre><code" in content["formatted_body"]
        expected_html = "<p>⏳ Preparing isolated worker for <code>shell.run</code>...</p>"
        assert content["formatted_body"].endswith(expected_html)
        assert content["formatted_body"].rfind(expected_html) > content["formatted_body"].rfind("</pre>")

    @pytest.mark.asyncio
    async def test_worker_warmup_visible_body_uses_canonical_matrix_body(self) -> None:
        """Warmup metadata should preserve the canonical Matrix body, not the pre-mention display text."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_visible_body"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.accumulated_text = "Ping @helper"
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=2.0,
                ),
            ),
        )

        await streaming._send_or_edit_message(mock_client)

        content = mock_client.room_send.call_args.kwargs["content"]
        assert content["body"].startswith("Ping @mindroom_helper:localhost")
        assert content[STREAM_VISIBLE_BODY_KEY] == "Ping @mindroom_helper:localhost"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("terminal_kind", "expected_final_text"),
        [("cancel", INTERRUPTED_RESPONSE_NOTE), ("complete", PROGRESS_PLACEHOLDER)],
    )
    async def test_send_streaming_response_terminal_update_ignores_late_progress(
        self,
        terminal_kind: str,
        expected_final_text: str,
    ) -> None:
        """Late worker progress after terminal shutdown must not re-add the warmup suffix."""
        mock_client = _make_matrix_client_mock()
        captured_texts: list[str] = []
        late_event_done = threading.Event()
        late_event_thread: threading.Thread | None = None

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            captured_texts.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def wait_for_edit_count(expected_count: int) -> None:
            for _ in range(200):
                if len(captured_texts) >= expected_count:
                    return
                await asyncio.sleep(0.001)
            msg = f"Timed out waiting for {expected_count} edits"
            raise AssertionError(msg)

        async def stream() -> AsyncIterator[str]:
            nonlocal late_event_thread
            if False:
                yield ""
            pump = get_worker_progress_pump()
            assert pump is not None

            def emit_late_progress() -> None:
                pump.shutdown.wait(timeout=1.0)
                pump.loop.call_soon_threadsafe(
                    pump.queue.put_nowait,
                    WorkerProgressEvent(
                        tool_name="shell",
                        function_name="run",
                        progress=WorkerReadyProgress(
                            phase="waiting",
                            worker_key="worker-a",
                            backend_name="kubernetes",
                            elapsed_seconds=9.0,
                        ),
                    ),
                )
                late_event_done.set()

            late_event_thread = threading.Thread(target=emit_late_progress, daemon=True)
            late_event_thread.start()
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await wait_for_edit_count(1)
            if terminal_kind == "cancel":
                raise asyncio.CancelledError
            return

        with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
            if terminal_kind == "cancel":
                with pytest.raises(StreamingDeliveryError):
                    await send_streaming_response(
                        client=mock_client,
                        room_id="!test:localhost",
                        reply_to_event_id="$original_123",
                        thread_id=None,
                        sender_domain="localhost",
                        config=self.config,
                        runtime_paths=runtime_paths_for(self.config),
                        response_stream=stream(),
                        existing_event_id="$thinking_123",
                        adopt_existing_placeholder=True,
                        room_mode=True,
                    )
            else:
                await send_streaming_response(
                    client=mock_client,
                    room_id="!test:localhost",
                    reply_to_event_id="$original_123",
                    thread_id=None,
                    sender_domain="localhost",
                    config=self.config,
                    runtime_paths=runtime_paths_for(self.config),
                    response_stream=stream(),
                    existing_event_id="$thinking_123",
                    adopt_existing_placeholder=True,
                    room_mode=True,
                )

        assert late_event_thread is not None
        late_event_thread.join(timeout=1.0)
        assert late_event_done.is_set()
        await asyncio.sleep(0.05)

        assert len(captured_texts) == 2
        assert "Preparing isolated worker" in captured_texts[0]
        assert "Preparing isolated worker" not in captured_texts[1]
        assert captured_texts[1] == expected_final_text

    @pytest.mark.asyncio
    async def test_send_streaming_response_error_update_ignores_late_progress(self) -> None:
        """Late worker progress after an error must not re-add the warmup suffix."""
        mock_client = _make_matrix_client_mock()
        captured_texts: list[str] = []
        late_event_done = threading.Event()
        late_event_thread: threading.Thread | None = None

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            new_text: str,
        ) -> DeliveredMatrixEvent:
            captured_texts.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent={})

        async def wait_for_edit_count(expected_count: int) -> None:
            for _ in range(200):
                if len(captured_texts) >= expected_count:
                    return
                await asyncio.sleep(0.001)
            msg = f"Timed out waiting for {expected_count} edits"
            raise AssertionError(msg)

        async def failing_stream() -> AsyncIterator[str]:
            nonlocal late_event_thread
            if False:
                yield ""
            pump = get_worker_progress_pump()
            assert pump is not None

            def emit_late_progress() -> None:
                pump.shutdown.wait(timeout=1.0)
                pump.loop.call_soon_threadsafe(
                    pump.queue.put_nowait,
                    WorkerProgressEvent(
                        tool_name="shell",
                        function_name="run",
                        progress=WorkerReadyProgress(
                            phase="waiting",
                            worker_key="worker-a",
                            backend_name="kubernetes",
                            elapsed_seconds=9.0,
                        ),
                    ),
                )
                late_event_done.set()

            late_event_thread = threading.Thread(target=emit_late_progress, daemon=True)
            late_event_thread.start()
            pump.queue.put_nowait(
                WorkerProgressEvent(
                    tool_name="shell",
                    function_name="run",
                    progress=WorkerReadyProgress(
                        phase="cold_start",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=2.0,
                    ),
                ),
            )
            await wait_for_edit_count(1)
            msg = "worker failed"
            raise RuntimeError(msg)

        with (
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(StreamingDeliveryError, match="worker failed"),
        ):
            await send_streaming_response(
                client=mock_client,
                room_id="!test:localhost",
                reply_to_event_id="$original_123",
                thread_id=None,
                sender_domain="localhost",
                config=self.config,
                runtime_paths=runtime_paths_for(self.config),
                response_stream=failing_stream(),
                existing_event_id="$thinking_123",
                adopt_existing_placeholder=True,
                room_mode=True,
            )

        assert late_event_thread is not None
        late_event_thread.join(timeout=1.0)
        assert late_event_done.is_set()
        await asyncio.sleep(0.05)

        assert len(captured_texts) == 2
        assert "Preparing isolated worker" in captured_texts[0]
        assert "Preparing isolated worker" not in captured_texts[1]
        assert captured_texts[1].startswith("**[Response interrupted by an error:")

    @pytest.mark.asyncio
    async def test_worker_warmup_coalesces_parallel_calls_on_same_worker_key(self) -> None:
        """Multiple tool calls sharing one worker should render as one warmup line."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_same_worker"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        for tool_name, function_name in (("shell", "run"), ("python", "execute")):
            streaming.apply_worker_progress_event(
                WorkerProgressEvent(
                    tool_name=tool_name,
                    function_name=function_name,
                    progress=WorkerReadyProgress(
                        phase="waiting",
                        worker_key="worker-a",
                        backend_name="kubernetes",
                        elapsed_seconds=10.0,
                    ),
                ),
            )

        await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        content = mock_client.room_send.call_args.kwargs["content"]
        body = content["body"]
        assert body == "Thinking...\n\n⏳ Preparing isolated worker for `shell.run`, `python.execute`... 10s elapsed."
        assert (
            content["formatted_body"]
            == "<p>Thinking...</p>\n<p>⏳ Preparing isolated worker for <code>shell.run</code>, <code>python.execute</code>... 10s elapsed.</p>"
        )

    @pytest.mark.asyncio
    async def test_worker_warmup_renders_multiple_lines_for_distinct_workers(self) -> None:
        """Distinct warming workers should each render their own status line."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_two_workers"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        for worker_key, tool_name in (("worker-a", "shell"), ("worker-b", "python")):
            streaming.apply_worker_progress_event(
                WorkerProgressEvent(
                    tool_name=tool_name,
                    function_name="run" if tool_name == "shell" else "execute",
                    progress=WorkerReadyProgress(
                        phase="waiting",
                        worker_key=worker_key,
                        backend_name="kubernetes",
                        elapsed_seconds=10.0,
                    ),
                ),
            )

        await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        body = mock_client.room_send.call_args.kwargs["content"]["body"]
        assert body.count("Preparing isolated worker") == 2

    @pytest.mark.asyncio
    async def test_worker_warmup_retry_clears_stale_failure_notice_before_text(self) -> None:
        """A new retry should replace the old failed notice for the same tool before normal text arrives."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_retry"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="failed",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=4.0,
                    error="boom",
                ),
            ),
        )
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-b",
                    backend_name="kubernetes",
                    elapsed_seconds=1.0,
                ),
            ),
        )

        await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        body = mock_client.room_send.call_args.kwargs["content"]["body"]
        assert "Preparing isolated worker" in body
        assert "Worker startup failed" not in body

    @pytest.mark.asyncio
    async def test_worker_warmup_failed_status_renders_exact_visible_copy(self) -> None:
        """Visible failed warmups should pin exact plain-text and HTML formatting."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_failed_visible"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
        )
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="failed",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=1.0,
                    error="intentional example",
                ),
            ),
        )

        await streaming._send_or_edit_message(mock_client, allow_empty_progress=True)

        content = mock_client.room_send.call_args.kwargs["content"]
        assert content["body"] == "Thinking...\n\n⚠️ Worker startup failed for `shell.run`: intentional example."
        assert (
            content["formatted_body"]
            == "<p>Thinking...</p>\n<p>⚠️ Worker startup failed for <code>shell.run</code>: intentional example.</p>"
        )

    @pytest.mark.asyncio
    async def test_streamed_success_allows_one_final_response_transform(self) -> None:
        """A clean streamed success may replace the final visible text exactly once."""
        response_envelope = MessageEnvelope(
            source_event_id="$event123",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", None, "$event123"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="helper",
            source_kind="message",
        )
        response_hooks = SimpleNamespace(
            apply_before_response=AsyncMock(
                return_value=SimpleNamespace(
                    response_text="chunk",
                    response_kind="ai",
                    tool_trace=None,
                    extra_content=None,
                    envelope=response_envelope,
                    suppress=False,
                ),
            ),
            apply_final_response_transform=AsyncMock(
                return_value=SimpleNamespace(
                    response_text="updated text",
                    response_kind="ai",
                    envelope=response_envelope,
                ),
            ),
            emit_after_response=AsyncMock(),
            emit_cancelled_response=AsyncMock(),
        )
        gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=SimpleNamespace(
                    client=_make_matrix_client_mock(),
                    orchestrator=None,
                    config=self.config,
                    runtime_started_at=0.0,
                ),
                runtime_paths=runtime_paths_for(self.config),
                agent_name="helper",
                logger=MagicMock(),
                redact_message_event=AsyncMock(return_value=True),
                sender_domain="localhost",
                resolver=MagicMock(),
                response_hooks=response_hooks,
            ),
        )
        object.__setattr__(gateway, "edit_text", AsyncMock(return_value=True))

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", None, "$event123"),
                stream_transport_outcome=StreamTransportOutcome(
                    last_physical_stream_event_id="$streaming",
                    terminal_status="completed",
                    rendered_body="chunk",
                    visible_body_state="visible_body",
                ),
                initial_delivery_kind="sent",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-final-transform-success",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "completed"
        assert outcome.final_visible_event_id == "$streaming"
        assert outcome.final_visible_body == "updated text"
        assert outcome.delivery_kind == "edited"
        response_hooks.apply_before_response.assert_not_awaited()
        response_hooks.apply_final_response_transform.assert_awaited_once()
        gateway.edit_text.assert_awaited_once()
        edited_request = gateway.edit_text.await_args.args[0]
        assert edited_request.event_id == "$streaming"
        assert edited_request.new_text == "updated text"
        lifecycle = ResponseLifecycle(
            ResponseLifecycleDeps(
                response_hooks=response_hooks,
                logger=MagicMock(),
            ),
            response_kind="ai",
            pipeline_timing=None,
            response_envelope=response_envelope,
            correlation_id="corr-final-transform-success",
        )
        finalized = await lifecycle.finalize(
            outcome,
            build_post_response_outcome=lambda _delivered: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=MagicMock()),
        )

        assert finalized.delivery_kind == "edited"
        assert finalized.response_text == "updated text"
        response_hooks.emit_after_response.assert_awaited_once()
        after_kwargs = response_hooks.emit_after_response.await_args.kwargs
        assert after_kwargs["response_text"] == "updated text"
        assert after_kwargs["delivery_kind"] == "edited"
        response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_streamed_success_noop_final_transform_keeps_visible_stream_text(self) -> None:
        """Canonical final content must not rewrite the visible stream unless the hook changes it."""
        response_envelope = MessageEnvelope(
            source_event_id="$event123",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", None, "$event123"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="helper",
            source_kind="message",
        )
        response_hooks = SimpleNamespace(
            apply_before_response=AsyncMock(),
            apply_final_response_transform=AsyncMock(
                return_value=SimpleNamespace(
                    response_text="canonical final",
                    response_kind="ai",
                    envelope=response_envelope,
                ),
            ),
            emit_after_response=AsyncMock(),
            emit_cancelled_response=AsyncMock(),
        )
        gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=SimpleNamespace(
                    client=_make_matrix_client_mock(),
                    orchestrator=None,
                    config=self.config,
                    runtime_started_at=0.0,
                ),
                runtime_paths=runtime_paths_for(self.config),
                agent_name="helper",
                logger=MagicMock(),
                redact_message_event=AsyncMock(return_value=True),
                sender_domain="localhost",
                resolver=MagicMock(),
                response_hooks=response_hooks,
            ),
        )
        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", None, "$event123"),
                stream_transport_outcome=StreamTransportOutcome(
                    last_physical_stream_event_id="$streaming",
                    terminal_status="completed",
                    rendered_body="chunk",
                    visible_body_state="visible_body",
                    canonical_final_body_candidate="canonical final",
                ),
                initial_delivery_kind="sent",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-final-transform-noop",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.final_visible_event_id == "$streaming"
        assert outcome.final_visible_body == "chunk"
        response_hooks.apply_before_response.assert_not_awaited()
        response_hooks.apply_final_response_transform.assert_awaited_once()
        lifecycle = ResponseLifecycle(
            ResponseLifecycleDeps(
                response_hooks=response_hooks,
                logger=MagicMock(),
            ),
            response_kind="ai",
            pipeline_timing=None,
            response_envelope=response_envelope,
            correlation_id="corr-final-transform-noop",
        )
        finalized = await lifecycle.finalize(
            outcome,
            build_post_response_outcome=lambda _delivered: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=MagicMock()),
        )

        assert finalized.delivery_kind == "sent"
        assert finalized.response_text == "chunk"
        response_hooks.emit_after_response.assert_awaited_once()
        after_kwargs = response_hooks.emit_after_response.await_args.kwargs
        assert after_kwargs["response_text"] == "chunk"
        assert after_kwargs["delivery_kind"] == "sent"
        response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_streamed_success_noop_final_transform_uses_matching_visible_interactive_metadata(self) -> None:
        """No-op final transforms should keep interactive metadata when the canonical block matches visible text."""
        response_envelope = MessageEnvelope(
            source_event_id="$event123",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", None, "$event123"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="helper",
            source_kind="message",
        )
        raw_interactive = (
            "```interactive\n"
            '{"question":"Approve?","options":[{"emoji":"✅","label":"Approve","value":"approve"}]}\n'
            "```"
        )
        formatted_interactive = interactive.parse_and_format_interactive(
            raw_interactive,
            extract_mapping=True,
        )
        response_hooks = SimpleNamespace(
            apply_before_response=AsyncMock(),
            apply_final_response_transform=AsyncMock(
                return_value=SimpleNamespace(
                    response_text=raw_interactive,
                    response_kind="ai",
                    envelope=response_envelope,
                ),
            ),
            emit_after_response=AsyncMock(),
            emit_cancelled_response=AsyncMock(),
        )
        gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=SimpleNamespace(
                    client=_make_matrix_client_mock(),
                    orchestrator=None,
                    config=self.config,
                    runtime_started_at=0.0,
                ),
                runtime_paths=runtime_paths_for(self.config),
                agent_name="helper",
                logger=MagicMock(),
                redact_message_event=AsyncMock(return_value=True),
                sender_domain="localhost",
                resolver=MagicMock(),
                response_hooks=response_hooks,
            ),
        )

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", None, "$event123"),
                stream_transport_outcome=StreamTransportOutcome(
                    last_physical_stream_event_id="$streaming",
                    terminal_status="completed",
                    rendered_body=formatted_interactive.formatted_text,
                    visible_body_state="visible_body",
                    canonical_final_body_candidate=raw_interactive,
                ),
                initial_delivery_kind="sent",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-final-transform-interactive",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.final_visible_event_id == "$streaming"
        assert outcome.final_visible_body == formatted_interactive.formatted_text
        assert dict(outcome.option_map or {}) == {"✅": "approve", "1": "approve"}
        assert list(outcome.options_list or ()) == [
            {"emoji": "✅", "label": "Approve", "value": "approve"},
        ]
        response_hooks.apply_final_response_transform.assert_awaited_once()
        response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worker_warmup_failure_notice_clears_before_visible_tool_start(self) -> None:
        """A visible tool-start marker should clear stale worker failure suffixes before sending."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_tool_start"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            show_tool_calls=True,
        )
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="failed",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=4.0,
                    error="boom",
                ),
            ),
        )

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)
        await streaming.finalize(mock_client)

        assert mock_client.room_send.call_count == 2
        first_body = mock_client.room_send.call_args_list[0].kwargs["content"]["body"]
        assert "save_file" in first_body
        assert "Worker startup failed" not in first_body

    @pytest.mark.asyncio
    async def test_worker_warmup_failure_notice_clears_before_hidden_tool_start(self) -> None:
        """A hidden tool-start keepalive should drop stale worker failure suffixes before sending."""
        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$warmup_hidden_tool_start"
        mock_client.room_send.return_value = mock_response

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            show_tool_calls=False,
            progress_update_interval=0.0,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="failed",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=4.0,
                    error="boom",
                ),
            ),
        )

        async def response_stream() -> AsyncIterator[object]:
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        with patch("mindroom.streaming.time.time", return_value=100.25):
            await _consume_streaming_chunks_for_test(mock_client, response_stream(), streaming)
        await streaming.finalize(mock_client)

        assert mock_client.room_send.call_count == 2
        first_body = mock_client.room_send.call_args_list[0].kwargs["content"]["body"]
        assert first_body == "Thinking..."

    @pytest.mark.asyncio
    async def test_hidden_tool_start_force_refresh_clears_inflight_warmup_failure(self) -> None:
        """A hidden tool start should correct an in-flight failed warmup notice immediately."""
        mock_client = _make_matrix_client_mock()
        first_send_started = asyncio.Event()
        allow_first_send_to_finish = asyncio.Event()
        send_count = 0

        async def room_send(*args: object, **kwargs: object) -> nio.RoomSendResponse:
            nonlocal send_count
            _ = (args, kwargs)
            send_count += 1
            if send_count == 1:
                first_send_started.set()
                await allow_first_send_to_finish.wait()
            mock_response = MagicMock()
            mock_response.__class__ = nio.RoomSendResponse
            mock_response.event_id = f"$warmup_hidden_refresh_{send_count}"
            return mock_response

        mock_client.room_send.side_effect = room_send

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
            runtime_paths=runtime_paths_for(self.config),
            show_tool_calls=False,
            update_interval=10.0,
            min_update_interval=10.0,
            progress_update_interval=10.0,
            min_char_update_interval=0.35,
            max_idle=0.25,
        )
        streaming.last_update = 100.0
        streaming.stream_started_at = 100.0
        streaming.apply_worker_progress_event(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="failed",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=4.0,
                    error="boom",
                ),
            ),
        )

        delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
        delivery_task = asyncio.create_task(_drive_stream_delivery(mock_client, streaming, delivery_queue))
        delivery_queue.put_nowait(_DeliveryRequest(force_refresh=True, allow_empty_progress=True))

        async def response_stream() -> AsyncIterator[object]:
            await first_send_started.wait()
            yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))

        streaming_times = iter([100.1, 100.2, 100.3])
        with patch("mindroom.streaming.time.time", side_effect=lambda: next(streaming_times, 100.3)):
            consume_task = asyncio.create_task(
                _consume_streaming_chunks_impl(response_stream(), streaming, delivery_queue),
            )
            await first_send_started.wait()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            allow_first_send_to_finish.set()
            await consume_task
            shutdown_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)

        assert shutdown_error is None
        assert mock_client.room_send.call_count == 2
        bodies = [
            call.kwargs["content"].get("m.new_content", call.kwargs["content"])["body"]
            for call in mock_client.room_send.call_args_list
        ]
        assert "Worker startup failed" in bodies[0]
        assert bodies[1] == "Thinking..."


class TestStreamingConfig:
    """Tests for StreamingConfig and its wiring into send_streaming_response."""

    def test_streaming_config_defaults_match_hardcoded(self) -> None:
        """StreamingConfig defaults must match StreamingResponse dataclass field defaults."""
        sc = StreamingConfig()
        sr = StreamingResponse.__dataclass_fields__
        assert sc.update_interval == sr["update_interval"].default
        assert sc.min_update_interval == sr["min_update_interval"].default
        assert sc.interval_ramp_seconds == sr["interval_ramp_seconds"].default
        assert sc.max_idle == sr["max_idle"].default

    @pytest.mark.asyncio
    async def test_streaming_config_applied(self) -> None:
        """Custom StreamingConfig values should propagate to the StreamingResponse instance."""
        sc = StreamingConfig(update_interval=2.0, min_update_interval=0.3, interval_ramp_seconds=10.0, max_idle=0.7)
        config = Config(
            agents={"a": AgentConfig(display_name="A", rooms=["!r:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            router=RouterConfig(model="default"),
            defaults={"streaming": sc.model_dump()},
        )
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        config = bind_runtime_paths(config, runtime_paths)

        mock_client = _make_matrix_client_mock()
        mock_response = MagicMock()
        mock_response.__class__ = nio.RoomSendResponse
        mock_response.event_id = "$cfg_test"
        mock_client.room_send.return_value = mock_response

        async def empty_stream() -> AsyncIterator[str]:
            yield "hello"

        captured: list[StreamingResponse] = []
        original_cls = StreamingResponse

        class CapturingStreamingResponse(original_cls):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                captured.append(self)

        outcome = await send_streaming_response(
            client=mock_client,
            room_id="!r:localhost",
            reply_to_event_id="$orig",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths,
            response_stream=empty_stream(),
            streaming_cls=CapturingStreamingResponse,
            room_mode=True,
        )

        event_id = outcome.last_physical_stream_event_id
        text = outcome.rendered_body
        assert event_id == "$cfg_test"
        assert text is not None
        assert text == "hello"
        assert len(captured) == 1
        sr = captured[0]
        assert sr.update_interval == 2.0
        assert sr.min_update_interval == 0.3
        assert sr.interval_ramp_seconds == 10.0
        assert sr.max_idle == 0.7

    def test_streaming_config_partial_override(self) -> None:
        """Setting only update_interval via Config should keep other fields at defaults."""
        config = Config(
            agents={"a": AgentConfig(display_name="A", rooms=["!r:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            router=RouterConfig(model="default"),
            defaults={"streaming": {"update_interval": 2.0}},
        )
        sc = config.defaults.streaming
        assert sc.update_interval == 2.0
        assert sc.min_update_interval == 0.5
        assert sc.interval_ramp_seconds == 15.0
        assert sc.max_idle == 2.0

    def test_streaming_config_validation(self) -> None:
        """Reject invalid values: update_interval <= 0, min_update_interval <= 0, interval_ramp_seconds < 0."""
        with pytest.raises(ValueError, match="greater than 0"):
            StreamingConfig(update_interval=0)
        with pytest.raises(ValueError, match="greater than 0"):
            StreamingConfig(update_interval=-1)
        with pytest.raises(ValueError, match="greater than 0"):
            StreamingConfig(min_update_interval=0)
        with pytest.raises(ValueError, match="greater than 0"):
            StreamingConfig(min_update_interval=-0.5)
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            StreamingConfig(interval_ramp_seconds=-1)
        with pytest.raises(ValidationError, match="greater than 0"):
            StreamingConfig(max_idle=0)
        with pytest.raises(ValidationError, match="greater than 0"):
            StreamingConfig(max_idle=-0.25)
        # interval_ramp_seconds=0 should be valid (disables ramp)
        sc = StreamingConfig(interval_ramp_seconds=0)
        assert sc.interval_ramp_seconds == 0
