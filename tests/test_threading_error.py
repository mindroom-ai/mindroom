"""Test threading behavior to reproduce and fix the threading error.

This test verifies that:
1. Agents always respond in threads (never in main room)
2. Commands that are replies don't cause threading errors
3. The bot handles various message relation scenarios correctly
"""

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom.bot import AgentBot
from mindroom.matrix import AgentMatrixUser


class TestThreadingBehavior:
    """Test that agents correctly handle threading in various scenarios."""

    @pytest_asyncio.fixture
    async def bot(self, tmp_path):
        """Create an AgentBot for testing."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_general:localhost",
            password="test_password",
            display_name="GeneralAgent",
            agent_name="general",
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,  # Disable streaming for simpler testing
        )

        # Mock the orchestrator
        bot.orchestrator = MagicMock()

        # Create a mock client
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.user_id = "@mindroom_general:localhost"
        bot.client.homeserver = "http://localhost:8008"

        # Initialize components that depend on client
        bot.response_tracker = MagicMock()
        bot.thread_invite_manager = MagicMock()

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*args, **kwargs):
            return mock_response

        mock_agent.arun = mock_arun
        bot.agent = mock_agent

        yield bot

        # No cleanup needed since we're using mocks

    @pytest.mark.asyncio
    async def test_agent_creates_thread_when_mentioned_in_main_room(self, bot):
        """Test that agents create threads when mentioned in main room messages."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
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
            }
        )

        # The bot should send a response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost")
        )

        # Mock thread history fetch (returns empty for new thread)
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"}, room_id="!test:localhost"
            )
        )

        # Initialize the bot (to set up components it needs)
        bot.response_tracker.has_responded.return_value = False
        bot.thread_invite_manager.is_agent_invited_to_thread = AsyncMock(return_value=False)
        bot.thread_invite_manager.update_agent_activity = AsyncMock()

        # Mock interactive.handle_text_response to return None (not an interactive response)
        # Mock _generate_response to capture the call and send a test response
        with patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)), patch.object(
            bot, "_generate_response"
        ) as mock_generate:
            # Process the message
            await bot._on_message(room, event)

            # Check that _generate_response was called
            mock_generate.assert_called_once()

            # Now simulate the response being sent
            await bot._send_response(room, event.event_id, "I can help you with that!", None)

        # Verify the bot sent a response
        bot.client.room_send.assert_called_once()

        # Check the content of the response
        call_args = bot.client.room_send.call_args
        content = call_args.kwargs["content"]

        # The response should create a thread from the original message
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$main_msg:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$main_msg:localhost"

    @pytest.mark.asyncio
    async def test_agent_responds_in_existing_thread(self, bot):
        """Test that agents respond correctly in existing threads."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
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
            }
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost")
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"}, room_id="!test:localhost"
            )
        )

        # Initialize response tracking
        bot.response_tracker.has_responded.return_value = False
        bot.thread_invite_manager.is_agent_invited_to_thread = AsyncMock(return_value=False)
        bot.thread_invite_manager.update_agent_activity = AsyncMock()

        # Mock interactive.handle_text_response
        with patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)):
            # Process the message
            await bot._on_message(room, event)

        # Verify the bot sent a response
        bot.client.room_send.assert_called_once()

        # Check the content
        call_args = bot.client.room_send.call_args
        content = call_args.kwargs["content"]

        # The response should be in the same thread
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$thread_msg:localhost"

    @pytest.mark.asyncio
    async def test_command_as_reply_doesnt_cause_thread_error(self, tmp_path):
        """Test that commands sent as replies don't cause threading errors."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password="test_password",
            display_name="Router",
            agent_name="router",
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
        )

        # Mock the orchestrator
        bot.orchestrator = MagicMock()

        # Create a mock client
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.user_id = "@mindroom_router:localhost"
        bot.client.homeserver = "http://localhost:8008"

        # Initialize components that depend on client
        bot.response_tracker = MagicMock()
        bot.thread_invite_manager = MagicMock()

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*args, **kwargs):
            return mock_response

        mock_agent.arun = mock_arun
        bot.agent = mock_agent

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
            }
        )

        # Mock the bot's response - it should succeed
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost")
        )

        # Process the command
        await bot._on_message(room, event)

        # The bot should send an error message about needing threads
        bot.client.room_send.assert_called_once()

        # Check the content
        call_args = bot.client.room_send.call_args
        content = call_args.kwargs["content"]

        # The error response should be sent without a thread (thread_id=None passed to _send_response)
        # After the fix, router command errors don't create threads
        assert "m.relates_to" in content
        # Should only have a reply relation, not a thread
        assert "m.in_reply_to" in content["m.relates_to"]
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_reply:localhost"
        # Should NOT have a thread relation when it's a router command error
        if "rel_type" in content["m.relates_to"]:
            assert content["m.relates_to"]["rel_type"] != "m.thread"

    @pytest.mark.asyncio
    async def test_command_in_thread_works_correctly(self, tmp_path):
        """Test that commands in threads work without errors."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password="test_password",
            display_name="Router",
            agent_name="router",
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
        )

        # Mock the orchestrator
        bot.orchestrator = MagicMock()

        # Create a mock client
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.user_id = "@mindroom_router:localhost"
        bot.client.homeserver = "http://localhost:8008"

        # Initialize components that depend on client
        bot.response_tracker = MagicMock()
        bot.thread_invite_manager = MagicMock()

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*args, **kwargs):
            return mock_response

        mock_agent.arun = mock_arun
        bot.agent = mock_agent

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
            }
        )

        # Mock room_get_state for list_schedules command
        bot.client.room_get_state = AsyncMock(
            return_value=nio.RoomGetStateResponse.from_dict(
                [],  # No scheduled tasks
                room_id="!test:localhost",
            )
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost")
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
    async def test_message_with_multiple_relations_handled_correctly(self, bot):
        """Test that messages with complex relations are handled properly."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
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
            }
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost")
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"}, room_id="!test:localhost"
            )
        )

        # Initialize response tracking
        bot.response_tracker.has_responded.return_value = False
        bot.thread_invite_manager.is_agent_invited_to_thread = AsyncMock(return_value=False)
        bot.thread_invite_manager.update_agent_activity = AsyncMock()

        # Mock interactive.handle_text_response and generate_response
        with patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)), patch.object(
            bot, "_generate_response"
        ) as mock_generate:
            # Process the message
            await bot._on_message(room, event)

            # Check that _generate_response was called
            mock_generate.assert_called_once()

            # Now simulate the response being sent
            await bot._send_response(
                room, event.event_id, "I can help with that complex question!", "$thread_root:localhost"
            )

        # Verify the bot sent a response
        bot.client.room_send.assert_called_once()

        # Check the content
        call_args = bot.client.room_send.call_args
        content = call_args.kwargs["content"]

        # The response should maintain the thread context
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$complex_msg:localhost"
