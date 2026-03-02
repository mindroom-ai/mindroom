"""Comprehensive unit tests for streaming behavior with agent edits."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import (
    _CANCELLED_RESPONSE_NOTE,
    _PROGRESS_PLACEHOLDER,
    IN_PROGRESS_MARKER,
    ReplacementStreamingResponse,
    StreamingResponse,
    is_in_progress_message,
    send_streaming_response,
)
from tests.conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


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
        self.config = Config(
            agents={
                "helper": AgentConfig(display_name="HelperAgent", rooms=["!test:localhost"]),
                "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
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
        )
        helper_bot.client = AsyncMock()

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
        )
        calc_bot.client = AsyncMock()

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
        )
        calc_bot.client = AsyncMock()

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
        mock_client = AsyncMock()
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

    @pytest.mark.asyncio
    async def test_throttled_send_uses_ramp_interval(self) -> None:
        """Integration test: _throttled_send respects the ramped interval."""
        mock_client = AsyncMock()
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
        mock_client = AsyncMock()
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
        mock_client = AsyncMock()
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
        mock_client = AsyncMock()

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
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
        assert edit_args[3]["body"].startswith(_PROGRESS_PLACEHOLDER)
        assert IN_PROGRESS_MARKER in edit_args[3]["body"]

    @pytest.mark.asyncio
    async def test_progress_hint_creates_initial_message_on_cold_start(self) -> None:
        """Tool-first streams with hidden tool calls should create an initial placeholder message."""
        mock_client = AsyncMock()
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
        assert sent_content["body"].startswith(_PROGRESS_PLACEHOLDER)
        assert IN_PROGRESS_MARKER in sent_content["body"]

    @pytest.mark.asyncio
    async def test_finalize_strips_marker_from_placeholder_only_stream(self) -> None:
        """Finalize should edit out the in-progress marker even when no text was ever emitted."""
        mock_client = AsyncMock()
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

        # Verify the initial message has the in-progress marker
        initial_content = mock_client.room_send.call_args[1]["content"]
        assert IN_PROGRESS_MARKER in initial_content["body"]

        # Now finalize with no text ever emitted
        with patch(
            "mindroom.streaming.edit_message",
            new=AsyncMock(return_value="$placeholder_msg"),
        ) as mock_edit:
            await streaming.finalize(mock_client)

        assert mock_edit.await_count == 1
        final_body = mock_edit.await_args.args[3]["body"]
        assert final_body == _PROGRESS_PLACEHOLDER
        assert IN_PROGRESS_MARKER not in final_body

    @pytest.mark.asyncio
    async def test_finalize_does_not_overwrite_existing_message_without_placeholder(self) -> None:
        """Finalize should not force a placeholder onto arbitrary existing messages."""
        mock_client = AsyncMock()

        streaming = StreamingResponse(
            room_id="!test:localhost",
            reply_to_event_id="$original_123",
            thread_id=None,
            sender_domain="localhost",
            config=self.config,
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
    async def test_streaming_in_progress_marker(
        self,
        mock_helper_agent: AgentMatrixUser,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test that in-progress marker is shown during streaming but not in final message."""
        # Create a mock client
        mock_client = AsyncMock()
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
        )

        # Stream some content
        await streaming.update_content("Hello world", mock_client)

        # Check that the sent message includes the in-progress marker
        first_call = mock_client.room_send.call_args_list[0]
        content = first_call[1]["content"]
        # The body should contain the in-progress marker
        assert IN_PROGRESS_MARKER in content["body"]
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
        mock_client = AsyncMock()
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
                response_stream=cancelling_stream(),
                existing_event_id="$thinking_123",
                room_mode=True,
            )

        assert len(edited_texts) == 2
        assert IN_PROGRESS_MARKER in edited_texts[0]
        assert edited_texts[-1] == f"Partial answer\n\n{_CANCELLED_RESPONSE_NOTE}"
