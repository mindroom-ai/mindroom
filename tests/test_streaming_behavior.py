"""Comprehensive unit tests for streaming behavior with agent edits."""

from __future__ import annotations

import asyncio
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig, StreamingConfig
from mindroom.constants import STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.streaming import (
    CANCELLED_RESPONSE_NOTE,
    IN_PROGRESS_MARKER,
    PROGRESS_PLACEHOLDER,
    ReplacementStreamingResponse,
    StreamingResponse,
    clean_partial_reply_text,
    is_in_progress_message,
    is_interrupted_partial_reply,
    send_streaming_response,
)
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _aiter(*events: object) -> AsyncIterator[object]:
    for event in events:
        yield event


def _make_matrix_client_mock() -> AsyncMock:
    client = AsyncMock()
    client.rooms = {}
    client.add_event_callback = MagicMock()
    client.add_response_callback = MagicMock()
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    return client


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
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.stream_agent_response")
    @patch("mindroom.bot.should_use_streaming")
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
        with patch("mindroom.bot.check_agent_mentioned") as mock_check:
            mock_check.return_value = ([MatrixID.parse("@mindroom_helper:localhost")], True, False)

            # Process message with helper bot - it should stream a response
            await helper_bot._on_message(mock_room, user_event)

        # Verify helper bot sent initial message and edit
        assert helper_bot.client.room_send.call_count >= 1  # At least initial message

        # Simulate the initial message from helper (with in-progress marker)
        initial_event = MagicMock(spec=nio.RoomMessageText)
        initial_event.sender = "@mindroom_helper:localhost"
        initial_event.body = "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2? ⋯"
        initial_event.event_id = "$helper_response_123"
        initial_event.server_timestamp = 1234567890
        initial_event.source = {
            "content": {
                "body": "Let me help with that calculation. @mindroom_calculator:localhost what's 2+2? ⋯",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Process initial message - calculator should NOT respond (has in-progress marker)
        with patch("mindroom.bot.check_agent_mentioned") as mock_check:
            mock_check.return_value = ([MatrixID.parse("@mindroom_calculator:localhost")], True, False)

            # Debug: let's see what happens
            calc_bot.logger.info(f"Processing initial message: '{initial_event.body}'")

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
        with patch("mindroom.bot.check_agent_mentioned") as mock_check:
            mock_check.return_value = ([MatrixID.parse("@mindroom_calculator:localhost")], True, False)
            with patch("mindroom.bot.extract_agent_name") as mock_extract:
                # Make extract_agent_name return 'helper' for the sender
                mock_extract.return_value = "helper"
                await calc_bot._on_message(mock_room, final_event)

        assert calc_bot.client.room_send.call_count == 2  # thinking + final
        assert mock_ai_response.call_count == 1

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
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

    def test_is_in_progress_message_detects_animated_suffix(self) -> None:
        """In-progress detection should handle animated marker suffixes."""
        assert is_in_progress_message("Thinking... ⋯")
        assert is_in_progress_message("Thinking... ⋯.")
        assert is_in_progress_message("Thinking... ⋯..")
        assert not is_in_progress_message("Thinking...")
        assert not is_in_progress_message(None)

    def test_is_interrupted_partial_reply_detects_terminal_markers(self) -> None:
        """Interrupted partial-reply detection should recognize shared cancelled/error notes."""
        assert is_interrupted_partial_reply(f"Draft answer\n\n{CANCELLED_RESPONSE_NOTE}")
        assert is_interrupted_partial_reply("Draft answer\n\n**[Response interrupted by an error: boom]**")
        assert not is_interrupted_partial_reply("Finished answer")
        assert not is_interrupted_partial_reply(None)

    def test_clean_partial_reply_text_strips_shared_markers(self) -> None:
        """Shared partial-reply cleanup should normalize cancelled/error/placeholder bodies."""
        assert clean_partial_reply_text(f"Draft answer\n\n{CANCELLED_RESPONSE_NOTE}") == "Draft answer"
        assert (
            clean_partial_reply_text("Draft answer\n\n**[Response interrupted by an error: boom]**") == "Draft answer"
        )
        assert clean_partial_reply_text(f"{PROGRESS_PLACEHOLDER} ⋯") == ""
        assert clean_partial_reply_text("... ⋯") == ""

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
                "mindroom.streaming.edit_message",
                new=AsyncMock(return_value="$existing_event"),
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
            "mindroom.streaming.edit_message",
            new=AsyncMock(return_value="$placeholder_msg"),
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
            "mindroom.streaming.edit_message",
            new=AsyncMock(return_value="$existing_msg"),
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
        ) -> str:
            edited_contents.append((new_content, new_text))
            return "$edit"

        async def empty_stream() -> AsyncIterator[str]:
            if False:
                yield ""
            return

        with patch("mindroom.streaming.edit_message", new=AsyncMock(side_effect=record_edit)):
            event_id, accumulated = await send_streaming_response(
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

        assert event_id == "$thinking_123"
        assert accumulated == ""
        assert len(edited_contents) == 1
        final_content, final_text = edited_contents[0]
        assert final_text == PROGRESS_PLACEHOLDER
        assert final_content["body"] == PROGRESS_PLACEHOLDER
        assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED

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

        async def empty_request_knowledge_managers(
            _agent_names: list[str],
            _execution_identity: object,
        ) -> dict[str, object]:
            return {}

        async def no_latest_thread_event(
            _client: object,
            _room_id: str,
            _thread_id: str | None,
            _reply_to_event_id: str | None,
            _existing_event_id: str | None = None,
        ) -> None:
            return None

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
        bot.client = MagicMock(rooms={})
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._ensure_request_knowledge_managers = empty_request_knowledge_managers
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            room_id="!test:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id=None,
                reply_to_event_id="$reply_plain:localhost",
                safe_thread_root="$thread_root:localhost",
            ),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="Continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="helper",
            source_kind="message",
        )

        async def record_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
            sent_contents.append(content)
            return "$stream_1"

        async def record_edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            _new_content: dict[str, object],
            _new_text: str,
        ) -> str:
            return "$stream_1"

        with (
            patch("mindroom.bot.stream_agent_response", new=MagicMock(return_value=response_stream())),
            patch("mindroom.bot.typing_indicator", new=noop_typing),
            patch("mindroom.streaming.get_latest_thread_event_id_if_needed", new=no_latest_thread_event),
            patch("mindroom.streaming.send_message", new=record_send),
            patch("mindroom.streaming.edit_message", new=record_edit),
        ):
            delivery = await bot._process_and_respond_streaming(
                room_id="!test:localhost",
                prompt="Continue",
                reply_to_event_id="$reply_plain:localhost",
                thread_id=None,
                thread_history=[],
                existing_event_id=None,
                user_id="@user:localhost",
                response_envelope=envelope,
                correlation_id="$request:localhost",
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

        async def record_send(_client: object, room_id: str, content: dict[str, object]) -> str:
            sent_messages.append((room_id, content))
            return "$stream_1"

        with patch("mindroom.streaming.send_message", new=record_send):
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
        ) -> str:
            edited_texts.append(new_text)
            return "$edit"

        async def cancelling_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            raise asyncio.CancelledError

        with (
            patch("mindroom.streaming.edit_message", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(asyncio.CancelledError),
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
        ) -> str:
            edited_texts.append(new_text)
            return "$edit"

        async def failing_stream() -> AsyncIterator[str]:
            yield "Partial answer"
            msg = "model backend disconnected"
            raise RuntimeError(msg)

        with (
            patch("mindroom.streaming.edit_message", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(RuntimeError, match="model backend disconnected"),
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

        assert len(edited_texts) == 2
        assert IN_PROGRESS_MARKER not in edited_texts[0]
        final_text = edited_texts[-1]
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
        ) -> str:
            edited_texts.append(new_text)
            return "$edit"

        async def failing_stream() -> AsyncIterator[str]:
            if False:
                yield ""
            msg = "provider stream failed"
            raise RuntimeError(msg)

        with (
            patch("mindroom.streaming.edit_message", new=AsyncMock(side_effect=record_edit)),
            pytest.raises(RuntimeError, match="provider stream failed"),
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

        assert len(edited_texts) == 1
        final_text = edited_texts[0]
        assert final_text.startswith("**[Response interrupted by an error:")
        assert "provider stream failed" in final_text
        assert IN_PROGRESS_MARKER not in final_text


class TestStreamingConfig:
    """Tests for StreamingConfig and its wiring into send_streaming_response."""

    def test_streaming_config_defaults_match_hardcoded(self) -> None:
        """StreamingConfig defaults must match StreamingResponse dataclass field defaults."""
        sc = StreamingConfig()
        sr = StreamingResponse.__dataclass_fields__
        assert sc.update_interval == sr["update_interval"].default
        assert sc.min_update_interval == sr["min_update_interval"].default
        assert sc.interval_ramp_seconds == sr["interval_ramp_seconds"].default

    @pytest.mark.asyncio
    async def test_streaming_config_applied(self) -> None:
        """Custom StreamingConfig values should propagate to the StreamingResponse instance."""
        sc = StreamingConfig(update_interval=2.0, min_update_interval=0.3, interval_ramp_seconds=10.0)
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

        with patch("mindroom.streaming.get_latest_thread_event_id_if_needed", new=AsyncMock(return_value=None)):
            event_id, text = await send_streaming_response(
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

        assert event_id == "$cfg_test"
        assert text == "hello"
        assert len(captured) == 1
        sr = captured[0]
        assert sr.update_interval == 2.0
        assert sr.min_update_interval == 0.3
        assert sr.interval_ramp_seconds == 10.0

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
        # interval_ramp_seconds=0 should be valid (disables ramp)
        sc = StreamingConfig(interval_ramp_seconds=0)
        assert sc.interval_ramp_seconds == 0
