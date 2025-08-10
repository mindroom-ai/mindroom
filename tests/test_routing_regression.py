"""Regression tests for routing behavior.

These tests ensure that fixed bugs don't resurface, particularly:
1. Router should NOT respond when any agent is mentioned
2. Only mentioned agents should respond
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import Config
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_invites import ThreadInviteManager

from .conftest import TEST_PASSWORD


def setup_test_bot(
    agent: AgentMatrixUser,
    storage_path: Path,
    room_id: str,
    enable_streaming: bool = False,
) -> AgentBot:
    """Set up a test bot with all required mocks."""
    config = Config.from_yaml()

    bot = AgentBot(agent, storage_path, rooms=[room_id], enable_streaming=enable_streaming, config=config)
    bot.client = AsyncMock()
    bot.response_tracker = ResponseTracker(bot.agent_name, base_path=storage_path)
    bot.thread_invite_manager = ThreadInviteManager(bot.client)
    return bot


@pytest.fixture
def mock_research_agent() -> AgentMatrixUser:
    """Create a mock research agent user."""
    return AgentMatrixUser(
        agent_name="research",
        password=TEST_PASSWORD,
        display_name="MindRoomResearch",
        user_id="@mindroom_research:localhost",
    )


@pytest.fixture
def mock_news_agent() -> AgentMatrixUser:
    """Create a mock news agent user."""
    return AgentMatrixUser(
        agent_name="news",
        password=TEST_PASSWORD,
        display_name="MindRoomNews",
        user_id="@mindroom_news:localhost",
    )


class TestRoutingRegression:
    """Regression tests for routing behavior."""

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.suggest_agent_for_message")
    async def test_router_does_not_respond_when_agent_mentioned(
        self,
        mock_suggest_agent: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that router doesn't activate when an agent is directly mentioned.

        Regression test for issue where both mentioned agent AND router-selected
        agent would respond to the same message.
        """
        test_room_id = "!research:localhost"

        # Set up research bot (the one being mentioned)
        research_bot = setup_test_bot(mock_research_agent, tmp_path, test_room_id)

        # Set up news bot
        news_bot = setup_test_bot(mock_news_agent, tmp_path, test_room_id)

        # Mock AI responses
        mock_ai_response.return_value = "I can help with that research!"
        mock_suggest_agent.return_value = "news"  # Router would pick news

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        research_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]
        news_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]

        # Create room with both agents
        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {
            mock_research_agent.user_id: MagicMock(),
            mock_news_agent.user_id: MagicMock(),
        }

        # User mentions research agent specifically
        message_event = MagicMock(spec=nio.RoomMessageText)
        message_event.sender = "@user:localhost"
        message_event.body = "@mindroom_research:localhost what can you do?"
        message_event.event_id = "$user_msg_123"
        message_event.source = {
            "content": {
                "body": "@mindroom_research:localhost what can you do?",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        }

        # Process with research bot - SHOULD respond
        await research_bot._on_message(mock_room, message_event)
        assert research_bot.client.room_send.call_count == 1  # type: ignore[union-attr]
        assert mock_ai_response.call_count == 1

        # Process with news bot - should NOT respond and NOT use router
        await news_bot._on_message(mock_room, message_event)
        assert news_bot.client.room_send.call_count == 0  # type: ignore[union-attr]
        # Router should NOT have been called
        assert mock_suggest_agent.call_count == 0

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.suggest_agent_for_message")
    async def test_router_activates_when_no_agent_mentioned(
        self,
        mock_suggest_agent: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that router DOES activate when no agents are mentioned."""
        test_room_id = "!research:localhost"

        # Create router agent
        router_agent = AgentMatrixUser(
            agent_name="router",
            password=TEST_PASSWORD,
            display_name="RouterAgent",
            user_id="@mindroom_router:localhost",
        )

        # Set up router bot
        router_bot = setup_test_bot(router_agent, tmp_path, test_room_id)

        # Set up research bot
        research_bot = setup_test_bot(mock_research_agent, tmp_path, test_room_id)

        # Set up news bot
        news_bot = setup_test_bot(mock_news_agent, tmp_path, test_room_id)

        # Mock AI responses
        mock_ai_response.return_value = "I can help with that!"
        mock_suggest_agent.return_value = "research"  # Router picks research

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        router_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]
        research_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]
        news_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]

        # Create room with all agents
        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {
            router_agent.user_id: MagicMock(),
            mock_research_agent.user_id: MagicMock(),
            mock_news_agent.user_id: MagicMock(),
        }

        # User message with NO mentions
        message_event = MagicMock(spec=nio.RoomMessageText)
        message_event.sender = "@user:localhost"
        message_event.body = "What's the latest news?"
        message_event.event_id = "$user_msg_456"
        message_event.source = {
            "content": {
                "body": "What's the latest news?",
            },
        }

        # Process with router bot (should handle routing)
        await router_bot._on_message(mock_room, message_event)

        # Router SHOULD have been called
        mock_suggest_agent.assert_called_once()
        # Router bot should send the routing message
        assert router_bot.client.room_send.call_count == 1  # type: ignore[union-attr]

        # Process with other bots - they should not do anything
        await research_bot._on_message(mock_room, message_event)
        await news_bot._on_message(mock_room, message_event)
        assert research_bot.client.room_send.call_count == 0  # type: ignore[union-attr]
        assert news_bot.client.room_send.call_count == 0  # type: ignore[union-attr]

    @pytest.mark.asyncio
    @patch("mindroom.teams.Team.arun")
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.teams.get_model_instance")
    @patch("mindroom.config.Config.from_yaml")
    async def test_multiple_mentions_each_responds_once(
        self,
        mock_from_yaml: MagicMock,
        mock_get_model_instance: MagicMock,
        mock_ai_response: AsyncMock,
        mock_team_arun: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that when multiple agents are mentioned, each responds exactly once."""
        # Create a mock config with proper models
        from mindroom.config import AgentConfig, Config, ModelConfig

        mock_config = Config(
            agents={
                "research": AgentConfig(display_name="ResearchAgent", rooms=["!research:localhost"]),
                "news": AgentConfig(display_name="NewsAgent", rooms=["!research:localhost"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="anthropic", id="claude-3-5-haiku-latest")},
        )
        mock_from_yaml.return_value = mock_config

        # Mock get_model_instance to return a mock model
        mock_model = MagicMock()
        mock_get_model_instance.return_value = mock_model

        test_room_id = "!research:localhost"

        # Set up both bots
        research_bot = setup_test_bot(mock_research_agent, tmp_path, test_room_id)

        # Mock orchestrator for research bot
        mock_orchestrator = MagicMock()
        mock_agent_bot = MagicMock()
        mock_agent_bot.agent = MagicMock()
        mock_orchestrator.agent_bots = {"research": mock_agent_bot, "news": mock_agent_bot}
        mock_orchestrator.current_config = research_bot.config
        mock_orchestrator.config = research_bot.config  # This is what teams.py uses
        research_bot.orchestrator = mock_orchestrator

        news_bot = setup_test_bot(mock_news_agent, tmp_path, test_room_id)

        # Mock orchestrator for news bot
        news_bot.orchestrator = mock_orchestrator

        # Mock AI responses and team response
        mock_ai_response.side_effect = ["Research response!", "News response!"]
        mock_team_arun.return_value = "Team response"

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        research_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]
        news_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]

        # Create room
        mock_room = MagicMock()
        mock_room.room_id = test_room_id

        # User mentions BOTH agents
        message_event = MagicMock(spec=nio.RoomMessageText)
        message_event.sender = "@user:localhost"
        message_event.body = "@mindroom_research:localhost and @mindroom_news:localhost, what do you think?"
        message_event.event_id = "$user_msg_789"
        message_event.source = {
            "content": {
                "body": "@mindroom_research:localhost and @mindroom_news:localhost, what do you think?",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost", "@mindroom_news:localhost"]},
            },
        }

        # Process with both bots
        await research_bot._on_message(mock_room, message_event)
        await news_bot._on_message(mock_room, message_event)

        # With simplified team behavior: multiple mentions should form a team
        # The alphabetically first agent (news) handles team formation
        # The other agent (research) does not respond individually
        assert research_bot.client.room_send.call_count == 0  # type: ignore[union-attr]  # No individual response
        assert news_bot.client.room_send.call_count == 1  # type: ignore[union-attr]  # Team response
        assert mock_team_arun.call_count == 1  # Team formed once

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    async def test_router_message_has_completion_marker(
        self,
        mock_ai_response: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that router messages trigger responses from mentioned agents.

        Regression test for potential issue where router mentions an agent but
        that agent ignores it.
        """
        test_room_id = "!research:localhost"

        # Create router agent
        router_agent = AgentMatrixUser(
            agent_name="router",
            password=TEST_PASSWORD,
            display_name="RouterAgent",
            user_id="@mindroom_router:localhost",
        )

        # Set up router bot
        router_bot = setup_test_bot(router_agent, tmp_path, test_room_id)

        # Set up research bot (will be mentioned by router)
        research_bot = setup_test_bot(mock_research_agent, tmp_path, test_room_id)

        # Mock AI response
        mock_ai_response.return_value = "I can help with that research question!"

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$router_msg"
        router_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]
        research_bot.client.room_send.return_value = mock_send_response  # type: ignore[union-attr]

        # Create room
        mock_room = MagicMock()
        mock_room.room_id = test_room_id

        # Simulate router message from router agent mentioning research
        # The router sends its messages
        router_message = MagicMock(spec=nio.RoomMessageText)
        router_message.sender = "@mindroom_router:localhost"
        router_message.body = "@research could you help with this?"
        router_message.event_id = "$router_msg"
        router_message.source = {
            "content": {
                "body": "@research could you help with this?",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        }

        # Process router message with research bot
        await research_bot._on_message(mock_room, router_message)

        # Research bot SHOULD respond
        assert research_bot.client.room_send.call_count == 1  # type: ignore[union-attr]
        assert mock_ai_response.call_count == 1
