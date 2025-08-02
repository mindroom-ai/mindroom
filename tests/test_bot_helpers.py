"""Tests for bot helper functions."""

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import _handle_invite_command, _handle_list_invites_command, _is_sender_other_agent
from mindroom.utils import should_route_to_agent


class TestBotHelpers:
    """Test bot helper functions."""

    def test_is_sender_other_agent(self):
        """Test _is_sender_other_agent function."""
        # Test with same user ID (self)
        assert not _is_sender_other_agent("@mindroom_calculator:localhost", "@mindroom_calculator:localhost")

        # Test with another agent
        assert _is_sender_other_agent("@mindroom_general:localhost", "@mindroom_calculator:localhost")

        # Test with regular user
        assert not _is_sender_other_agent("@user:localhost", "@mindroom_calculator:localhost")

    @pytest.mark.asyncio
    async def test_handle_invite_command_unknown_agent(self):
        """Test _handle_invite_command with unknown agent."""
        result = await _handle_invite_command(
            room_id="!room:localhost",
            thread_id=None,
            agent_name="unknown_agent",
            to_room=True,
            duration_hours=None,
            sender="@user:localhost",
            agent_domain="localhost",
            client=None,
        )

        assert "❌ Unknown agent: unknown_agent" in result
        assert "Available agents:" in result

    @pytest.mark.asyncio
    async def test_handle_invite_command_room_invite_no_client(self):
        """Test _handle_invite_command for room invite with no client."""
        with patch("mindroom.bot.load_config") as mock_config:
            mock_config.return_value.agents = {"calculator": MagicMock()}

            result = await _handle_invite_command(
                room_id="!room:localhost",
                thread_id=None,
                agent_name="calculator",
                to_room=True,
                duration_hours=24,
                sender="@user:localhost",
                agent_domain="localhost",
                client=None,
            )

            assert result == "❌ No Matrix client available to send invite"

    @pytest.mark.asyncio
    async def test_handle_invite_command_room_invite_success(self):
        """Test successful room invite."""
        mock_client = AsyncMock()
        mock_client.room_invite.return_value = nio.RoomInviteResponse()

        with patch("mindroom.bot.load_config") as mock_config:
            mock_config.return_value.agents = {"calculator": MagicMock()}

            result = await _handle_invite_command(
                room_id="!room:localhost",
                thread_id=None,
                agent_name="calculator",
                to_room=True,
                duration_hours=12,
                sender="@user:localhost",
                agent_domain="localhost",
                client=mock_client,
            )

            assert "✅ Invited @calculator to this room" in result
            assert "12 hours of inactivity" in result

    @pytest.mark.asyncio
    async def test_handle_invite_command_thread_invite(self):
        """Test thread invite."""
        with patch("mindroom.bot.load_config") as mock_config:
            mock_config.return_value.agents = {"calculator": MagicMock()}

            result = await _handle_invite_command(
                room_id="!room:localhost",
                thread_id="$thread123",
                agent_name="calculator",
                to_room=False,
                duration_hours=6,
                sender="@user:localhost",
                agent_domain="localhost",
                client=None,
            )

            assert "✅ Invited @calculator to this thread for 6 hours" in result
            assert "you've been invited to help in this thread!" in result

    @pytest.mark.asyncio
    async def test_handle_invite_command_thread_invite_no_thread_id(self):
        """Test thread invite without thread ID."""
        with patch("mindroom.bot.load_config") as mock_config:
            mock_config.return_value.agents = {"calculator": MagicMock()}

            result = await _handle_invite_command(
                room_id="!room:localhost",
                thread_id=None,
                agent_name="calculator",
                to_room=False,
                duration_hours=None,
                sender="@user:localhost",
                agent_domain="localhost",
                client=None,
            )

            assert "❌ Thread invites can only be used in a thread" in result

    @pytest.mark.asyncio
    async def test_handle_list_invites_command_empty(self):
        """Test list invites with no invites."""
        with (
            patch("mindroom.bot.room_invite_manager") as mock_room_mgr,
            patch("mindroom.bot.thread_invite_manager") as mock_thread_mgr,
        ):
            mock_room_mgr.get_room_invites = AsyncMock(return_value=[])
            mock_thread_mgr.get_thread_agents = AsyncMock(return_value=[])

            result = await _handle_list_invites_command("!room:localhost", None)

            assert result == "No agents are currently invited to this room or thread."

    @pytest.mark.asyncio
    async def test_handle_list_invites_command_with_invites(self):
        """Test list invites with active invites."""
        with (
            patch("mindroom.bot.room_invite_manager") as mock_room_mgr,
            patch("mindroom.bot.thread_invite_manager") as mock_thread_mgr,
        ):
            mock_room_mgr.get_room_invites = AsyncMock(return_value=["calculator", "research"])
            mock_thread_mgr.get_thread_agents = AsyncMock(return_value=["code"])

            result = await _handle_list_invites_command("!room:localhost", "$thread123")

            assert "**Room invites:**" in result
            assert "- @calculator (room invite)" in result
            assert "- @research (room invite)" in result
            assert "**Thread invites:**" in result
            assert "- @code (thread invite)" in result

    def test_should_route_to_agent(self):
        """Test should_route_to_agent function."""
        # Empty list
        assert not should_route_to_agent("calculator", [])

        # First agent should route
        assert should_route_to_agent("calculator", ["calculator", "general", "research"])

        # Other agents should not route
        assert not should_route_to_agent("general", ["calculator", "general", "research"])
        assert not should_route_to_agent("research", ["calculator", "general", "research"])
