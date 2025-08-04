"""Integration tests for multi-agent routing scenarios.

These tests simulate real-world scenarios to ensure agents behave correctly
when multiple agents are in a room and routing decisions need to be made.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.matrix import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_invites import ThreadInviteManager


class TestRoutingIntegration:
    """Integration tests for routing behavior with multiple agents."""

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response_streaming")
    @patch("mindroom.bot.suggest_agent_for_message")
    async def test_real_scenario_research_channel(
        self,
        mock_suggest_agent: AsyncMock,
        mock_ai_response_streaming: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Test the exact scenario reported: MindRoomResearch mentioned in research channel.

        When a user mentions @MindRoomResearch, only that agent should respond.
        MindRoomNews (alphabetically first) should NOT use the router to respond.
        """

        # Create generator for streaming response
        async def streaming_generator():
            yield "I am MindRoomResearch and I can help with research tasks"

        mock_ai_response_streaming.return_value = streaming_generator()

        # Create agents
        research_agent = AgentMatrixUser(
            agent_name="research",
            password="test",
            display_name="MindRoomResearch",
            user_id="@mindroom_research:localhost",
        )

        news_agent = AgentMatrixUser(
            agent_name="news",
            password="test",
            display_name="MindRoomNews",
            user_id="@mindroom_news:localhost",
        )

        # Set up bots
        research_bot = AgentBot(research_agent, tmp_path, rooms=["!research:localhost"], enable_streaming=True)
        news_bot = AgentBot(news_agent, tmp_path, rooms=["!research:localhost"], enable_streaming=True)

        # Mock clients
        for bot in [research_bot, news_bot]:
            bot.client = AsyncMock()
            bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
            bot.thread_invite_manager = ThreadInviteManager(bot.client)

            # Mock room_send for streaming
            mock_send = MagicMock()
            mock_send.__class__ = nio.RoomSendResponse
            mock_send.event_id = f"${bot.agent_name}_response"
            bot.client.room_send.return_value = mock_send

        # Create room with both agents
        mock_room = MagicMock()
        mock_room.room_id = "!research:localhost"
        mock_room.users = {
            research_agent.user_id: MagicMock(),
            news_agent.user_id: MagicMock(),
            "@user:localhost": MagicMock(),
        }

        # User asks research agent what it can do
        user_message = MagicMock(spec=nio.RoomMessageText)
        user_message.sender = "@user:localhost"
        user_message.body = "@mindroom_research:localhost what can you do?"
        user_message.event_id = "$user_question"
        user_message.source = {
            "content": {
                "body": "@mindroom_research:localhost what can you do?",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            }
        }

        # Process message with both bots
        await research_bot._on_message(mock_room, user_message)
        await news_bot._on_message(mock_room, user_message)

        # Only research bot should respond (streaming makes 2 calls)
        assert research_bot.client.room_send.call_count >= 1  # At least initial message
        assert news_bot.client.room_send.call_count == 0

        # Router should NOT have been called at all
        assert mock_suggest_agent.call_count == 0

        # Verify the response includes completion marker
        last_call = research_bot.client.room_send.call_args_list[-1]
        assert last_call[1]["content"]["body"].endswith(" ✓")

    @pytest.mark.asyncio
    @patch("mindroom.multi_agent.login_agent_user")
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.suggest_agent_for_message")
    async def test_orchestrator_routing_with_mentions(
        self,
        mock_suggest_agent: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_login: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Test orchestrator behavior when agents are mentioned."""
        # Mock login
        mock_client = AsyncMock()
        mock_client.user_id = "@mindroom_test:localhost"
        mock_login.return_value = mock_client

        # Create orchestrator with multiple agents
        orchestrator = MultiAgentOrchestrator(
            homeserver="http://localhost:8008",
            agent_names=["calculator", "general", "research"],
            storage_path=tmp_path,
            room_id="!test:localhost",
        )

        # Mock agent creation
        with patch("mindroom.multi_agent.create_agent_user") as mock_create:
            # Create mock agents
            calc_agent = AgentMatrixUser(
                agent_name="calculator",
                user_id="@mindroom_calculator:localhost",
                display_name="Calculator",
                password="test",
            )
            general_agent = AgentMatrixUser(
                agent_name="general",
                user_id="@mindroom_general:localhost",
                display_name="General",
                password="test",
            )
            research_agent = AgentMatrixUser(
                agent_name="research",
                user_id="@mindroom_research:localhost",
                display_name="Research",
                password="test",
            )

            mock_create.side_effect = [calc_agent, general_agent, research_agent]

            # Initialize orchestrator
            await orchestrator.initialize()

            # Start bots
            await orchestrator.start()

            # Verify only one bot handles routing (alphabetically first with room access)
            # In this case, calculator is alphabetically first

            # Create test room
            mock_room = MagicMock()
            mock_room.room_id = "!test:localhost"
            mock_room.users = {
                calc_agent.user_id: MagicMock(),
                general_agent.user_id: MagicMock(),
                research_agent.user_id: MagicMock(),
            }

            # Test 1: Message with no mentions - router should activate
            no_mention_msg = MagicMock(spec=nio.RoomMessageText)
            no_mention_msg.sender = "@user:localhost"
            no_mention_msg.body = "I need help"
            no_mention_msg.event_id = "$msg1"
            no_mention_msg.source = {"content": {"body": "I need help"}}

            mock_suggest_agent.return_value = "general"
            mock_ai_response.return_value = "How can I help?"

            # Only routing bot should use router
            for bot in orchestrator.bots:
                bot.client = mock_client
                bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
                bot.thread_invite_manager = ThreadInviteManager(bot.client)

            # Process with all bots
            for bot in orchestrator.bots:
                await bot._on_message(mock_room, no_mention_msg)

            # Router should have been called once (by calculator)
            assert mock_suggest_agent.call_count == 1

            # Test 2: Message mentioning specific agent - no router
            mock_suggest_agent.reset_mock()

            mention_msg = MagicMock(spec=nio.RoomMessageText)
            mention_msg.sender = "@user:localhost"
            mention_msg.body = "@mindroom_research:localhost analyze this"
            mention_msg.event_id = "$msg2"
            mention_msg.source = {
                "content": {
                    "body": "@mindroom_research:localhost analyze this",
                    "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
                }
            }

            # Process with all bots
            for bot in orchestrator.bots:
                await bot._on_message(mock_room, mention_msg)

            # Router should NOT have been called
            assert mock_suggest_agent.call_count == 0
