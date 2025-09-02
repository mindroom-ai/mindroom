"""Test that the 🛑 emoji can be reused for other purposes when not stopping generation."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix.users import AgentMatrixUser
from mindroom.stop import StopManager


@pytest.mark.asyncio
async def test_stop_emoji_only_stops_during_generation(tmp_path: Path) -> None:
    """Test that 🛑 reaction only acts as stop button during message generation."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        rooms=["!test:example.com"],
    )

    # Set up the bot with necessary mocks
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.stop_manager = StopManager()

    # Create a room and reaction event
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a 🛑 reaction event
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    # Mock interactive.handle_reaction to simulate it being an interactive question
    with patch("mindroom.bot.interactive.handle_reaction") as mock_handle_reaction:
        mock_handle_reaction.return_value = ("stop_option", None)  # Simulate selecting a stop option

        # Case 1: Message is NOT being generated - should handle as interactive
        await bot._on_reaction(room, reaction_event)

        # Should have called interactive.handle_reaction since message wasn't being tracked
        mock_handle_reaction.assert_called_once()

        # Reset the mock
        mock_handle_reaction.reset_mock()

        # Case 2: Message IS being generated - should handle as stop button
        # Track a message as being generated
        task = MagicMock()  # Use MagicMock instead of AsyncMock for the task
        task.done = MagicMock(return_value=False)  # done() is a regular method, not async
        bot.stop_manager.set_current(
            message_id="$message:example.com",
            room_id="!test:example.com",
            task=task,
        )

        # Process the same reaction again
        await bot._on_reaction(room, reaction_event)

        # Should NOT have called interactive.handle_reaction since it was handled as stop
        mock_handle_reaction.assert_not_called()

        # The task should have been cancelled
        task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stop_emoji_from_agent_falls_through(tmp_path: Path) -> None:
    """Test that 🛑 reactions from agents fall through to other handlers."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        rooms=["!test:example.com"],
    )

    # Set up the bot
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.response_tracker = MagicMock()
    bot.logger = MagicMock()
    bot.stop_manager = StopManager()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a 🛑 reaction from ANOTHER AGENT
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@mindroom_helper:example.com",  # Another agent
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    # Mock extract_agent_name to return that this is an agent
    with (
        patch("mindroom.bot.extract_agent_name", return_value="helper"),
        patch("mindroom.bot.interactive.handle_reaction") as mock_handle_reaction,
        patch("mindroom.bot.config_confirmation.get_pending_change", return_value=None),
    ):
        mock_handle_reaction.return_value = None  # No interactive result

        # Track a message as being generated
        task = MagicMock()  # Use MagicMock instead of AsyncMock for the task
        task.done = MagicMock(return_value=False)  # done() is a regular method, not async
        bot.stop_manager.set_current(
            message_id="$message:example.com",
            room_id="!test:example.com",
            task=task,
        )

        # Process the reaction from an agent
        await bot._on_reaction(room, reaction_event)

        # Should have called interactive.handle_reaction (fell through)
        mock_handle_reaction.assert_called_once()

        # Task should NOT have been cancelled (agents can't stop generation)
        task.cancel.assert_not_called()
