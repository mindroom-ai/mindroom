"""Tests for room membership management."""

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.room_manager import audit_and_fix_room_memberships


class TestRoomManager:
    """Test room membership management functionality."""

    @pytest.mark.asyncio
    async def test_audit_removes_unconfigured_bots(self):
        """Test that audit removes bots from rooms when they're not configured."""

        # Mock the config to have only one agent configured
        mock_config = MagicMock()
        mock_config.agents = {"configured_agent": MagicMock(rooms=["room1", "room2"])}
        mock_config.teams = {}

        with (
            patch("mindroom.room_manager.load_config", return_value=mock_config),
            patch("mindroom.room_manager.get_all_existing_mindroom_users") as mock_get_users,
        ):
            # Set up mock clients for two users - one configured, one orphaned
            configured_client = AsyncMock()
            orphaned_client = AsyncMock()

            # Configured agent is in correct rooms
            configured_rooms_response = MagicMock(spec=nio.JoinedRoomsResponse)
            configured_rooms_response.rooms = ["room1", "room2"]
            configured_client.joined_rooms.return_value = configured_rooms_response

            # Orphaned agent is in rooms it shouldn't be
            orphaned_rooms_response = MagicMock(spec=nio.JoinedRoomsResponse)
            orphaned_rooms_response.rooms = ["room1", "room2", "room3"]
            orphaned_client.joined_rooms.return_value = orphaned_rooms_response

            # Mock room_leave responses
            leave_response = MagicMock(spec=nio.RoomLeaveResponse)
            orphaned_client.room_leave.return_value = leave_response

            mock_get_users.return_value = {
                "@mindroom_configured_agent:localhost": configured_client,
                "@mindroom_orphaned_agent:localhost": orphaned_client,
            }

            # Run the audit
            report = await audit_and_fix_room_memberships("http://localhost:8008")

            # Check that orphaned bot was removed from all rooms
            assert orphaned_client.room_leave.call_count == 3
            orphaned_client.room_leave.assert_any_call("room1")
            orphaned_client.room_leave.assert_any_call("room2")
            orphaned_client.room_leave.assert_any_call("room3")

            # Check that configured bot wasn't touched
            configured_client.room_leave.assert_not_called()

            # Check the report
            assert len(report["removed"]) == 3
            assert len(report["errors"]) == 0
            assert len(report["checked"]) == 2

    @pytest.mark.asyncio
    async def test_audit_handles_router_specially(self):
        """Test that router is expected to be in all configured rooms."""

        # Mock config with multiple agents in different rooms
        mock_config = MagicMock()
        mock_config.agents = {
            "agent1": MagicMock(rooms=["room1", "room2"]),
            "agent2": MagicMock(rooms=["room2", "room3"]),
        }
        mock_config.teams = {"team1": MagicMock(rooms=["room4"])}

        with (
            patch("mindroom.room_manager.load_config", return_value=mock_config),
            patch("mindroom.room_manager.get_all_existing_mindroom_users") as mock_get_users,
        ):
            # Set up mock router client
            router_client = AsyncMock()

            # Router is in all rooms plus an extra one
            router_rooms_response = MagicMock(spec=nio.JoinedRoomsResponse)
            router_rooms_response.rooms = ["room1", "room2", "room3", "room4", "room5"]
            router_client.joined_rooms.return_value = router_rooms_response

            # Mock room_leave response
            leave_response = MagicMock(spec=nio.RoomLeaveResponse)
            router_client.room_leave.return_value = leave_response

            mock_get_users.return_value = {
                "@mindroom_router:localhost": router_client,
            }

            # Run the audit
            report = await audit_and_fix_room_memberships("http://localhost:8008")

            # Router should only be removed from room5 (not configured)
            assert router_client.room_leave.call_count == 1
            router_client.room_leave.assert_called_with("room5")

            # Check the report
            assert len(report["removed"]) == 1
            assert "room5" in report["removed"][0]

    @pytest.mark.asyncio
    async def test_audit_handles_errors_gracefully(self):
        """Test that audit continues even when some operations fail."""

        mock_config = MagicMock()
        mock_config.agents = {}
        mock_config.teams = {}

        with (
            patch("mindroom.room_manager.load_config", return_value=mock_config),
            patch("mindroom.room_manager.get_all_existing_mindroom_users") as mock_get_users,
        ):
            # Set up a client that will fail
            failing_client = AsyncMock()

            # Make joined_rooms fail
            failing_client.joined_rooms.side_effect = Exception("Connection error")

            mock_get_users.return_value = {
                "@mindroom_failing:localhost": failing_client,
            }

            # Run the audit - should not raise
            report = await audit_and_fix_room_memberships("http://localhost:8008")

            # Check that error was recorded
            assert len(report["errors"]) == 1
            assert "Connection error" in report["errors"][0]
            assert len(report["checked"]) == 1
