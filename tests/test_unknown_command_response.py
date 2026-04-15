"""Tests for unknown command response handling."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_runtime_cache_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
)


@pytest.mark.asyncio
async def test_unknown_command_in_main_room(tmp_path: Path) -> None:
    """Test that unknown commands get a helpful error response in main room."""
    # Create config
    config = bind_runtime_paths(
        Config(
            agents={
                "router": AgentConfig(
                    display_name="Router",
                    role="Route messages",
                    rooms=["!test:localhost"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )

    # Create router agent user
    agent_user = AgentMatrixUser(
        agent_name="router",
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )

    # Create router bot
    bot = AgentBot(
        agent_user=agent_user,
        config=config,
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=False,
        rooms=["!test:localhost"],  # Make sure bot knows it's in this room
    )

    # Mock client and initialize required components
    bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    install_runtime_cache_support(bot)

    # Create mock room and event
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:localhost"
    room.canonical_alias = None
    room.name = "Test Room"
    room.users = {
        "@mindroom_router:localhost": None,
        "@mindroom_general:localhost": None,
        "@user:localhost": None,
    }

    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.server_timestamp = 1000
    event.sender = "@user:localhost"
    event.body = "!unknown_command"
    event.server_timestamp = 1234567890
    event.source = {"content": {"body": "!unknown_command"}}

    # Mock send_message to capture what would be sent
    sent_messages = []

    async def mock_send_message(
        _client: AsyncMock,
        room_id: str,
        content: dict,
    ) -> object:
        # Extract thread_id from content if present
        thread_id = None
        if "m.relates_to" in content:
            relates_to = content["m.relates_to"]
            if "rel_type" in relates_to and relates_to["rel_type"] == "m.thread":
                thread_id = relates_to.get("event_id")

        sent_messages.append(
            {
                "room_id": room_id,
                "content": content,
                "thread_id": thread_id,
            },
        )
        return delivered_matrix_event("$response_event", content)

    # Add orchestrator mock
    bot.orchestrator = MagicMock()
    bot.orchestrator.thread_specific_agents = {}

    with patch("mindroom.delivery_gateway.send_message_result", mock_send_message):
        await bot._on_message(room, event)

    # Verify error message was sent
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg["room_id"] == "!test:localhost"
    assert "Unknown command" in msg["content"]["body"]
    assert "!help" in msg["content"]["body"]
    # In main room, the response creates a thread from the original message
    assert msg["thread_id"] == "$test_event"


@pytest.mark.asyncio
async def test_unknown_command_in_thread(tmp_path: Path) -> None:
    """Test that unknown commands get a helpful error response when in a thread."""
    # Create config
    config = bind_runtime_paths(
        Config(
            agents={
                "router": AgentConfig(
                    display_name="Router",
                    role="Route messages",
                    rooms=["!test:localhost"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )

    # Create router agent user
    agent_user = AgentMatrixUser(
        agent_name="router",
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )

    # Create router bot
    bot = AgentBot(
        agent_user=agent_user,
        config=config,
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=False,
        rooms=["!test:localhost"],  # Make sure bot knows it's in this room
    )

    # Mock client and initialize required components
    bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    install_runtime_cache_support(bot)

    # Create mock room and event
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:localhost"
    room.canonical_alias = None
    room.name = "Test Room"
    room.users = {
        "@mindroom_router:localhost": None,
        "@mindroom_general:localhost": None,
        "@user:localhost": None,
    }

    # Create an event that's already in a thread
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.server_timestamp = 1000
    event.sender = "@user:localhost"
    event.body = "!schedule"  # Incomplete schedule command
    event.server_timestamp = 1234567890
    event.source = {
        "content": {
            "body": "!schedule",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    # Mock send_message to capture what would be sent
    sent_messages = []
    error_messages = []

    async def mock_send_message(
        _client: AsyncMock,
        room_id: str,
        content: dict,
    ) -> object:
        # Extract thread_id from content if present
        thread_id = None
        if "m.relates_to" in content:
            relates_to = content["m.relates_to"]
            if "rel_type" in relates_to and relates_to["rel_type"] == "m.thread":
                thread_id = relates_to.get("event_id")

        # Check if this is trying to create a thread from a thread message incorrectly
        if thread_id == "$test_event":  # Using the event itself as thread root
            # This would trigger the Matrix error
            error_messages.append("Cannot start threads from an event with a relation")
            msg = "M_UNKNOWN Cannot start threads from an event with a relation"
            raise nio.SendRetryError(msg)

        sent_messages.append(
            {
                "room_id": room_id,
                "content": content,
                "thread_id": thread_id,
            },
        )
        return delivered_matrix_event("$response_event", content)

    # Add orchestrator mock
    bot.orchestrator = MagicMock()
    bot.orchestrator.thread_specific_agents = {}

    with (
        patch("mindroom.delivery_gateway.send_message_result", mock_send_message),
        patch(
            "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_snapshot",
            AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
        patch(
            "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
            AsyncMock(return_value=[]),
        ),
        patch(
            "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
            AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
        patch(
            "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
            AsyncMock(return_value=[]),
        ),
    ):
        await bot._on_message(room, event)

    assert not error_messages
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg["room_id"] == "!test:localhost"
    assert "Unknown command" in msg["content"]["body"]
    assert msg["thread_id"] == "$thread_root"


@pytest.mark.asyncio
async def test_unknown_command_with_reply_stays_plain_reply(tmp_path: Path) -> None:
    """Plain-reply unknown commands should not invent a thread root."""
    # Create config
    config = bind_runtime_paths(
        Config(
            agents={
                "router": AgentConfig(
                    display_name="Router",
                    role="Route messages",
                    rooms=["!test:localhost"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )

    # Create router agent user
    agent_user = AgentMatrixUser(
        agent_name="router",
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )

    # Create router bot
    bot = AgentBot(
        agent_user=agent_user,
        config=config,
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=False,
        rooms=["!test:localhost"],  # Make sure bot knows it's in this room
    )

    # Mock client and initialize required components
    bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    install_runtime_cache_support(bot)

    # Create mock room and event
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:localhost"
    room.canonical_alias = None
    room.name = "Test Room"
    room.users = {
        "@mindroom_router:localhost": None,
        "@mindroom_general:localhost": None,
        "@user:localhost": None,
    }

    # Create an event that's a reply to another message
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.server_timestamp = 1000
    event.sender = "@user:localhost"
    event.body = "!invalid"
    event.server_timestamp = 1234567890
    event.source = {
        "content": {"body": "!invalid", "m.relates_to": {"m.in_reply_to": {"event_id": "$original_message"}}},
    }

    # Mock send_message
    sent_messages = []

    async def mock_send_message(
        _client: AsyncMock,
        room_id: str,
        content: dict,
    ) -> object:
        # Extract thread_id from content if present
        thread_id = None
        if "m.relates_to" in content:
            relates_to = content["m.relates_to"]
            if "rel_type" in relates_to and relates_to["rel_type"] == "m.thread":
                thread_id = relates_to.get("event_id")

        sent_messages.append(
            {
                "room_id": room_id,
                "content": content,
                "thread_id": thread_id,
            },
        )
        return delivered_matrix_event("$response_event", content)

    # Add orchestrator mock
    bot.orchestrator = MagicMock()
    bot.orchestrator.thread_specific_agents = {}

    with patch("mindroom.delivery_gateway.send_message_result", mock_send_message):
        await bot._on_message(room, event)

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg["room_id"] == "!test:localhost"
    assert "Unknown command" in msg["content"]["body"]
    assert msg["thread_id"] is None
    assert msg["content"]["m.relates_to"] == {"m.in_reply_to": {"event_id": "$test_event"}}
