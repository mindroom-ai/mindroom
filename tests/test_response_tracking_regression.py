"""Regression tests for response tracking bugs.

These tests ensure that commands, unknown commands, and router messages
are properly tracked to prevent re-processing after restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.commands import Command, CommandType
from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def mock_router_agent() -> AgentMatrixUser:
    """Create a mock router agent user."""
    return AgentMatrixUser(
        agent_name="router",
        password=TEST_PASSWORD,
        display_name="RouterAgent",
        user_id="@mindroom_router:localhost",
    )


@pytest.fixture
def mock_config() -> Config:
    """Create a mock config with some agents."""
    return Config(
        agents={
            "calculator": AgentConfig(display_name="Calculator", rooms=["!test:localhost"]),
            "research": AgentConfig(display_name="Research", rooms=["!test:localhost"]),
        },
        teams={},
        room_models={},
        models={"default": ModelConfig(provider="anthropic", id="claude-3-5-haiku-latest")},
    )


class TestResponseTrackingRegression:
    """Regression tests for response tracking issues."""

    @pytest.mark.asyncio
    async def test_command_response_tracking(
        self,
        mock_router_agent: AgentMatrixUser,
        mock_config: Config,
        tmp_path: Path,
    ) -> None:
        """Test that commands are tracked in response tracker.

        Regression test for issue where commands like !schedule would be
        re-processed after bot restart.
        """
        test_room_id = "!test:localhost"

        # Set up router bot (only router handles commands)
        bot = AgentBot(
            agent_user=mock_router_agent,
            config=mock_config,
            storage_path=tmp_path,
            enable_streaming=False,
            rooms=[test_room_id],
        )
        bot.client = AsyncMock()
        bot.client.user_id = mock_router_agent.user_id

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        bot.client.room_send.return_value = mock_send_response

        # Create a help command
        command = Command(type=CommandType.HELP, args={"topic": None}, raw_text="!help")

        # Create command event
        command_event = MagicMock(spec=nio.RoomMessageText)
        command_event.sender = "@user:localhost"
        command_event.body = "!help"
        command_event.event_id = "$command_123"
        command_event.source = {
            "content": {
                "body": "!help",
            },
        }

        mock_room = MagicMock()
        mock_room.room_id = test_room_id

        # Process command first time
        await bot._handle_command(mock_room, command_event, command)

        # Verify response was sent
        assert bot.client.room_send.call_count == 1

        # IMPORTANT: Check if event was marked as responded
        # This should be True after the fix
        assert bot.response_tracker.has_responded(command_event.event_id), (
            "Command event should be marked as responded to prevent re-processing"
        )

        # Reset mock
        bot.client.room_send.reset_mock()

        # Process same command again (simulating restart)
        await bot._handle_command(mock_room, command_event, command)

        # Should NOT send another response if properly tracked
        # (In real scenario, _should_skip_duplicate_response would prevent this)
        # But here we're testing that the tracking was done

    @pytest.mark.asyncio
    async def test_unknown_command_response_tracking(
        self,
        mock_router_agent: AgentMatrixUser,
        mock_config: Config,
        tmp_path: Path,
    ) -> None:
        """Test that unknown commands are tracked in response tracker.

        Regression test for issue where unknown commands would trigger
        error messages repeatedly after restart.
        """
        test_room_id = "!test:localhost"

        # Set up router bot
        bot = AgentBot(
            agent_user=mock_router_agent,
            config=mock_config,
            storage_path=tmp_path,
            enable_streaming=False,
            rooms=[test_room_id],
        )
        bot.client = AsyncMock()
        bot.client.user_id = mock_router_agent.user_id

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_456"
        bot.client.room_send.return_value = mock_send_response

        # Create unknown command event
        unknown_command_event = MagicMock(spec=nio.RoomMessageText)
        unknown_command_event.sender = "@user:localhost"
        unknown_command_event.body = "!unknowncommand"
        unknown_command_event.event_id = "$unknown_cmd_123"
        unknown_command_event.source = {
            "content": {
                "body": "!unknowncommand",
            },
        }

        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {mock_router_agent.user_id: MagicMock()}

        # Mock the necessary methods for _on_message flow
        bot._extract_message_context = AsyncMock()  # type: ignore[method-assign]
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        mock_context.mentioned_agents = []
        bot._extract_message_context.return_value = mock_context

        # Mock the _send_response to track the call
        original_send_response = bot._send_response
        bot._send_response = AsyncMock(side_effect=original_send_response)  # type: ignore[method-assign]

        # Mock constants to make router handle commands
        with patch("mindroom.constants.ROUTER_AGENT_NAME", "router"):
            # Call _on_message which should detect unknown command and respond
            await bot._on_message(mock_room, unknown_command_event)

        # Verify that _send_response was called with the error message
        bot._send_response.assert_called_once()
        call_args = bot._send_response.call_args[0]
        assert "❌ Unknown command" in call_args[2]

        # IMPORTANT: Check if event was marked as responded
        # This should be True after the fix in bot.py at line 371
        assert bot.response_tracker.has_responded(unknown_command_event.event_id), (
            "Unknown command event should be marked as responded"
        )

    @pytest.mark.asyncio
    @patch("mindroom.bot.suggest_agent_for_message")
    async def test_router_ai_routing_response_tracking(
        self,
        mock_suggest_agent: AsyncMock,
        mock_router_agent: AgentMatrixUser,
        mock_config: Config,
        tmp_path: Path,
    ) -> None:
        """Test that router AI routing is tracked in response tracker.

        Regression test for issue where router would re-route messages
        after restart.
        """
        test_room_id = "!test:localhost"

        # Set up router bot
        bot = AgentBot(
            agent_user=mock_router_agent,
            config=mock_config,
            storage_path=tmp_path,
            enable_streaming=False,
            rooms=[test_room_id],
        )
        bot.client = AsyncMock()
        bot.client.user_id = mock_router_agent.user_id

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$router_response_123"
        bot.client.room_send.return_value = mock_send_response

        # Mock suggest_agent to return "research"
        mock_suggest_agent.return_value = "research"

        # Create a regular message (no mentions)
        message_event = MagicMock(spec=nio.RoomMessageText)
        message_event.sender = "@user:localhost"
        message_event.body = "What is quantum computing?"
        message_event.event_id = "$user_msg_789"
        message_event.source = {
            "content": {
                "body": "What is quantum computing?",
            },
        }

        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {
            mock_router_agent.user_id: MagicMock(),
            "@mindroom_research:localhost": MagicMock(),
        }

        # Process routing
        await bot._handle_ai_routing(mock_room, message_event, [])

        # Verify routing message was sent
        assert bot.client.room_send.call_count == 1
        # NOTE: The fix should add mark_responded() in bot.py at line 787
        # We're NOT adding it here in the test - we're testing that bot.py does it

        # IMPORTANT: Check if event was marked as responded
        # This should be True after the fix
        assert bot.response_tracker.has_responded(message_event.event_id), (
            "Router event should be marked as responded to prevent re-routing"
        )

        # Reset mock
        bot.client.room_send.reset_mock()
        mock_suggest_agent.reset_mock()

        # Process same message again (simulating restart)
        await bot._handle_ai_routing(mock_room, message_event, [])

        # With proper tracking, this shouldn't happen again
        # (In real scenario, _should_skip_duplicate_response would prevent reaching here)
