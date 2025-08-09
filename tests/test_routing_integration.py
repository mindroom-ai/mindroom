"""Integration tests for multi-agent routing scenarios.

These tests simulate real-world scenarios to ensure agents behave correctly
when multiple agents are in a room and routing decisions need to be made.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.matrix.users import AgentMatrixUser
from mindroom.models import Config, RouterConfig
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
        MindRoomNews should NOT respond or route.
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
        config = Config(router=RouterConfig(model="default"))

        research_bot = AgentBot(
            research_agent, tmp_path, rooms=["!research:localhost"], enable_streaming=True, config=config
        )
        config = Config(router=RouterConfig(model="default"))

        news_bot = AgentBot(news_agent, tmp_path, rooms=["!research:localhost"], enable_streaming=True, config=config)

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

        # Verify the response was sent
        last_call = research_bot.client.room_send.call_args_list[-1]
        assert "body" in last_call[1]["content"]
