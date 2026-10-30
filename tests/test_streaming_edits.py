"""Test streaming edit handling to prevent duplicate responses."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_edit_regeneration_mock,
    install_runtime_journal_support,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot


async def _empty_event_iterator() -> AsyncGenerator[object, None]:
    if False:
        yield None


def _answer_stream(text: str) -> Callable[..., AsyncIterator[str]]:
    """Return a turn stream that answers ``text`` each time a turn runs."""

    def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        async def chunks() -> AsyncIterator[str]:
            yield text

        return chunks()

    return stream


def _make_matrix_client_mock() -> AsyncMock:
    client = make_matrix_client_mock(user_id="@mindroom_calculator:localhost")
    client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _empty_event_iterator())
    return client


def _delivery_resolution(response_event_id: str | None) -> str | None:
    return response_event_id


def setup_test_bot(
    agent: AgentMatrixUser,
    storage_path: Path,
    room_id: str,
    config: Config | None = None,
) -> AgentBot:
    """Set up a non-streaming test bot with all required mocks."""
    runtime_paths = test_runtime_paths(storage_path)
    if config is None:
        config = bind_runtime_paths(Config(router=RouterConfig(model="default")), runtime_paths)
    else:
        try:
            runtime_paths_for(config)
        except KeyError:
            config = bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(config, runtime_paths_for(config))
    config.defaults.enable_streaming = False

    bot = make_test_agent_bot(
        agent,
        config,
        runtime_paths_for(config),
        rooms=[room_id],
    )
    install_direct_response_admission(bot)
    bot.client = _make_matrix_client_mock()
    install_runtime_journal_support(bot)
    bot._turn_ingress.deps.resolver.dispatch_thread_snapshot = AsyncMock(
        return_value=thread_history_result([], is_full_history=False),
    )
    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
    )

    return bot


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Create a mock agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password=TEST_PASSWORD,
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


class TestStreamingEdits:
    """Test that streaming edits don't trigger duplicate responses."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = with_current_room_member_access(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                    "helper": AgentConfig(display_name="HelperAgent", rooms=["!test:localhost"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.stream_agent_response")
    async def test_agent_regenerates_on_user_edits(
        self,
        mock_stream_agent_response: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents regenerate their response when users edit messages."""
        # Set up bot
        bot = setup_test_bot(mock_agent_user, tmp_path, "!test:localhost", config=self.config)

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Mock the turn's answer
        mock_stream_agent_response.side_effect = _answer_stream("I can help with that!")

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Initial message mentioning the agent
        initial_event = MagicMock()
        initial_event.sender = "@user:localhost"
        initial_event.body = "@mindroom_calculator:localhost: What's 2+2?"
        initial_event.event_id = "$initial123"
        initial_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Process initial message - bot should respond
        await bot._on_message(mock_room, initial_event)
        await drain_coalescing(bot)
        assert bot.client.room_send.call_count == 2  # thinking + final
        assert mock_stream_agent_response.call_count == 1
        await bot._turn_store.record_turn(
            TurnRecord.create(["$initial123"], response_event_id="$response123"),
            room_id="!test:localhost",
        )

        # Reset mocks
        bot.client.room_send.reset_mock()
        mock_stream_agent_response.reset_mock()

        # Edit event 1 - simulating streaming update
        edit_event1 = MagicMock()
        edit_event1.sender = "@user:localhost"
        edit_event1.body = "* @mindroom_calculator:localhost: What's 2+2? Can you show the work?"
        edit_event1.event_id = "$edit1"
        edit_event1.server_timestamp = 1000
        edit_event1.source = {
            "content": {
                "body": "* @mindroom_calculator:localhost: What's 2+2? Can you show the work?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$initial123",  # References the original message
                },
                "m.new_content": {
                    "body": "@mindroom_calculator:localhost: What's 2+2? Can you show the work?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            },
        }

        # Process edit - bot SHOULD regenerate its response.
        mock_generate_response = AsyncMock(return_value=_delivery_resolution("$response123"))
        install_edit_regeneration_mock(bot, mock_generate_response)
        await bot._on_message(mock_room, edit_event1)
        await drain_coalescing(bot)
        assert mock_generate_response.await_count == 1

        # Edit event 2 - another streaming update
        edit_event2 = MagicMock()
        edit_event2.sender = "@user:localhost"
        edit_event2.body = "* @mindroom_calculator:localhost: What's 2+2? Can you show the work step by step?"
        edit_event2.event_id = "$edit2"
        edit_event2.server_timestamp = 2000
        edit_event2.source = {
            "content": {
                "body": "* @mindroom_calculator:localhost: What's 2+2? Can you show the work step by step?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$initial123",  # Still references the original
                },
                "m.new_content": {
                    "body": "@mindroom_calculator:localhost: What's 2+2? Can you show the work step by step?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            },
        }

        # Reset mocks again
        bot.client.room_send.reset_mock()
        mock_stream_agent_response.reset_mock()

        # Process second edit - bot should regenerate again.
        mock_generate_response = AsyncMock(return_value=_delivery_resolution("$response123"))
        install_edit_regeneration_mock(bot, mock_generate_response)
        await bot._on_message(mock_room, edit_event2)
        await drain_coalescing(bot)
        assert mock_generate_response.await_count == 1

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.stream_agent_response")
    async def test_agent_responds_to_new_messages_after_edits(
        self,
        mock_stream_agent_response: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents still respond to new messages after seeing edits."""
        # Set up bot
        bot = setup_test_bot(mock_agent_user, tmp_path, "!test:localhost", config=self.config)

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Mock the turn's answer
        mock_stream_agent_response.side_effect = _answer_stream("Here's the answer!")

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Mark that we already responded to some original message
        await bot._turn_store.record_turn(TurnRecord.create(["$original123"]), room_id="!test:localhost")

        # New message (NOT an edit) mentioning the agent
        new_event = MagicMock()
        new_event.sender = "@user:localhost"
        new_event.body = "@mindroom_calculator:localhost: What's 5+5?"
        new_event.event_id = "$new456"
        new_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 5+5?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Process new message - bot SHOULD respond
        await bot._on_message(mock_room, new_event)
        await drain_coalescing(bot)
        assert bot.client.room_send.call_count == 2  # thinking + final
        assert mock_stream_agent_response.call_count == 1

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.stream_agent_response")
    async def test_agent_ignores_all_edits_from_agents(
        self,
        mock_stream_agent_response: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents ignore ALL edits from other agents, even first-time mentions."""
        # Set up bot
        bot = setup_test_bot(mock_agent_user, tmp_path, "!test:localhost", config=self.config)

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Mock the turn's answer
        mock_stream_agent_response.side_effect = _answer_stream("I can help with that!")

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Initial message from another agent WITHOUT mentioning calculator
        initial_event = MagicMock()
        initial_event.sender = "@mindroom_helper:localhost"  # Another agent
        initial_event.body = "Let me calculate something..."
        initial_event.event_id = "$initial123"
        initial_event.source = {
            "content": {
                "body": "Let me calculate something...",
            },
        }

        # Process initial message - calculator should NOT respond (not mentioned)
        await bot._on_message(mock_room, initial_event)
        await drain_coalescing(bot)
        assert bot.client.room_send.call_count == 0
        assert mock_stream_agent_response.call_count == 0

        # Edit from agent that NOW mentions calculator (with in-progress marker)
        edit_event = MagicMock()
        edit_event.sender = "@mindroom_helper:localhost"  # Same agent
        edit_event.body = "* Let me calculate something... @mindroom_calculator:localhost can you help? ⋯"
        edit_event.event_id = "$edit1"
        edit_event.source = {
            "content": {
                "body": "* Let me calculate something... @mindroom_calculator:localhost can you help? ⋯",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$initial123",
                },
                "m.new_content": {
                    "body": "Let me calculate something... @mindroom_calculator:localhost can you help? ⋯",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            },
        }

        # Process edit - calculator should STILL NOT respond (it's an edit from an agent)
        await bot._on_message(mock_room, edit_event)
        await drain_coalescing(bot)
        assert bot.client.room_send.call_count == 0
        assert mock_stream_agent_response.call_count == 0

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.stream_agent_response")
    async def test_agent_responds_to_user_edits_with_new_mentions(
        self,
        mock_stream_agent_response: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agents DO NOT respond to user edits (even those that add mentions).

        This is by design - the bot ignores all edits to prevent confusion.
        Users should send a new message if they want a new response after editing.
        """
        # Set up bot
        bot = setup_test_bot(mock_agent_user, tmp_path, "!test:localhost", config=self.config)

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Mock the turn's answer
        mock_stream_agent_response.side_effect = _answer_stream("I can help with that!")

        # Set up room
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        # Initial message from user WITHOUT mentioning calculator
        initial_event = MagicMock()
        initial_event.sender = "@user:localhost"  # Regular user
        initial_event.body = "I need some help..."
        initial_event.event_id = "$initial123"
        initial_event.source = {
            "content": {
                "body": "I need some help...",
            },
        }

        # Process initial message - calculator should NOT respond (not mentioned)
        await bot._on_message(mock_room, initial_event)
        await drain_coalescing(bot)
        assert bot.client.room_send.call_count == 0
        assert mock_stream_agent_response.call_count == 0

        # Edit from user that NOW mentions calculator
        edit_event = MagicMock()
        edit_event.sender = "@user:localhost"  # Same user
        edit_event.body = "* I need some help... @mindroom_calculator:localhost what's 2+2?"
        edit_event.event_id = "$edit1"
        edit_event.source = {
            "content": {
                "body": "* I need some help... @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$initial123",
                },
                "m.new_content": {
                    "body": "I need some help... @mindroom_calculator:localhost what's 2+2?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            },
        }

        # Process edit - calculator should NOT respond (bot ignores all edits)
        await bot._on_message(mock_room, edit_event)
        await drain_coalescing(bot)
        assert bot.client.room_send.call_count == 0
        assert mock_stream_agent_response.call_count == 0
