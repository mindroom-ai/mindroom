"""Regression tests for response tracking bugs.

These tests ensure that commands, unknown commands, and router messages
are properly tracked to prevent re-processing after restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.commands.parsing import Command, CommandType
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.turn_record import TurnRecord
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_runtime_journal_support,
    install_send_response_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)

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
def mock_config(tmp_path: Path) -> Config:
    """Create a mock config with some agents."""
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["!test:localhost"]),
                "research": AgentConfig(display_name="Research", rooms=["!test:localhost"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="anthropic", id="claude-haiku-4-5")},
        ),
        runtime_paths,
    )


class TestResponseTrackingRegression:
    """Regression tests for response tracking issues."""

    @pytest.mark.asyncio
    async def test_command_response_tracking(
        self,
        mock_router_agent: AgentMatrixUser,
        mock_config: Config,
    ) -> None:
        """Test that commands are tracked in response tracker.

        Regression test for issue where commands like !schedule would be
        re-processed after bot restart.
        """
        test_room_id = "!test:localhost"

        mock_config.defaults.enable_streaming = False
        # Set up router bot (only router handles commands)
        bot = make_test_agent_bot(
            agent_user=mock_router_agent,
            config=mock_config,
            runtime_paths=runtime_paths_for(mock_config),
            rooms=[test_room_id],
        )
        wrap_extracted_collaborators(bot)
        bot.client = make_matrix_client_mock(user_id=mock_router_agent.user_id)
        install_runtime_journal_support(bot)

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
        command_event.server_timestamp = 1234567890
        command_event.source = {
            "content": {
                "body": "!help",
            },
        }

        mock_room = MagicMock()
        mock_room.room_id = test_room_id

        # Process command first time
        await bot._command_turn_executor.execute(
            mock_room,
            command_event,
            "@user:localhost",
            command,
            target=MessageTarget.resolve(test_room_id, None, "$command_123", thread_start_root_event_id="$command_123"),
            handled_turn=TurnRecord.create([command_event.event_id]),
            acts_for_requester=False,
        )

        # Verify response was sent
        assert bot.client.room_send.call_count == 1

        # IMPORTANT: Check if event was marked as responded
        assert await bot._turn_store.is_handled(test_room_id, command_event.event_id), (
            "Command event should be marked as responded to prevent re-processing"
        )

        # Reset mock
        bot.client.room_send.reset_mock()

        # Process same command again (simulating restart)
        handled_turn = await bot._turn_store.get_turn_record(command_event.event_id)
        assert handled_turn is not None
        await bot._command_turn_executor.execute(
            mock_room,
            command_event,
            "@user:localhost",
            command,
            target=MessageTarget.resolve(test_room_id, None, "$command_123", thread_start_root_event_id="$command_123"),
            handled_turn=handled_turn,
            acts_for_requester=False,
        )

        # Should NOT send another response if properly tracked
        # But here we're testing that the tracking was done

    @pytest.mark.asyncio
    async def test_unknown_command_response_tracking(
        self,
        mock_router_agent: AgentMatrixUser,
        mock_config: Config,
    ) -> None:
        """Test that unknown commands are tracked in response tracker.

        Regression test for issue where unknown commands would trigger
        error messages repeatedly after restart.
        """
        test_room_id = "!test:localhost"

        mock_config.defaults.enable_streaming = False
        # Set up router bot
        bot = make_test_agent_bot(
            agent_user=mock_router_agent,
            config=mock_config,
            runtime_paths=runtime_paths_for(mock_config),
            rooms=[test_room_id],
        )
        wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.client.user_id = mock_router_agent.user_id
        install_runtime_journal_support(bot)

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
        unknown_command_event.server_timestamp = 1234567890
        unknown_command_event.source = {
            "content": {
                "body": "!unknowncommand",
            },
        }

        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {mock_router_agent.user_id: MagicMock()}

        # Mock the necessary methods for _on_message flow
        bot._conversation_resolver.extract_dispatch_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )

        send_response = AsyncMock(return_value="$response_456")
        install_send_response_mock(bot, send_response)

        # Call _on_message which should detect unknown command and respond
        await bot._on_message(mock_room, unknown_command_event)
        await drain_coalescing(bot)

        send_response.assert_awaited_once()
        assert "❌ Unknown command" in send_response.await_args.kwargs["response_text"]

        # IMPORTANT: Check if event was marked as responded
        assert await bot._turn_store.is_handled(test_room_id, unknown_command_event.event_id), (
            "Unknown command event should be marked as responded"
        )
