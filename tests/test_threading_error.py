"""Test threading behavior to reproduce and fix the threading error.

This test verifies that:
1. Agents always respond in threads (never in main room)
2. Commands that are replies don't cause threading errors
3. The bot handles various message relation scenarios correctly
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config, ModelConfig, RouterConfig
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


class TestThreadingBehavior:
    """Test that agents correctly handle threading in various scenarios."""

    @pytest_asyncio.fixture
    async def bot(self, tmp_path: Path) -> AsyncGenerator[AgentBot, None]:
        """Create an AgentBot for testing."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_general:localhost",
            password=TEST_PASSWORD,
            display_name="GeneralAgent",
            agent_name="general",
        )

        config = Config(
            agents={"general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"])},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,  # Disable streaming for simpler testing
            config=config,
        )

        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.user_id = "@mindroom_general:localhost"
        bot.client.homeserver = "http://localhost:8008"

        # Initialize components that depend on client
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*_args: object, **_kwargs: object) -> MagicMock:
            return mock_response

        mock_agent.arun = mock_arun

        # Mock create_agent to return our mock agent
        with patch("mindroom.bot.create_agent", return_value=mock_agent):
            yield bot

        # No cleanup needed since we're using mocks

    @pytest.mark.asyncio
    async def test_agent_creates_thread_when_mentioned_in_main_room(self, bot: AgentBot) -> None:
        """Test that agents create threads when mentioned in main room messages."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)  # type: ignore[union-attr]
        room.name = "Test Room"

        # Create a main room message that mentions the agent
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general Can you help me?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                },
                "event_id": "$main_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # The bot should send a response
        bot.client.room_send = AsyncMock(  # type: ignore[union-attr]
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history fetch (returns empty for new thread)
        bot.client.room_messages = AsyncMock(  # type: ignore[union-attr]
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )

        # Initialize the bot (to set up components it needs)
        bot.response_tracker.has_responded.return_value = False  # type: ignore[attr-defined]

        # Mock interactive.handle_text_response to return None (not an interactive response)
        # Mock _generate_response to capture the call and send a test response
        with (
            patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch.object(bot, "_generate_response") as mock_generate,
        ):
            # Process the message
            await bot._on_message(room, event)

            # Check that _generate_response was called
            mock_generate.assert_called_once()

            # Now simulate the response being sent
            await bot._send_response(room.room_id, event.event_id, "I can help you with that!", None)

        # Verify the bot sent a response
        bot.client.room_send.assert_called_once()  # type: ignore[union-attr]

        # Check the content of the response
        call_args = bot.client.room_send.call_args  # type: ignore[union-attr]
        content = call_args.kwargs["content"]

        # The response should create a thread from the original message
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$main_msg:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$main_msg:localhost"

    @pytest.mark.asyncio
    async def test_agent_responds_in_existing_thread(self, bot: AgentBot) -> None:
        """Test that agents respond correctly in existing threads."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)  # type: ignore[union-attr]
        room.name = "Test Room"

        # Create a message in a thread
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general What about this?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(  # type: ignore[union-attr]
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(  # type: ignore[union-attr]
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )

        # Initialize response tracking
        bot.response_tracker.has_responded.return_value = False  # type: ignore[attr-defined]

        # Mock interactive.handle_text_response and make AI fast
        with (
            patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch("mindroom.bot.ai_response", AsyncMock(return_value="OK")),
            patch("mindroom.bot.get_latest_thread_event_id_if_needed", AsyncMock(return_value="latest_thread_event")),
        ):
            # Process the message
            await bot._on_message(room, event)

        # Verify the bot sent messages (thinking + final)
        assert bot.client.room_send.call_count == 2  # type: ignore[union-attr]

        # Check the initial message (first call)
        first_call = bot.client.room_send.call_args_list[0]  # type: ignore[union-attr]
        initial_content = first_call.kwargs["content"]
        assert "m.relates_to" in initial_content
        assert initial_content["m.relates_to"]["rel_type"] == "m.thread"
        assert initial_content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert initial_content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$thread_msg:localhost"

    @pytest.mark.asyncio
    async def test_command_as_reply_doesnt_cause_thread_error(self, tmp_path: Path) -> None:
        """Test that commands sent as replies don't cause threading errors."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = Config(
            agents={"router": AgentConfig(display_name="Router", rooms=["!test:localhost"])},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
        )

        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.user_id = "@mindroom_router:localhost"
        bot.client.homeserver = "http://localhost:8008"

        # Initialize components that depend on client
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*_args: object, **_kwargs: object) -> MagicMock:
            return mock_response

        mock_agent.arun = mock_arun

        with patch("mindroom.bot.create_agent", return_value=mock_agent):
            room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
            room.name = "Test Room"

            # Create a command that's a reply to another message (not in a thread)
            event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "!help",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$some_other_msg:localhost"}},
                    },
                    "event_id": "$cmd_reply:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

            # Mock the bot's response - it should succeed
            bot.client.room_send = AsyncMock(
                return_value=nio.RoomSendResponse.from_dict(
                    {"event_id": "$response:localhost"},
                    room_id="!test:localhost",
                ),
            )

            # Process the command
            await bot._on_message(room, event)

            # The bot should send an error message about needing threads
            bot.client.room_send.assert_called_once()

            # Check the content
            call_args = bot.client.room_send.call_args
            content = call_args.kwargs["content"]

            # The error response should create a thread from the message the command is replying to
            # Since the command is a reply to $some_other_msg:localhost, that becomes the thread root
            assert "m.relates_to" in content
            assert content["m.relates_to"]["rel_type"] == "m.thread"
            # Thread root should be the message the command was replying to
            assert content["m.relates_to"]["event_id"] == "$some_other_msg:localhost"
            # Should reply to the command message
            assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_reply:localhost"

    @pytest.mark.asyncio
    async def test_command_in_thread_works_correctly(self, tmp_path: Path) -> None:
        """Test that commands in threads work without errors."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = Config(
            agents={"router": AgentConfig(display_name="Router", rooms=["!test:localhost"])},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
        )
        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.user_id = "@mindroom_router:localhost"
        bot.client.homeserver = "http://localhost:8008"

        # Initialize components that depend on client
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*_args: object, **_kwargs: object) -> MagicMock:
            return mock_response

        mock_agent.arun = mock_arun

        with patch("mindroom.bot.create_agent", return_value=mock_agent):
            room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
            room.name = "Test Room"

            # Create a command in a thread
            event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "!list_schedules",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$cmd_thread:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

            # Mock room_get_state for list_schedules command
            bot.client.room_get_state = AsyncMock(
                return_value=nio.RoomGetStateResponse.from_dict(
                    [],  # No scheduled tasks
                    room_id="!test:localhost",
                ),
            )

            # Mock the bot's response
            bot.client.room_send = AsyncMock(
                return_value=nio.RoomSendResponse.from_dict(
                    {"event_id": "$response:localhost"},
                    room_id="!test:localhost",
                ),
            )

            # Process the command
            await bot._on_message(room, event)

            # The bot should respond
            bot.client.room_send.assert_called_once()

            # Check the content
            call_args = bot.client.room_send.call_args
            content = call_args.kwargs["content"]

            # The response should be in the same thread
            assert "m.relates_to" in content
            assert content["m.relates_to"]["rel_type"] == "m.thread"
            assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
            assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_thread:localhost"

    @pytest.mark.asyncio
    async def test_message_with_multiple_relations_handled_correctly(self, bot: AgentBot) -> None:
        """Test that messages with complex relations are handled properly."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)  # type: ignore[union-attr]
        room.name = "Test Room"

        # Create a message that's both in a thread AND a reply (complex relations)
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general Complex question?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root:localhost",
                        "m.in_reply_to": {"event_id": "$previous_msg:localhost"},
                    },
                },
                "event_id": "$complex_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(  # type: ignore[union-attr]
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(  # type: ignore[union-attr]
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )

        # Initialize response tracking
        bot.response_tracker.has_responded.return_value = False  # type: ignore[attr-defined]

        # Mock interactive.handle_text_response and generate_response
        with (
            patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch.object(bot, "_generate_response") as mock_generate,
        ):
            # Process the message
            await bot._on_message(room, event)

            # Check that _generate_response was called
            mock_generate.assert_called_once()

            # Now simulate the response being sent
            await bot._send_response(
                room.room_id,
                event.event_id,
                "I can help with that complex question!",
                "$thread_root:localhost",
            )

        # Verify the bot sent a response
        bot.client.room_send.assert_called_once()  # type: ignore[union-attr]

        # Check the content
        call_args = bot.client.room_send.call_args  # type: ignore[union-attr]
        content = call_args.kwargs["content"]

        # The response should maintain the thread context
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$complex_msg:localhost"
