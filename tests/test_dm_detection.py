"""Tests for DM room detection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix import rooms
from mindroom.matrix.rooms import is_dm_room


@pytest.mark.asyncio
class TestDMDetection:
    """Test DM room detection functionality."""

    def setup_method(self) -> None:
        """Clear the cache before each test."""
        rooms.DM_ROOM_CACHE.clear()
        rooms.DIRECT_ROOMS_CACHE.clear()

    async def test_detects_dm_room_with_is_direct_flag(self) -> None:
        """Test that a room with is_direct=true in member state is detected as DM."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        # m.direct returns nothing so we fall through to state events
        client.list_direct_rooms.return_value = nio.DirectRoomsErrorResponse(
            "No direct rooms",
            "M_NOT_FOUND",
        )

        # Mock response with a member event that has is_direct=true
        mock_response = MagicMock(spec=nio.RoomGetStateResponse)
        mock_response.events = [
            {
                "type": "m.room.member",
                "content": {
                    "membership": "join",
                    "displayname": "User",
                    "is_direct": True,  # This marks it as a DM
                },
            },
            {
                "type": "m.room.create",
                "content": {"creator": "@user:server"},
            },
        ]

        client.room_get_state.return_value = mock_response

        result = await is_dm_room(client, "!room:server")

        assert result is True
        client.room_get_state.assert_called_once_with("!room:server")

    async def test_detects_dm_room_from_m_direct_account_data(self) -> None:
        """Test that rooms in m.direct account data are detected as DMs."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.return_value = nio.DirectRoomsResponse(
            {"@user:server": ["!room:server"]},
        )

        result = await is_dm_room(client, "!room:server")

        assert result is True
        client.room_get_state.assert_not_called()

    async def test_detects_dm_room_from_two_member_group_fallback(self) -> None:
        """Test unnamed 2-member rooms are treated as DM when metadata is missing."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.return_value = nio.DirectRoomsErrorResponse(
            "No direct rooms",
            "M_NOT_FOUND",
        )

        room = nio.MatrixRoom("!room:server", "@agent:server")
        room.users = {
            "@agent:server": MagicMock(),
            "@user:server": MagicMock(),
        }
        client.rooms = {"!room:server": room}

        result = await is_dm_room(client, "!room:server")

        assert result is True
        client.room_get_state.assert_not_called()

    async def test_detects_non_dm_room(self) -> None:
        """Test that a room without is_direct flag is not detected as DM."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.return_value = nio.DirectRoomsErrorResponse(
            "No direct rooms",
            "M_NOT_FOUND",
        )

        # Mock response with member events but no is_direct flag
        mock_response = MagicMock(spec=nio.RoomGetStateResponse)
        mock_response.events = [
            {
                "type": "m.room.member",
                "content": {
                    "membership": "join",
                    "displayname": "User",
                    # No is_direct flag
                },
            },
            {
                "type": "m.room.create",
                "content": {"creator": "@user:server"},
            },
        ]

        client.room_get_state.return_value = mock_response

        result = await is_dm_room(client, "!room:server")

        assert result is False

    async def test_room_cache_is_scoped_per_user(self) -> None:
        """Test DM cache does not leak between different clients/users."""
        room_id = "!room:server"

        dm_client = AsyncMock()
        dm_client.user_id = "@agent_a:server"
        dm_client.list_direct_rooms.return_value = nio.DirectRoomsResponse(
            {"@user:server": [room_id]},
        )

        non_dm_client = AsyncMock()
        non_dm_client.user_id = "@agent_b:server"
        non_dm_client.list_direct_rooms.return_value = nio.DirectRoomsResponse({"@user:server": []})
        non_dm_state_response = MagicMock(spec=nio.RoomGetStateResponse)
        non_dm_state_response.events = [
            {"type": "m.room.member", "content": {"membership": "join"}},
        ]
        non_dm_client.room_get_state.return_value = non_dm_state_response

        dm_result = await is_dm_room(dm_client, room_id)
        non_dm_result = await is_dm_room(non_dm_client, room_id)

        assert dm_result is True
        assert non_dm_result is False

    async def test_does_not_cache_false_when_state_request_errors(self) -> None:
        """Test API errors don't poison cache with a false negative."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.return_value = nio.DirectRoomsErrorResponse("No direct rooms", "M_NOT_FOUND")

        error_response = MagicMock(spec=nio.RoomGetStateError)
        non_dm_state_response = MagicMock(spec=nio.RoomGetStateResponse)
        non_dm_state_response.events = [
            {"type": "m.room.member", "content": {"membership": "join"}},
        ]
        client.room_get_state.side_effect = [error_response, non_dm_state_response]

        first_result = await is_dm_room(client, "!room:server")
        second_result = await is_dm_room(client, "!room:server")

        assert first_result is False
        assert second_result is False
        assert client.room_get_state.call_count == 2
