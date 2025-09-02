"""Tests for Matrix presence and status message utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config import AgentConfig, Config, ModelConfig, TeamConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.presence import (
    build_agent_status_message,
    is_user_online,
    set_presence_status,
    should_use_streaming,
)


class TestSetPresenceStatus:
    """Test the set_presence_status function."""

    @pytest.mark.asyncio
    async def test_set_presence_success(self) -> None:
        """Test successfully setting presence status."""
        mock_client = AsyncMock()
        mock_response = nio.PresenceSetResponse()
        mock_client.set_presence.return_value = mock_response

        await set_presence_status(mock_client, "Test status", "online")

        mock_client.set_presence.assert_called_once_with("online", "Test status")

    @pytest.mark.asyncio
    async def test_set_presence_failure(self) -> None:
        """Test handling presence set failure."""
        mock_client = AsyncMock()
        mock_response = MagicMock()  # Not a PresenceSetResponse
        mock_client.set_presence.return_value = mock_response

        await set_presence_status(mock_client, "Test status")

        mock_client.set_presence.assert_called_once_with("online", "Test status")


class TestBuildAgentStatusMessage:
    """Test the build_agent_status_message function."""

    def test_router_agent_status(self) -> None:
        """Test building status message for router agent."""
        config = Config(
            router={"model": "gpt-4"},
            models={"gpt-4": ModelConfig(provider="openai", id="gpt-4-turbo")},
        )

        status = build_agent_status_message(ROUTER_AGENT_NAME, config)

        assert "🤖 Model: openai/gpt-4-turbo" in status
        assert "📍 Routes messages to appropriate agents" in status

    def test_regular_agent_status_with_tools(self) -> None:
        """Test building status message for regular agent with tools."""
        config = Config(
            agents={
                "researcher": AgentConfig(
                    display_name="Research Agent",
                    role="Research specialist focused on finding information",
                    tools=["web_search", "arxiv", "wikipedia", "news"],
                    model="claude",
                ),
            },
            models={"claude": ModelConfig(provider="anthropic", id="claude-3-opus")},
        )

        status = build_agent_status_message("researcher", config)

        assert "🤖 Model: anthropic/claude-3-opus" in status
        assert "💼 Research specialist focused on finding information" in status
        assert "🔧 4 tools available" in status

    def test_regular_agent_status_no_tools(self) -> None:
        """Test building status message for regular agent without tools."""
        config = Config(
            agents={
                "assistant": AgentConfig(
                    display_name="General Assistant",
                    role="General purpose assistant",
                    model="default",
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="llama3")},
        )

        status = build_agent_status_message("assistant", config)

        assert "🤖 Model: ollama/llama3" in status
        assert "💼 General purpose assistant" in status
        assert "🔧" not in status  # No tools section

    def test_team_agent_status(self) -> None:
        """Test building status message for team agent."""
        config = Config(
            teams={
                "research_team": TeamConfig(
                    display_name="Research Team",
                    role="Collaborative research team",
                    agents=["researcher", "analyst", "writer", "reviewer", "editor", "fact_checker"],
                    model="gpt-4",
                ),
            },
            models={"gpt-4": ModelConfig(provider="openai", id="gpt-4")},
        )

        status = build_agent_status_message("research_team", config)

        assert "🤖 Model: openai/gpt-4" in status
        assert "👥 Collaborative research team" in status
        assert "🤝 Team: researcher, analyst, writer, reviewer, editor" in status  # First 5 only
        assert "fact_checker" not in status  # 6th agent not shown

    def test_long_status_message(self) -> None:
        """Test that long status messages are built correctly."""
        config = Config(
            agents={
                "agent": AgentConfig(
                    display_name="Agent",
                    role="A" * 200,  # Very long role (truncated to 100 chars internally)
                    tools=["tool1", "tool2", "tool3", "tool4", "tool5", "tool6", "tool7", "tool8"],
                    model="very_long_model_name",
                ),
            },
            models={
                "very_long_model_name": ModelConfig(
                    provider="provider",
                    id="model_id",
                ),
            },
        )

        status = build_agent_status_message("agent", config)

        # Just verify it builds without error and includes expected parts
        assert "Model: provider/model_id" in status
        assert "A" * 100 in status  # Role truncated to 100 chars
        assert "8 tools available" in status

    def test_unknown_model_fallback(self) -> None:
        """Test fallback when model is not in config.models."""
        config = Config(
            agents={
                "agent": AgentConfig(
                    display_name="Agent",
                    model="unknown_model",
                ),
            },
        )

        status = build_agent_status_message("agent", config)

        assert "🤖 Model: unknown_model" in status


class TestIsUserOnline:
    """Test the is_user_online function."""

    @pytest.mark.asyncio
    async def test_user_online(self) -> None:
        """Test detecting online user."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.presence = "online"
        mock_response.last_active_ago = None
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is True
        mock_client.get_presence.assert_called_once_with("@user:example.com")

    @pytest.mark.asyncio
    async def test_user_unavailable_considered_online(self) -> None:
        """Test that 'unavailable' status is considered online for streaming."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.presence = "unavailable"  # Idle but client open
        mock_response.last_active_ago = 300000  # 5 minutes
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is True

    @pytest.mark.asyncio
    async def test_user_offline(self) -> None:
        """Test detecting offline user."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.presence = "offline"
        mock_response.last_active_ago = 3600000  # 1 hour
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_presence_error(self) -> None:
        """Test handling presence API error."""
        mock_client = AsyncMock()
        mock_response = nio.PresenceGetError(message="Not found")
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_presence_exception(self) -> None:
        """Test handling exception during presence check."""
        mock_client = AsyncMock()
        mock_client.get_presence.side_effect = Exception("Network error")

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False  # Defaults to False on error


class TestShouldUseStreaming:
    """Test the should_use_streaming function."""

    @pytest.mark.asyncio
    async def test_streaming_disabled_globally(self) -> None:
        """Test that streaming is disabled when ENABLE_STREAMING is False."""
        mock_client = AsyncMock()

        with patch("mindroom.matrix.presence.ENABLE_STREAMING", False):
            result = await should_use_streaming(mock_client, "!room:example.com", "@user:example.com")

        assert result is False
        mock_client.get_presence.assert_not_called()

    @pytest.mark.asyncio
    async def test_streaming_no_requester(self) -> None:
        """Test defaulting to streaming when no requester specified."""
        mock_client = AsyncMock()

        with patch("mindroom.matrix.presence.ENABLE_STREAMING", True):
            result = await should_use_streaming(mock_client, "!room:example.com", None)

        assert result is True
        mock_client.get_presence.assert_not_called()

    @pytest.mark.asyncio
    async def test_streaming_user_online(self) -> None:
        """Test enabling streaming when user is online."""
        mock_client = AsyncMock()

        with (
            patch("mindroom.matrix.presence.ENABLE_STREAMING", True),
            patch("mindroom.matrix.presence.is_user_online", return_value=True) as mock_is_online,
        ):
            result = await should_use_streaming(mock_client, "!room:example.com", "@user:example.com")

        assert result is True
        mock_is_online.assert_called_once_with(mock_client, "@user:example.com")

    @pytest.mark.asyncio
    async def test_streaming_user_offline(self) -> None:
        """Test disabling streaming when user is offline."""
        mock_client = AsyncMock()

        with (
            patch("mindroom.matrix.presence.ENABLE_STREAMING", True),
            patch("mindroom.matrix.presence.is_user_online", return_value=False) as mock_is_online,
        ):
            result = await should_use_streaming(mock_client, "!room:example.com", "@user:example.com")

        assert result is False
        mock_is_online.assert_called_once_with(mock_client, "@user:example.com")
