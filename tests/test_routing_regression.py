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
from mindroom.matrix import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_invites import ThreadInviteManager


@pytest.fixture
def mock_research_agent() -> AgentMatrixUser:
    """Create a mock research agent user."""
    return AgentMatrixUser(
        agent_name="research",
        password="test_password",
        display_name="MindRoomResearch",
        user_id="@mindroom_research:localhost",
    )


@pytest.fixture
def mock_news_agent() -> AgentMatrixUser:
    """Create a mock news agent user."""
    return AgentMatrixUser(
        agent_name="news",
        password="test_password",
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
        research_bot = AgentBot(mock_research_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        research_bot.client = AsyncMock()
        research_bot.response_tracker = ResponseTracker(research_bot.agent_name, base_path=tmp_path)
        research_bot.thread_invite_manager = ThreadInviteManager(research_bot.client)

        # Set up news bot
        news_bot = AgentBot(mock_news_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        news_bot.client = AsyncMock()
        news_bot.response_tracker = ResponseTracker(news_bot.agent_name, base_path=tmp_path)
        news_bot.thread_invite_manager = ThreadInviteManager(news_bot.client)

        # Mock AI responses
        mock_ai_response.return_value = "I can help with that research!"
        mock_suggest_agent.return_value = "news"  # Router would pick news

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        research_bot.client.room_send.return_value = mock_send_response
        news_bot.client.room_send.return_value = mock_send_response

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
            }
        }

        # Process with research bot - SHOULD respond
        await research_bot._on_message(mock_room, message_event)
        assert research_bot.client.room_send.call_count == 1
        assert mock_ai_response.call_count == 1

        # Process with news bot - should NOT respond and NOT use router
        await news_bot._on_message(mock_room, message_event)
        assert news_bot.client.room_send.call_count == 0
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
            password="test_password",
            display_name="RouterAgent",
            user_id="@mindroom_router:localhost",
        )

        # Set up router bot
        router_bot = AgentBot(router_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        router_bot.client = AsyncMock()
        router_bot.response_tracker = ResponseTracker(router_bot.agent_name, base_path=tmp_path)
        router_bot.thread_invite_manager = ThreadInviteManager(router_bot.client)

        # Set up research bot
        research_bot = AgentBot(mock_research_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        research_bot.client = AsyncMock()
        research_bot.response_tracker = ResponseTracker(research_bot.agent_name, base_path=tmp_path)
        research_bot.thread_invite_manager = ThreadInviteManager(research_bot.client)

        # Set up news bot
        news_bot = AgentBot(mock_news_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        news_bot.client = AsyncMock()
        news_bot.response_tracker = ResponseTracker(news_bot.agent_name, base_path=tmp_path)
        news_bot.thread_invite_manager = ThreadInviteManager(news_bot.client)

        # Mock AI responses
        mock_ai_response.return_value = "I can help with that!"
        mock_suggest_agent.return_value = "research"  # Router picks research

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        router_bot.client.room_send.return_value = mock_send_response
        research_bot.client.room_send.return_value = mock_send_response
        news_bot.client.room_send.return_value = mock_send_response

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
            }
        }

        # Process with router bot (should handle routing)
        await router_bot._on_message(mock_room, message_event)

        # Router SHOULD have been called
        mock_suggest_agent.assert_called_once()
        # Router bot should send the routing message
        assert router_bot.client.room_send.call_count == 1

        # Process with other bots - they should not do anything
        await research_bot._on_message(mock_room, message_event)
        await news_bot._on_message(mock_room, message_event)
        assert research_bot.client.room_send.call_count == 0
        assert news_bot.client.room_send.call_count == 0

    @pytest.mark.asyncio
    @patch("mindroom.teams.Team.arun")
    @patch("mindroom.bot.ai_response")
    async def test_multiple_mentions_each_responds_once(
        self,
        mock_ai_response: AsyncMock,
        mock_team_arun: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that when multiple agents are mentioned, each responds exactly once."""
        test_room_id = "!research:localhost"

        # Set up both bots
        research_bot = AgentBot(mock_research_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        research_bot.client = AsyncMock()
        research_bot.response_tracker = ResponseTracker(research_bot.agent_name, base_path=tmp_path)
        research_bot.thread_invite_manager = ThreadInviteManager(research_bot.client)

        # Mock orchestrator for research bot
        mock_orchestrator = MagicMock()
        mock_agent_bot = MagicMock()
        mock_agent_bot.agent = MagicMock()
        mock_orchestrator.agent_bots = {"research": mock_agent_bot, "news": mock_agent_bot}
        research_bot.orchestrator = mock_orchestrator

        news_bot = AgentBot(mock_news_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        news_bot.client = AsyncMock()
        news_bot.response_tracker = ResponseTracker(news_bot.agent_name, base_path=tmp_path)
        news_bot.thread_invite_manager = ThreadInviteManager(news_bot.client)

        # Mock orchestrator for news bot
        news_bot.orchestrator = mock_orchestrator

        # Mock AI responses and team response
        mock_ai_response.side_effect = ["Research response!", "News response!"]
        mock_team_arun.return_value = "Team response"

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        research_bot.client.room_send.return_value = mock_send_response
        news_bot.client.room_send.return_value = mock_send_response

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
            }
        }

        # Process with both bots
        await research_bot._on_message(mock_room, message_event)
        await news_bot._on_message(mock_room, message_event)

        # With simplified team behavior: multiple mentions should form a team
        # The alphabetically first agent (news) handles team formation
        # The other agent (research) does not respond individually
        assert research_bot.client.room_send.call_count == 0  # No individual response
        assert news_bot.client.room_send.call_count == 1  # Team response
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
        """Test that router messages include completion marker so mentioned agents respond.

        Regression test for potential issue where router mentions an agent but
        that agent ignores it because there's no completion marker.
        """
        test_room_id = "!research:localhost"

        # Create router agent
        router_agent = AgentMatrixUser(
            agent_name="router",
            password="test_password",
            display_name="RouterAgent",
            user_id="@mindroom_router:localhost",
        )

        # Set up router bot
        router_bot = AgentBot(router_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        router_bot.client = AsyncMock()
        router_bot.response_tracker = ResponseTracker(router_bot.agent_name, base_path=tmp_path)
        router_bot.thread_invite_manager = ThreadInviteManager(router_bot.client)

        # Set up research bot (will be mentioned by router)
        research_bot = AgentBot(mock_research_agent, tmp_path, rooms=[test_room_id], enable_streaming=False)
        research_bot.client = AsyncMock()
        research_bot.response_tracker = ResponseTracker(research_bot.agent_name, base_path=tmp_path)
        research_bot.thread_invite_manager = ThreadInviteManager(research_bot.client)

        # Mock AI response
        mock_ai_response.return_value = "I can help with that research question!"

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$router_msg"
        router_bot.client.room_send.return_value = mock_send_response
        research_bot.client.room_send.return_value = mock_send_response

        # Create room
        mock_room = MagicMock()
        mock_room.room_id = test_room_id

        # Simulate router message from router agent mentioning research
        # The router always includes completion marker in its messages
        router_message = MagicMock(spec=nio.RoomMessageText)
        router_message.sender = "@mindroom_router:localhost"
        router_message.body = "@research could you help with this? ✓"
        router_message.event_id = "$router_msg"
        router_message.source = {
            "content": {
                "body": "@research could you help with this? ✓",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            }
        }

        # Process router message with research bot
        await research_bot._on_message(mock_room, router_message)

        # Research bot SHOULD respond (router messages always have completion marker)
        assert research_bot.client.room_send.call_count == 1
        assert mock_ai_response.call_count == 1
