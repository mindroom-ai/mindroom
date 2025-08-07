"""Tests for AI routing functionality."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.agent_config import describe_agent
from mindroom.bot import AgentBot
from mindroom.matrix import AgentMatrixUser
from mindroom.routing import AgentSuggestion, suggest_agent_for_message
from mindroom.thread_utils import extract_agent_name, has_any_agent_mentions_in_thread


class TestAIRouting:
    """Tests for AI routing in multi-agent threads."""

    @pytest.mark.asyncio
    async def test_suggest_agent_for_message_basic(self) -> None:
        """Test basic agent suggestion functionality."""
        with patch("mindroom.routing.get_model_instance"):
            # Mock the Agent and response
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = AgentSuggestion(
                agent_name="calculator", reasoning="User is asking about math calculation"
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.routing.Agent", return_value=mock_agent):
                result = await suggest_agent_for_message("What is 2 + 2?", ["calculator", "general"], None)

                assert result == "calculator"
                assert "calculator" in mock_agent.arun.call_args[0][0]
                assert "general" in mock_agent.arun.call_args[0][0]

    @pytest.mark.asyncio
    async def test_suggest_agent_with_thread_context(self) -> None:
        """Test agent suggestion with thread history."""
        thread_context = [
            {"sender": "@user:localhost", "body": "I need help with my taxes"},
            {"sender": "@mindroom_finance:localhost", "body": "I can help with that"},
        ]

        with patch("mindroom.routing.get_model_instance"):
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = AgentSuggestion(agent_name="finance", reasoning="Continuing financial discussion")
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.routing.Agent", return_value=mock_agent):
                result = await suggest_agent_for_message(
                    "How do I calculate deductions?", ["calculator", "finance", "general"], thread_context
                )

                assert result == "finance"
                # Check that context was included in prompt
                prompt = mock_agent.arun.call_args[0][0]
                assert "taxes" in prompt
                assert "Previous messages:" in prompt

    @pytest.mark.asyncio
    async def test_suggest_agent_unavailable_raises_assertion(self) -> None:
        """Test that suggesting unavailable agent raises assertion error."""
        with patch("mindroom.routing.get_model_instance"):
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            # AI suggests an agent not in available list
            mock_response.content = AgentSuggestion(
                agent_name="code",  # Not available
                reasoning="User asking about programming",
            )
            mock_agent.arun.return_value = mock_response

            with (
                patch("mindroom.routing.Agent", return_value=mock_agent),
                pytest.raises(AssertionError, match="AI suggested code but available agents are"),
            ):
                await suggest_agent_for_message(
                    "How do I write a Python function?",
                    ["calculator", "general"],  # code not available
                    None,
                )

    @pytest.mark.asyncio
    async def test_suggest_agent_error_handling(self) -> None:
        """Test error handling in agent suggestion."""
        with patch("mindroom.routing.get_model_instance") as mock_model:
            mock_model.side_effect = ValueError("Model error")

            result = await suggest_agent_for_message("Test message", ["general"], None)

            assert result is None

    @pytest.mark.asyncio
    async def test_only_router_agent_routes(self) -> None:
        """Test that only the router agent handles routing."""
        # Create general agent (not router)
        agent = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="GeneralAgent",
            password="test",
            access_token="token",
        )

        bot = AgentBot(agent, Path("/tmp"))

        mock_room = MagicMock()
        mock_room.users = MagicMock()
        mock_room.users.keys.return_value = [
            "@mindroom_calculator:localhost",
            "@mindroom_general:localhost",
            "@user:localhost",
        ]

        mock_event = MagicMock()
        mock_event.body = "Test message"

        with patch("mindroom.bot.suggest_agent_for_message") as mock_suggest:
            await bot._handle_ai_routing(mock_room, mock_event, [])

            # Should not call routing since general is not the router agent
            mock_suggest.assert_not_called()


class TestThreadUtils:
    """Test thread utility functions."""

    def test_has_any_agent_mentions_in_thread_with_mentions(self) -> None:
        """Test detecting agent mentions in thread."""
        thread_history = [
            {
                "sender": "@user:example.org",
                "body": "Hello",
                "content": {},
            },
            {
                "sender": "@user:example.org",
                "body": "@calculator help me",
                "content": {"m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]}},
            },
        ]

        assert has_any_agent_mentions_in_thread(thread_history) is True

    def test_has_any_agent_mentions_in_thread_no_mentions(self) -> None:
        """Test thread with no agent mentions."""
        thread_history = [
            {
                "sender": "@user:example.org",
                "body": "Hello",
                "content": {},
            },
            {
                "sender": "@mindroom_calculator:localhost",
                "body": "Hi there!",
                "content": {},
            },
        ]

        assert has_any_agent_mentions_in_thread(thread_history) is False

    def test_has_any_agent_mentions_in_thread_user_mentions(self) -> None:
        """Test thread with only user mentions (not agents)."""
        thread_history = [
            {
                "sender": "@user:example.org",
                "body": "@friend check this out",
                "content": {"m.mentions": {"user_ids": ["@friend:example.org"]}},
            },
        ]

        assert has_any_agent_mentions_in_thread(thread_history) is False

    def test_extract_agent_name_rejects_unconfigured(self) -> None:
        """Test that unconfigured agents are not recognized."""
        # This should return None because "fake_agent" is not in config.yaml
        assert extract_agent_name("@mindroom_fake_agent:localhost") is None

        # But real agents should work
        assert extract_agent_name("@mindroom_calculator:localhost") == "calculator"

        # Regular users should still be rejected
        assert extract_agent_name("@mindroom_user:localhost") is None
        assert extract_agent_name("@regular_user:localhost") is None


class TestAgentDescription:
    """Test agent description functionality."""

    def test_describe_agent_with_tools(self) -> None:
        """Test describing an agent with tools."""
        description = describe_agent("calculator")

        assert "calculator" in description
        assert "Solve mathematical problems" in description
        assert "Tools: calculator" in description
        assert "Use the calculator tools" in description

    def test_describe_agent_without_tools(self) -> None:
        """Test describing an agent without tools."""
        description = describe_agent("general")

        assert "general" in description
        assert "general-purpose assistant" in description
        assert "Tools:" not in description  # No tools section
        assert "Always provide a clear" in description

    def test_describe_unknown_agent(self) -> None:
        """Test describing an unknown agent."""
        description = describe_agent("nonexistent")

        assert description == "nonexistent: Unknown agent or team"
