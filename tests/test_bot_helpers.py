"""Tests for bot helper functions."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.commands import handle_invite_command, handle_list_invites_command
from mindroom.config import Config, RouterConfig
from mindroom.thread_invites import ThreadInviteManager


class TestBotHelpers:
    """Test bot helper functions."""

    @pytest.mark.asyncio
    async def test_handle_invite_command_unknown_agent(self) -> None:
        """Test _handle_invite_command with unknown agent."""
        mock_client = AsyncMock()
        mock_thread_mgr = AsyncMock(spec=ThreadInviteManager)
        config = Config(router=RouterConfig(model="default"))
        config.agents = {"calculator": MagicMock(), "general": MagicMock()}

        result = await handle_invite_command(
            room_id="!room:localhost",
            thread_id="$thread123",
            agent_name="unknown_agent",
            sender="@user:localhost",
            agent_domain="localhost",
            client=mock_client,
            thread_invite_manager=mock_thread_mgr,
            config=config,
        )

        assert "❌ Unknown agent: @unknown_agent" in result
        assert "Available agents:" in result

    @pytest.mark.asyncio
    async def test_handle_invite_command_thread_invite_success(self) -> None:
        """Test successful thread invite."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.members = []
        mock_client.joined_members.return_value = mock_response
        mock_client.room_invite.return_value = nio.RoomInviteResponse()

        config = Config(router=RouterConfig(model="default"))
        config.agents = {"calculator": MagicMock()}

        mock_thread_mgr = AsyncMock(spec=ThreadInviteManager)

        result = await handle_invite_command(
            room_id="!room:localhost",
            thread_id="$thread123",
            agent_name="calculator",
            sender="@user:localhost",
            agent_domain="localhost",
            client=mock_client,
            thread_invite_manager=mock_thread_mgr,
            config=config,
        )

        assert "✅ Invited @calculator to this thread" in result

    @pytest.mark.asyncio
    async def test_handle_invite_command_thread_invite(self) -> None:
        """Test thread invite."""
        config = Config(router=RouterConfig(model="default"))
        config.agents = {"calculator": MagicMock()}

        # Create mock client
        mock_client = AsyncMock()
        mock_members_response = MagicMock()
        mock_members_response.members = []
        mock_client.joined_members.return_value = mock_members_response
        mock_client.room_invite.return_value = MagicMock(spec=nio.RoomInviteResponse)

        mock_thread_mgr = AsyncMock(spec=ThreadInviteManager)

        result = await handle_invite_command(
            room_id="!room:localhost",
            thread_id="$thread123",
            agent_name="calculator",
            sender="@user:localhost",
            agent_domain="localhost",
            client=mock_client,
            thread_invite_manager=mock_thread_mgr,
            config=config,
        )

        assert "✅ Invited @calculator to this thread" in result
        assert "you've been invited to help in this thread!" in result

    @pytest.mark.asyncio
    async def test_handle_list_invites_command_empty(self) -> None:
        """Test list invites with no invites."""
        mock_thread_mgr = AsyncMock(spec=ThreadInviteManager)
        mock_thread_mgr.get_thread_agents = AsyncMock(return_value=[])

        result = await handle_list_invites_command("!room:localhost", "$thread123", mock_thread_mgr)

        assert result == "No agents are currently invited to this thread."

    @pytest.mark.asyncio
    async def test_handle_list_invites_command_with_invites(self) -> None:
        """Test list invites with active invites."""
        mock_thread_mgr = AsyncMock(spec=ThreadInviteManager)
        mock_thread_mgr.get_thread_agents = AsyncMock(return_value=["calculator", "research", "code"])

        result = await handle_list_invites_command("!room:localhost", "$thread123", mock_thread_mgr)

        assert "**Invited agents in this thread:**" in result
        assert "- @calculator" in result
        assert "- @research" in result
        assert "- @code" in result
