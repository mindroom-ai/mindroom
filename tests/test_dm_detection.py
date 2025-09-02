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

    async def test_detects_dm_room_with_is_direct_flag(self) -> None:
        """Test that a room with is_direct=true in member state is detected as DM."""
        client = AsyncMock()

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

    async def test_detects_non_dm_room(self) -> None:
        """Test that a room without is_direct flag is not detected as DM."""
        client = AsyncMock()

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

    async def test_handles_api_errors(self) -> None:
        """Test that API errors return False."""
        client = AsyncMock()

        # Mock an error response
        client.room_get_state.return_value = MagicMock(spec=nio.RoomGetStateError)

        result = await is_dm_room(client, "!room:server")

        # Should return False when we get an error response
        assert result is False
