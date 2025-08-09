"""Integration tests for thread invitation behavior.

These tests ensure that invited agents behave correctly in threads,
including the race condition fix where invited agents take ownership
of threads immediately upon invitation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix.users import AgentMatrixUser
from mindroom.models import AgentConfig, Config, ModelConfig, RouterConfig
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_invites import ThreadInviteManager
from mindroom.thread_utils import should_agent_respond


@pytest.fixture
def mock_config() -> Config:
    """Create a mock config with agents."""
    return Config(
        agents={
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=["#math:localhost"],  # Only configured for math room
            ),
            "general": AgentConfig(
                display_name="General",
                role="General assistance",
                rooms=["#general:localhost", "#automation:localhost"],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )


@pytest.mark.asyncio
async def test_invited_agent_responds_in_unconfigured_room() -> None:
    """Test that invited agents can respond in rooms they're not configured for."""
    # Create agent user
    agent_user = AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="Calculator",
        password="test_password",
    )

    # Create config where calculator is NOT configured for automation room
    config = Config(
        agents={
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=["#math:localhost"],  # Only configured for math room
            ),
        },
        router=RouterConfig(model="default"),
    )

    # Create bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=Path("/tmp/test"),
        config=config,
        rooms=["#math:localhost"],  # Bot only knows about math room
    )

    # Setup mock client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Setup thread invite manager
    bot.thread_invite_manager = ThreadInviteManager(mock_client)

    # Setup response tracker
    bot.response_tracker = ResponseTracker(agent_name="calculator", base_path=bot.storage_path)

    # Mock that calculator is invited to a thread in automation room
    async def mock_get_agent_threads(room_id: str, agent_name: str) -> list[str]:
        if room_id == "!automation:localhost" and agent_name == "calculator":
            return ["$thread1"]  # Calculator is invited to this thread
        return []

    bot.thread_invite_manager.get_agent_threads = mock_get_agent_threads

    # Create mock room and event in automation room (not configured)
    mock_room = MagicMock()
    mock_room.room_id = "!automation:localhost"

    mock_event = MagicMock()
    mock_event.sender = "@user:localhost"
    mock_event.body = "Calculate 2+2"
    mock_event.source = {
        "content": {
            "body": "Calculate 2+2",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread1",
            },
        }
    }

    # Mock interactive.handle_text_response to track if it's called
    with patch("mindroom.bot.interactive.handle_text_response") as mock_handle:
        await bot._on_message(mock_room, mock_event)

        # Verify that the bot processes the message despite not being configured for the room
        mock_handle.assert_called_once_with(mock_client, mock_room, mock_event, "calculator")


@pytest.mark.asyncio
async def test_invited_agent_takes_ownership_of_empty_thread(mock_config: Config) -> None:
    """Test that invited agents take ownership of threads with no agent history (race condition fix)."""
    # Test scenario: User invites calculator, then says "Hi" before calculator responds
    # Expected: Calculator should respond, not the router

    # Empty thread history (no agents have spoken yet)
    thread_history: list[dict] = []

    # Test invited agent behavior
    should_respond = should_agent_respond(
        agent_name="calculator",
        am_i_mentioned=False,  # Not mentioned in this message
        is_thread=True,
        room_id="!automation:localhost",
        configured_rooms=[],  # Calculator not configured for this room
        thread_history=thread_history,  # No one has spoken
        config=mock_config,
        is_invited_to_thread=True,  # Calculator is invited
    )

    # Invited agent should take ownership
    assert should_respond is True

    # Test non-invited agent in same scenario
    should_respond = should_agent_respond(
        agent_name="general",
        am_i_mentioned=False,
        is_thread=True,
        room_id="!automation:localhost",
        configured_rooms=["!automation:localhost"],  # General IS configured for room
        thread_history=thread_history,  # No one has spoken
        config=mock_config,
        is_invited_to_thread=False,  # General is NOT invited
    )

    # Non-invited agent should NOT respond (wait for router or invited agent)
    assert should_respond is False


@pytest.mark.asyncio
async def test_invited_agent_continues_conversation(mock_config: Config) -> None:
    """Test that invited agents continue conversations like native agents."""
    # Thread history where calculator has already responded
    thread_history = [
        {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
        {"sender": "@user:localhost", "body": "What about 3+3?"},
    ]

    # Test invited agent continuing conversation
    should_respond = should_agent_respond(
        agent_name="calculator",
        am_i_mentioned=False,
        is_thread=True,
        room_id="!automation:localhost",
        configured_rooms=[],  # Not configured
        thread_history=thread_history,
        config=mock_config,
        is_invited_to_thread=True,
    )

    # Should continue conversation
    assert should_respond is True


@pytest.mark.asyncio
async def test_multiple_agents_with_invited_agent(mock_config: Config) -> None:
    """Test that invited agents don't respond when multiple agents are in thread."""
    # Thread with multiple agents
    thread_history = [
        {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
        {"sender": "@mindroom_general:localhost", "body": "I can help too"},
        {"sender": "@user:localhost", "body": "Thanks both!"},
    ]

    # Test invited agent behavior with multiple agents
    should_respond = should_agent_respond(
        agent_name="calculator",
        am_i_mentioned=False,
        is_thread=True,
        room_id="!automation:localhost",
        configured_rooms=[],
        thread_history=thread_history,
        config=mock_config,
        is_invited_to_thread=True,
    )

    # Should not respond (multiple agents, no mention)
    assert should_respond is False

    # But should respond if mentioned
    should_respond = should_agent_respond(
        agent_name="calculator",
        am_i_mentioned=True,  # Mentioned
        is_thread=True,
        room_id="!automation:localhost",
        configured_rooms=[],
        thread_history=thread_history,
        config=mock_config,
        is_invited_to_thread=True,
    )

    # Should respond when mentioned
    assert should_respond is True


@pytest.mark.asyncio
async def test_bot_leaves_room_preserves_thread_invitations() -> None:
    """Test that leave_unconfigured_rooms preserves rooms with thread invitations."""
    # Create agent user
    agent_user = AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="Calculator",
        password="test_password",
    )

    # Create bot not configured for any rooms
    config = Config(router=RouterConfig(model="default"))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=Path("/tmp/test"),
        config=config,
        rooms=[],  # Not configured for any rooms
    )

    # Setup mock client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Mock joined_rooms - bot is in two rooms
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room2:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Setup thread invite manager
    bot.thread_invite_manager = ThreadInviteManager(mock_client)

    # Setup response tracker
    bot.response_tracker = ResponseTracker(agent_name="calculator", base_path=bot.storage_path)

    # Mock that calculator has thread invitation in room1 but not room2
    async def mock_get_agent_threads(room_id: str, agent_name: str) -> list[str]:
        if room_id == "!room1:localhost" and agent_name == "calculator":
            return ["$thread1"]
        return []

    bot.thread_invite_manager.get_agent_threads = mock_get_agent_threads

    # Track which rooms were left
    left_rooms = []

    async def mock_room_leave(room_id: str) -> Any:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    mock_client.room_leave = mock_room_leave

    # Run leave_unconfigured_rooms
    await bot.leave_unconfigured_rooms()

    # Verify bot left room2 (no invitation) but stayed in room1 (has invitation)
    assert "!room2:localhost" in left_rooms
    assert "!room1:localhost" not in left_rooms
