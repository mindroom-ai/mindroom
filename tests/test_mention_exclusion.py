"""Test that agents don't respond when other agents are mentioned by users."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD


@pytest.mark.asyncio
async def test_agent_ignores_user_message_mentioning_other_agents() -> None:
    """Test that an agent doesn't respond when a user mentions other agents."""
    # Create test config
    config = Config(
        agents={
            "general": AgentConfig(display_name="General", rooms=["!room:localhost"]),
            "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
        },
        teams={},
        room_models={},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )

    # Create GeneralAgent bot
    general_bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General",
            password=TEST_PASSWORD,
        ),
        storage_path=Mock(),
        config=config,
        rooms=["!room:localhost"],
    )

    # Mock the client
    general_bot.client = AsyncMock(spec=nio.AsyncClient)
    general_bot.client.user_id = "@mindroom_general:localhost"

    # Mock response tracker
    general_bot.response_tracker = Mock()
    general_bot.response_tracker.has_responded = Mock(return_value=False)

    # Create a test room
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_general:localhost")

    # Create a message where user mentions ResearchAgent
    # The message content has mentions for ResearchAgent
    event = Mock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.sender = "@user:localhost"  # User, not an agent
    event.body = "@research find the latest news"
    event.source = {
        "content": {
            "body": "@research find the latest news",
            "m.mentions": {
                "user_ids": ["@mindroom_research:localhost"],  # ResearchAgent is mentioned
            },
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    # Mock the thread history fetch
    with patch("mindroom.bot.fetch_thread_history") as mock_fetch_history:
        mock_fetch_history.return_value = []

        # Mock the generate_response method to track if it's called
        with patch.object(general_bot, "_generate_response") as mock_generate:
            # Process the message
            await general_bot._on_message(room, event)

            # GeneralAgent should NOT generate a response because ResearchAgent is mentioned
            mock_generate.assert_not_called()


@pytest.mark.asyncio
async def test_agent_responds_when_mentioned_along_with_others() -> None:
    """Test that an agent DOES respond when mentioned, even if other agents are also mentioned."""
    # Create test config
    config = Config(
        agents={
            "general": AgentConfig(display_name="General", rooms=["!room:localhost"]),
            "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
        },
        teams={},
        room_models={},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )

    # Create GeneralAgent bot
    general_bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General",
            password=TEST_PASSWORD,
        ),
        storage_path=Mock(),
        config=config,
        rooms=["!room:localhost"],
    )

    # Mock the client
    general_bot.client = AsyncMock(spec=nio.AsyncClient)
    general_bot.client.user_id = "@mindroom_general:localhost"

    # Mock response tracker
    general_bot.response_tracker = Mock()
    general_bot.response_tracker.has_responded = Mock(return_value=False)

    # Create a test room
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_general:localhost")

    # Create a message where user mentions BOTH agents
    event = Mock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.sender = "@user:localhost"  # User, not an agent
    event.body = "@general @research help me with this"
    event.source = {
        "content": {
            "body": "@general @research help me with this",
            "m.mentions": {
                "user_ids": [
                    "@mindroom_general:localhost",  # GeneralAgent is mentioned
                    "@mindroom_research:localhost",  # ResearchAgent is also mentioned
                ],
            },
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    # Mock the thread history fetch
    with patch("mindroom.bot.fetch_thread_history") as mock_fetch_history:
        mock_fetch_history.return_value = []

        # Mock decide_team_formation to return False (no team formation)
        with patch("mindroom.bot.decide_team_formation") as mock_decide_team_formation:
            mock_decide_team_formation.return_value = Mock(decide_team_formation=False, agents=[], mode=None)

            # Mock the generate_response method to track if it's called
            with patch.object(general_bot, "_generate_response") as mock_generate:
                # Process the message
                await general_bot._on_message(room, event)

                # GeneralAgent SHOULD generate a response because it's mentioned
                mock_generate.assert_called_once()
