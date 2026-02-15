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
        bot.client.rooms = {}
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
            },
        )

        # The bot should send a response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history fetch (returns empty for new thread)
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )

        # Initialize the bot (to set up components it needs)
        bot.response_tracker.has_responded.return_value = False

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
    async def test_agent_responds_in_existing_thread(self, bot: AgentBot) -> None:
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
            },
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )

        # Initialize response tracking
        bot.response_tracker.has_responded.return_value = False

        # Mock interactive.handle_text_response and make AI fast
        with (
            patch("mindroom.bot.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch("mindroom.bot.ai_response", AsyncMock(return_value="OK")),
            patch("mindroom.bot.get_latest_thread_event_id_if_needed", AsyncMock(return_value="latest_thread_event")),
        ):
            # Process the message
            await bot._on_message(room, event)

        # Verify the bot sent messages (thinking + final)
        assert bot.client.room_send.call_count == 2

        # Check the initial message (first call)
        first_call = bot.client.room_send.call_args_list[0]
        initial_content = first_call.kwargs["content"]
        assert "m.relates_to" in initial_content
        assert initial_content["m.relates_to"]["rel_type"] == "m.thread"
        assert initial_content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert initial_content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$thread_msg:localhost"

    @pytest.mark.asyncio
    async def test_extract_context_maps_plain_reply_to_existing_thread(self, bot: AgentBot) -> None:
        """Plain replies to thread messages should resolve to the original thread root."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow-up from a non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$reply_plain:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Agent answer in thread",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$thread_root:localhost",
                        },
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        expected_history = [
            {"event_id": "$thread_root:localhost", "body": "Root"},
            {"event_id": "$thread_msg:localhost", "body": "Agent answer in thread"},
        ]
        with patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=expected_history)) as mock_fetch:
            context = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_fetch.assert_awaited_once_with(bot.client, room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_maps_plain_reply_to_thread_root_with_existing_replies(self, bot: AgentBot) -> None:
        """Plain replies to a thread root should load full thread history, not just the root event."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow-up from a non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": "$reply_plain_root:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567892,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Original root message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$thread_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567889,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        expected_history = [
            {"event_id": "$thread_root:localhost", "body": "Original root message"},
            {"event_id": "$thread_msg:localhost", "body": "Agent answer in thread"},
        ]
        with patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=expected_history)) as mock_fetch:
            context = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_fetch.assert_awaited_once_with(bot.client, room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_builds_reply_chain_history_without_threads(self, bot: AgentBot) -> None:
        """Reply-only chains should still keep linear conversation context."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Third message in a reply-only chain",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg2:localhost"}},
                },
                "event_id": "$msg3:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567893,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second message",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg1:localhost"}},
                        },
                        "event_id": "$msg2:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First message",
                            "msgtype": "m.text",
                        },
                        "event_id": "$msg1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567891,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with patch("mindroom.bot.fetch_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg1:localhost"
        assert [msg["event_id"] for msg in context.thread_history] == ["$msg1:localhost", "$msg2:localhost"]
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_long_reply_chain_keeps_true_root(self, bot: AgentBot) -> None:
        """Long reply chains should keep a stable root instead of drifting."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        chain_length = 55  # Intentionally exceeds the old fixed depth cap of 50.
        newest_parent_id = f"$msg{chain_length}:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": f"{chain_length + 1}th message in reply-only chain",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": newest_parent_id}},
                },
                "event_id": f"$msg{chain_length + 1}:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890 + chain_length + 1,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        responses: list[nio.RoomGetEventResponse] = []
        for i in range(chain_length, 0, -1):
            content = {"body": f"Message {i}", "msgtype": "m.text"}
            if i > 1:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": f"$msg{i - 1}:localhost"}}

            responses.append(
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": content,
                        "event_id": f"$msg{i}:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567880 + i,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            )

        bot.client.room_get_event = AsyncMock(side_effect=responses)

        with patch("mindroom.bot.fetch_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._extract_message_context(room, event)
            # Re-resolving should use cached reply-chain nodes and roots.
            context_cached = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context_cached.is_thread is True
        assert context.thread_id == "$msg1:localhost"
        assert context_cached.thread_id == "$msg1:localhost"
        assert len(context.thread_history) == chain_length
        assert context.thread_history[0]["event_id"] == "$msg1:localhost"
        assert context.thread_history[-1]["event_id"] == newest_parent_id
        assert bot.client.room_get_event.await_count == chain_length
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_reply_chain_cycle_stops_cleanly(self, bot: AgentBot) -> None:
        """Cycle traversal should terminate without looping forever."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Cycle edge case",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg3:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Message 3",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg2:localhost"}},
                        },
                        "event_id": "$msg3:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Message 2",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg3:localhost"}},
                        },
                        "event_id": "$msg2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with patch("mindroom.bot.fetch_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg2:localhost"
        assert [msg["event_id"] for msg in context.thread_history] == ["$msg2:localhost", "$msg3:localhost"]
        assert bot.client.room_get_event.await_count == 2
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_preserves_plain_replies_before_thread_link(self, bot: AgentBot) -> None:
        """Reply-chain messages should be preserved when chain eventually points to a thread."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply from non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain2:localhost"}},
                },
                "event_id": "$plain3:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567895,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                        },
                        "event_id": "$plain2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                        },
                        "event_id": "$plain1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567893,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Earlier threaded message",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                        },
                        "event_id": "$thread_msg:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        thread_history = [
            {"event_id": "$thread_root:localhost", "body": "Thread root"},
            {"event_id": "$thread_msg:localhost", "body": "Earlier threaded message"},
        ]
        with patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=thread_history)) as mock_fetch:
            context = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert [msg["event_id"] for msg in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain1:localhost",
            "$plain2:localhost",
        ]
        mock_fetch.assert_awaited_once_with(bot.client, room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_preserves_plain_replies_across_thread_reentries(self, bot: AgentBot) -> None:
        """Plain replies should remain in context even when chain re-enters threaded events."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply from non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$p2:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$t2:localhost"}},
                        },
                        "event_id": "$p2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Thread reply after plain interleave",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$root:localhost",
                                "m.in_reply_to": {"event_id": "$p1:localhost"},
                            },
                        },
                        "event_id": "$t2:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First plain interleaved reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$t1:localhost"}},
                        },
                        "event_id": "$p1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567893,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First threaded reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$root:localhost",
                                "m.in_reply_to": {"event_id": "$root:localhost"},
                            },
                        },
                        "event_id": "$t1:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {"body": "Thread root", "msgtype": "m.text"},
                        "event_id": "$root:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567891,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        thread_history = [
            {"event_id": "$root:localhost", "body": "Thread root"},
            {"event_id": "$t1:localhost", "body": "First threaded reply"},
            {"event_id": "$t2:localhost", "body": "Thread reply after plain interleave"},
        ]
        with patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=thread_history)) as mock_fetch:
            context = await bot._extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$root:localhost"
        assert [msg["event_id"] for msg in context.thread_history] == [
            "$root:localhost",
            "$t1:localhost",
            "$p1:localhost",
            "$t2:localhost",
            "$p2:localhost",
        ]
        mock_fetch.assert_awaited_once_with(bot.client, room.room_id, "$root:localhost")

    def test_merge_thread_and_chain_history_preserves_chronological_order(self) -> None:
        """Merged context should preserve chronological order for interleaved plain replies."""
        thread_history = [
            {"event_id": "$root:localhost", "body": "Thread root"},
            {"event_id": "$t1:localhost", "body": "First threaded reply"},
            {"event_id": "$t2:localhost", "body": "Thread reply after plain interleave"},
        ]
        chain_history = [
            {"event_id": "$root:localhost", "body": "Thread root"},
            {"event_id": "$t1:localhost", "body": "First threaded reply"},
            {"event_id": "$p1:localhost", "body": "First plain interleaved reply"},
            {"event_id": "$t2:localhost", "body": "Thread reply after plain interleave"},
            {"event_id": "$p2:localhost", "body": "Second plain reply"},
        ]

        merged = AgentBot._merge_thread_and_chain_history(thread_history, chain_history)

        assert [msg["event_id"] for msg in merged] == [
            "$root:localhost",
            "$t1:localhost",
            "$p1:localhost",
            "$t2:localhost",
            "$p2:localhost",
        ]

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
        bot.client.rooms = {}
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
        bot.client.rooms = {}
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
    async def test_command_reply_to_thread_message_uses_existing_thread_root(self, tmp_path: Path) -> None:
        """Plain replies to a threaded message should keep command replies in that thread."""
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
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.rooms = {}
        bot.client.user_id = "@mindroom_router:localhost"
        bot.client.homeserver = "http://localhost:8008"

        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "!help",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$cmd_reply_plain:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Agent thread message",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict(
                {"event_id": "$response:localhost"},
                room_id="!test:localhost",
            ),
        )

        with patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=[])):
            await bot._on_message(room, event)

        bot.client.room_send.assert_called_once()
        content = bot.client.room_send.call_args.kwargs["content"]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_reply_plain:localhost"

    @pytest.mark.asyncio
    async def test_router_routing_reply_to_thread_message_uses_existing_thread_root(self, tmp_path: Path) -> None:
        """Router routing should resolve plain replies back to the real thread root."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = Config(
            agents={
                "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
            },
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
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.rooms = {}
        bot.client.user_id = "@mindroom_router:localhost"
        bot.client.homeserver = "http://localhost:8008"

        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Can someone help with this?",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Earlier message in thread",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567889,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        with (
            patch("mindroom.bot.suggest_agent_for_message", AsyncMock(return_value="general")),
            patch("mindroom.bot.get_latest_thread_event_id_if_needed", AsyncMock(return_value="$latest:localhost")),
            patch("mindroom.bot.send_message", AsyncMock(return_value="$router_response:localhost")) as mock_send,
        ):
            await bot._handle_ai_routing(room, event, thread_history=[], thread_id="$thread_root:localhost")

        mock_send.assert_awaited_once()
        bot.client.room_get_event.assert_not_called()
        content = mock_send.call_args.args[2]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$plain_reply:localhost"

    @pytest.mark.asyncio
    async def test_message_with_multiple_relations_handled_correctly(self, bot: AgentBot) -> None:
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
            },
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )

        # Initialize response tracking
        bot.response_tracker.has_responded.return_value = False

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
        bot.client.room_send.assert_called_once()

        # Check the content
        call_args = bot.client.room_send.call_args
        content = call_args.kwargs["content"]

        # The response should maintain the thread context
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$complex_msg:localhost"
