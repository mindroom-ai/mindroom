"""End-to-end threading integration tests (see test_thread_* and test_bot_sync_event_cache for split concerns)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.delivery_gateway import SendTextRequest
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    drain_coalescing,
    install_generate_response_mock,
    runtime_paths_for,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _install_runtime_write_coordinator,
    _make_client_mock,
    _matrix_room,
    _runtime_bound_config,
    _runtime_event_cache,
    thread_history_result,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

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
        bot.event_cache = _runtime_event_cache()
        bot.event_cache.get_thread_events.return_value = None
        bot.event_cache.append_event.return_value = True
        _install_runtime_write_coordinator(bot)

        # Initialize the bot (to set up components it needs)

        # Mock interactive.handle_text_response to return None (not an interactive response)
        # Mock response generation to capture the call and send a test response
        generate_response = AsyncMock()
        install_generate_response_mock(bot, generate_response)
        with patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)):
            # Process the message
            await bot._on_message(room, event)
            await drain_coalescing(bot)

            # Check that response generation was called
            generate_response.assert_called_once()

            # Now simulate the response being sent
            target = bot._conversation_resolver.build_message_target(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
                event_source=event.source,
            )
            await bot._delivery_gateway.send_text(
                SendTextRequest(
                    target=target,
                    response_text="I can help you with that!",
                ),
            )

        # Check the final response content.
        assert bot.client.room_send.call_count == 1
        content = bot.client.room_send.call_args_list[0].kwargs["content"]

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
        bot.event_cache = _runtime_event_cache()
        bot.event_cache.get_thread_events.return_value = None
        bot.event_cache.append_event.return_value = True
        _install_runtime_write_coordinator(bot)

        # Initialize response tracking

        # Mock interactive.handle_text_response and make AI fast
        resolver = unwrap_extracted_collaborator(bot._conversation_resolver)
        with (
            patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch("mindroom.response_runner.ai_response", AsyncMock(return_value="OK")),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                AsyncMock(return_value="latest_thread_event"),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
            patch.object(
                resolver,
                "fetch_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
            patch.object(
                bot._conversation_cache,
                "get_strict_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
        ):
            # Process the message
            await bot._on_message(room, event)
            await drain_coalescing(bot)

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
    async def test_command_as_reply_doesnt_cause_thread_error(self, tmp_path: Path) -> None:
        """Plain-reply commands should answer the command event without following stale reply targets."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = _runtime_bound_config(
            Config(
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)

        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        # Initialize components that depend on client

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

            with (
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                    AsyncMock(return_value=thread_history_result([], is_full_history=False)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                    AsyncMock(return_value=thread_history_result([], is_full_history=True)),
                ),
            ):
                # Process the command
                await bot._on_message(room, event)
                await drain_coalescing(bot)

            # The bot should send an error message about needing threads
            bot.client.room_send.assert_called_once()

            # Check the content
            call_args = bot.client.room_send.call_args
            content = call_args.kwargs["content"]

            assert "m.relates_to" in content
            assert content["m.relates_to"]["rel_type"] == "m.thread"
            assert content["m.relates_to"]["event_id"] == "$cmd_reply:localhost"
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

        config = _runtime_bound_config(
            Config(
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)
        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        # Initialize components that depend on client

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

            with (
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                    AsyncMock(return_value=thread_history_result([], is_full_history=False)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                    AsyncMock(return_value=thread_history_result([], is_full_history=True)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                    AsyncMock(return_value=thread_history_result([], is_full_history=True)),
                ),
            ):
                # Process the command
                await bot._on_message(room, event)
                await drain_coalescing(bot)

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
    async def test_command_reply_to_thread_message_stays_in_thread_transitively(
        self,
        tmp_path: Path,
    ) -> None:
        """Plain command replies to threaded messages should stay in the inherited thread."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = _runtime_bound_config(
            Config(
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

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

        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict(
                {"event_id": "$response:localhost"},
                room_id="!test:localhost",
            ),
        )

        with (
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ),
        ):
            await bot._on_message(room, event)
            await drain_coalescing(bot)

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

        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        room = _matrix_room(name="Test Room")

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
            patch("mindroom.turn_controller.suggest_responder_for_message", AsyncMock(return_value="general")),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                AsyncMock(return_value="$latest:localhost"),
            ),
            patch(
                "mindroom.delivery_gateway.send_message_result",
                AsyncMock(
                    return_value=DeliveredMatrixEvent(
                        event_id="$router_response:localhost",
                        content_sent={"body": "router relay"},
                    ),
                ),
            ) as mock_send,
        ):
            await bot._turn_controller._execute_router_relay(
                room,
                event,
                thread_history=[],
                thread_id="$thread_root:localhost",
                requester_user_id="@user:localhost",
            )

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
        bot.event_cache = _runtime_event_cache()
        bot.event_cache.get_thread_events.return_value = None
        bot.event_cache.append_event.return_value = True
        _install_runtime_write_coordinator(bot)

        # Initialize response tracking

        # Mock interactive.handle_text_response and generate_response
        generate_response = AsyncMock()
        install_generate_response_mock(bot, generate_response)
        with (
            patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                AsyncMock(return_value=thread_history_result([], is_full_history=True)),
            ),
        ):
            # Process the message
            await bot._on_message(room, event)
            await drain_coalescing(bot)

            # Check that response generation was called
            generate_response.assert_called_once()

            # Now simulate the response being sent
            target = bot._conversation_resolver.build_message_target(
                room_id=room.room_id,
                thread_id="$thread_root:localhost",
                reply_to_event_id=event.event_id,
                event_source=event.source,
            )
            await bot._delivery_gateway.send_text(
                SendTextRequest(
                    target=target,
                    response_text="I can help with that complex question!",
                ),
            )

        # Check the final response content.
        assert bot.client.room_send.call_count == 1
        content = bot.client.room_send.call_args_list[0].kwargs["content"]

        # The response should maintain the thread context
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$complex_msg:localhost"
