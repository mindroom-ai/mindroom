"""Tests for DM room preservation during cleanup operations."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config
from mindroom.matrix.users import AgentMatrixUser
from mindroom.room_cleanup import _cleanup_orphaned_bots_in_room, cleanup_all_orphaned_bots
from tests.conftest import TEST_PASSWORD


@pytest.mark.asyncio
class TestDMPreservationDuringCleanup:
    """Test that DM rooms are preserved during various cleanup operations."""

    async def test_agent_cleanup_preserves_dm_rooms(self, tmp_path: Path) -> None:
        """Test that AgentBot.cleanup() preserves DM rooms when DMs are enabled."""
        # Create config with DMs enabled
        config = Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    role="Test agent",
                ),
            },
        )

        # Create bot instance
        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            user_id="@mindroom_test_agent:server",
            display_name="Test Agent",
            password=TEST_PASSWORD,
            access_token="test_token",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!regular:server", "!another:server"],
        )
        bot.client = AsyncMock()
        bot.logger = MagicMock()

        # Mock joined rooms - mix of configured and unconfigured (DM) rooms
        joined_rooms = ["!regular:server", "!dm:server", "!another:server", "!otherdm:server"]

        # Mock is_dm_room to return True for DM rooms
        async def mock_is_dm_room(client: Any, room_id: str) -> bool:  # noqa: ARG001, ANN401
            return room_id in ["!dm:server", "!otherdm:server"]

        with (
            patch("mindroom.bot.get_joined_rooms", return_value=joined_rooms),
            patch("mindroom.bot.leave_room", return_value=True) as mock_leave,
            patch("mindroom.bot.is_dm_room", side_effect=mock_is_dm_room),
        ):
            await bot.cleanup()

            # Should leave configured rooms but not the DM rooms
            assert mock_leave.call_count == 2
            # Get the actual arguments from the calls
            leave_calls = [call[0][1] for call in mock_leave.call_args_list]  # call[0][1] is the room_id
            assert "!regular:server" in leave_calls
            assert "!another:server" in leave_calls
            assert "!dm:server" not in leave_calls
            assert "!otherdm:server" not in leave_calls

            # Check logging
            bot.logger.debug.assert_any_call("Preserving DM room !dm:server during cleanup")
            bot.logger.debug.assert_any_call("Preserving DM room !otherdm:server during cleanup")

    async def test_agent_cleanup_leaves_all_rooms(self, tmp_path: Path) -> None:
        """Test that AgentBot.cleanup() leaves all non-DM rooms."""
        # Create config
        config = Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    role="Test agent",
                ),
            },
        )

        # Create bot instance
        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            user_id="@mindroom_test_agent:server",
            display_name="Test Agent",
            password=TEST_PASSWORD,
            access_token="test_token",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!configured:server"],  # Only one configured room
        )
        bot.client = AsyncMock()
        bot.logger = MagicMock()

        # Mock joined rooms - mix of configured and non-configured rooms
        joined_rooms = ["!configured:server", "!unconfigured1:server", "!unconfigured2:server"]

        # Mock is_dm_room to return False for all rooms (none are DMs)
        async def mock_is_dm_room(client: Any, room_id: str) -> bool:  # noqa: ARG001, ANN401
            return False

        with (
            patch("mindroom.bot.get_joined_rooms", return_value=joined_rooms),
            patch("mindroom.bot.leave_room", return_value=True) as mock_leave,
            patch("mindroom.bot.is_dm_room", side_effect=mock_is_dm_room),
        ):
            await bot.cleanup()

            # Should leave all rooms when none are DMs
            assert mock_leave.call_count == 3
            leave_calls = [call.args[1] for call in mock_leave.call_args_list]
            assert "!configured:server" in leave_calls
            assert "!unconfigured1:server" in leave_calls
            assert "!unconfigured2:server" in leave_calls

    async def test_orphaned_bot_cleanup_skips_dm_rooms(self, tmp_path: Path) -> None:  # noqa: ARG002
        """Test that orphaned bot cleanup skips DM rooms (unconfigured rooms) when DM mode is enabled."""
        client = AsyncMock()
        config = Config(
            agents={
                "configured_agent": AgentConfig(
                    display_name="Configured Agent",
                    role="Agent that should be in rooms",
                ),
            },
        )
        # Mock a room with no configured bots (DM room)
        with patch(
            "mindroom.config.Config.get_configured_bots_for_room",
            return_value=set(),  # No bots configured for this room
        ):
            kicked_bots = await _cleanup_orphaned_bots_in_room(
                client,
                "!dm:server",
                config,
            )

            # Should not kick anyone from DM room
            assert kicked_bots == []
            # Should not even try to kick
            assert not client.room_kick.called

    async def test_orphaned_bot_cleanup_processes_regular_rooms(self, tmp_path: Path) -> None:  # noqa: ARG002
        """Test that orphaned bot cleanup processes rooms when DM mode is disabled."""
        client = AsyncMock()
        config = Config(
            agents={
                "configured_agent": AgentConfig(
                    display_name="Configured Agent",
                    role="Agent that should be in rooms",
                ),
            },
        )
        # Mock room members - includes an orphaned bot
        members = ["@user:server", "@mindroom_orphaned:server", "@mindroom_configured_agent:server"]

        with (
            patch(
                "mindroom.room_cleanup.get_room_members",
                return_value=members,
            ),
            patch(
                "mindroom.room_cleanup._get_all_known_bot_usernames",
                return_value={"mindroom_orphaned", "mindroom_configured_agent"},
            ),
            patch(
                "mindroom.config.Config.get_configured_bots_for_room",
                return_value={"mindroom_configured_agent"},
            ),
        ):
            client.room_kick = AsyncMock(return_value=nio.RoomKickResponse())

            kicked_bots = await _cleanup_orphaned_bots_in_room(
                client,
                "!regular:server",
                config,
            )

            # Should kick the orphaned bot
            assert kicked_bots == ["mindroom_orphaned"]
            client.room_kick.assert_called_once_with(
                "!regular:server",
                "@mindroom_orphaned:server",
                reason="Bot no longer configured for this room",
            )

    async def test_cleanup_all_orphaned_bots_respects_dm_rooms(self, tmp_path: Path) -> None:  # noqa: ARG002
        """Test that cleanup_all_orphaned_bots respects DM rooms when DM mode is enabled."""
        client = AsyncMock()
        config = Config(
            agents={
                "agent": AgentConfig(
                    display_name="Agent",
                    role="Test agent",
                ),
            },
        )

        # Mock joined rooms - mix of configured and DM rooms
        joined_rooms = ["!configured:server", "!dm:server", "!another_dm:server"]

        def mock_get_configured_bots(room_id: str) -> set[str]:
            # Only !configured:server has configured bots
            if room_id == "!configured:server":
                return {"mindroom_agent"}
            return set()  # DM rooms have no configured bots

        # Mock is_dm_room to return True for DM rooms
        async def mock_is_dm_room(client: Any, room_id: str) -> bool:  # noqa: ARG001, ANN401
            return room_id in ["!dm:server", "!another_dm:server"]

        with (
            patch("mindroom.room_cleanup.get_joined_rooms", return_value=joined_rooms),
            patch(
                "mindroom.room_cleanup.get_room_members",
                return_value=["@user:server", "@mindroom_orphaned:server"],
            ),
            patch(
                "mindroom.room_cleanup._get_all_known_bot_usernames",
                return_value={"mindroom_orphaned", "mindroom_agent"},
            ),
            patch(
                "mindroom.config.Config.get_configured_bots_for_room",
                side_effect=mock_get_configured_bots,
            ),
            patch("mindroom.room_cleanup.is_dm_room", side_effect=mock_is_dm_room),
        ):
            client.room_kick = AsyncMock(return_value=nio.RoomKickResponse())
            result = await cleanup_all_orphaned_bots(client, config)

            # Should process configured room but skip DM rooms
            assert "!configured:server" in result
            assert "!dm:server" not in result
            assert "!another_dm:server" not in result

            # Should only kick from configured room
            assert client.room_kick.call_count == 1
            assert client.room_kick.call_args[0][0] == "!configured:server"
