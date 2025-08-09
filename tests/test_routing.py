"""Tests for AI routing functionality."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.agent_config import describe_agent, load_config
from mindroom.bot import AgentBot
from mindroom.matrix.users import AgentMatrixUser
from mindroom.models import AgentConfig, Config, ModelConfig, RouterConfig
from mindroom.routing import AgentSuggestion, suggest_agent_for_message
from mindroom.thread_utils import extract_agent_name, has_any_agent_mentions_in_thread


class TestAIRouting:
    """Tests for AI routing in multi-agent threads."""

    @pytest.mark.asyncio
    async def test_suggest_agent_for_message_basic(self) -> None:
        """Test basic agent suggestion functionality."""
        config = Config(router=RouterConfig(model="default"))

        with patch("mindroom.routing.get_model_instance"):
            # Mock the Agent and response
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = AgentSuggestion(
                agent_name="calculator", reasoning="User is asking about math calculation"
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.routing.Agent", return_value=mock_agent):
                result = await suggest_agent_for_message("What is 2 + 2?", ["calculator", "general"], config)

                assert result == "calculator"
                assert "calculator" in mock_agent.arun.call_args[0][0]
                assert "general" in mock_agent.arun.call_args[0][0]

    @pytest.mark.asyncio
    async def test_suggest_agent_with_thread_context(self) -> None:
        """Test agent suggestion with thread history."""
        config = Config(router=RouterConfig(model="default"))
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
                    "How do I calculate deductions?", ["calculator", "finance", "general"], config, thread_context
                )

                assert result == "finance"
                # Check that context was included in prompt
                prompt = mock_agent.arun.call_args[0][0]
                assert "taxes" in prompt
                assert "Previous messages:" in prompt

    @pytest.mark.asyncio
    async def test_suggest_agent_unavailable_raises_assertion(self) -> None:
        """Test that suggesting unavailable agent raises assertion error."""
        config = Config(router=RouterConfig(model="default"))

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
                    config,
                )

    @pytest.mark.asyncio
    async def test_suggest_agent_error_handling(self) -> None:
        """Test error handling in agent suggestion."""
        config = Config(router=RouterConfig(model="default"))

        with patch("mindroom.routing.get_model_instance") as mock_model:
            mock_model.side_effect = ValueError("Model error")

            result = await suggest_agent_for_message("Test message", ["general"], config)

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

        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(agent, Path("/tmp"), config=config)

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
            # Should raise AssertionError since general is not the router agent
            with pytest.raises(AssertionError):
                await bot._handle_ai_routing(mock_room, mock_event, [])

            # Should not call routing since it failed the assertion
            mock_suggest.assert_not_called()


class TestThreadUtils:
    """Test thread utility functions."""

    def setup_method(self):
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

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

        assert has_any_agent_mentions_in_thread(thread_history, self.config) is True

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

        assert has_any_agent_mentions_in_thread(thread_history, self.config) is False

    def test_has_any_agent_mentions_in_thread_user_mentions(self) -> None:
        """Test thread with only user mentions (not agents)."""
        thread_history = [
            {
                "sender": "@user:example.org",
                "body": "@friend check this out",
                "content": {"m.mentions": {"user_ids": ["@friend:example.org"]}},
            },
        ]

        assert has_any_agent_mentions_in_thread(thread_history, self.config) is False

    def test_extract_agent_name_rejects_unconfigured(self) -> None:
        """Test that unconfigured agents are not recognized."""
        # This should return None because "fake_agent" is not in config.yaml
        assert extract_agent_name("@mindroom_fake_agent:localhost", self.config) is None

        # But real agents should work
        assert extract_agent_name("@mindroom_calculator:localhost", self.config) == "calculator"

        # Regular users should still be rejected
        assert extract_agent_name("@mindroom_user:localhost", self.config) is None
        assert extract_agent_name("@regular_user:localhost", self.config) is None


class TestAgentDescription:
    """Test agent description functionality."""

    def test_describe_agent_with_tools(self) -> None:
        """Test describing an agent with tools."""
        config = load_config()
        description = describe_agent("calculator", config)

        assert "calculator" in description
        assert "Solve mathematical problems" in description
        assert "Tools: calculator" in description
        assert "Use the calculator tools" in description

    def test_describe_agent_without_tools(self) -> None:
        """Test describing an agent without tools."""
        config = load_config()
        description = describe_agent("general", config)

        assert "general" in description
        assert "general-purpose assistant" in description
        assert "Tools:" not in description  # No tools section
        assert "Always provide a clear" in description

    def test_describe_unknown_agent(self) -> None:
        """Test describing an unknown agent."""
        config = load_config()
        description = describe_agent("nonexistent", config)

        assert description == "nonexistent: Unknown agent or team"
