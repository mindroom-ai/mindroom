"""Tests for DM room detection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import nio
import pytest

from mindroom.matrix import rooms
from mindroom.matrix.rooms import is_dm_room


def _direct_rooms(rooms_by_user: dict[str, list[str]], status: int = 200) -> nio.DirectRoomsResponse:
    """Build the m.direct response nio returns for an HTTP reply with *status*."""
    response = nio.DirectRoomsResponse(rooms_by_user)
    response.transport_response = MagicMock(spec=aiohttp.ClientResponse, status=status)
    return response


def _non_dm_state() -> nio.RoomGetStateResponse:
    """Build a room state reply whose member event carries no is_direct flag."""
    return nio.RoomGetStateResponse([{"type": "m.room.member", "content": {"membership": "join"}}], "!room:server")


_M_DIRECT_READ_FAILED = nio.DirectRoomsErrorResponse("Internal server error", "M_UNKNOWN")


def _failing_m_direct_client(
    failed_read: nio.DirectRoomsResponse | nio.DirectRoomsErrorResponse = _M_DIRECT_READ_FAILED,
    room_id: str = "!room:server",
) -> AsyncMock:
    """Build a client whose first m.direct read fails and whose next lists *room_id*, which its state does not mark."""
    client = AsyncMock()
    client.user_id = "@agent:server"
    client.rooms = {}
    client.list_direct_rooms.side_effect = [failed_read, _direct_rooms({"@user:server": [room_id]})]
    client.room_get_state.return_value = _non_dm_state()
    return client


@pytest.mark.asyncio
class TestDMDetection:
    """Test DM room detection functionality."""

    def setup_method(self) -> None:
        """Clear the cache before each test."""
        rooms._DM_ROOM_CACHE.clear()
        rooms._DIRECT_ROOMS_CACHE.clear()

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
        client.list_direct_rooms.return_value = _direct_rooms(
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
        dm_client.list_direct_rooms.return_value = _direct_rooms(
            {"@user:server": [room_id]},
        )

        non_dm_client = AsyncMock()
        non_dm_client.user_id = "@agent_b:server"
        non_dm_client.list_direct_rooms.return_value = _direct_rooms({"@user:server": []})
        non_dm_client.room_get_state.return_value = _non_dm_state()

        dm_result = await is_dm_room(dm_client, room_id)
        non_dm_result = await is_dm_room(non_dm_client, room_id)

        assert dm_result is True
        assert non_dm_result is False

    async def test_failed_state_read_answers_unknown_and_reads_again(self) -> None:
        """Test a failed state read after a clean m.direct read answers unknown, and the next call reads state again."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.return_value = nio.DirectRoomsErrorResponse("No direct rooms", "M_NOT_FOUND")

        error_response = MagicMock(spec=nio.RoomGetStateError)
        client.room_get_state.side_effect = [error_response, _non_dm_state()]

        first_result = await is_dm_room(client, "!room:server")
        second_result = await is_dm_room(client, "!room:server")

        assert first_result is None
        assert second_result is False
        assert client.room_get_state.call_count == 2

    async def test_caches_empty_m_direct_results_for_not_found(self) -> None:
        """Test M_NOT_FOUND m.direct responses are cached as empty results."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.return_value = nio.DirectRoomsErrorResponse("No direct rooms", "M_NOT_FOUND")

        client.room_get_state.return_value = _non_dm_state()

        first_result = await is_dm_room(client, "!room1:server")
        second_result = await is_dm_room(client, "!room2:server")
        repeated_result = await is_dm_room(client, "!room1:server")

        assert first_result is False
        assert second_result is False
        assert repeated_result is False
        assert client.list_direct_rooms.call_count == 1
        assert client.room_get_state.call_count == 2

    @pytest.mark.parametrize(
        "failed_read",
        [
            pytest.param(_M_DIRECT_READ_FAILED, id="error-response"),
            # nio parses a non-JSON error body, such as a proxy's 502 page, as an empty m.direct list.
            pytest.param(_direct_rooms({}, status=502), id="non-json-error-body"),
        ],
    )
    async def test_failed_m_direct_read_answers_unknown_and_reads_again(
        self,
        failed_read: nio.DirectRoomsResponse | nio.DirectRoomsErrorResponse,
    ) -> None:
        """Test a failed m.direct read answers unknown for an unmarked room, and the next call reads m.direct again."""
        client = _failing_m_direct_client(failed_read)

        first_result = await is_dm_room(client, "!room:server")
        second_result = await is_dm_room(client, "!room:server")

        assert first_result is None
        assert second_result is True
        assert client.list_direct_rooms.call_count == 2

    @pytest.mark.parametrize(
        ("state_reply", "expected"),
        [
            pytest.param(MagicMock(spec=nio.RoomGetStateError), None, id="state-read-fails"),
            pytest.param(
                nio.RoomGetStateResponse(
                    [{"type": "m.room.member", "content": {"membership": "join", "is_direct": True}}],
                    "!room:server",
                ),
                True,
                id="state-marks-direct",
            ),
        ],
    )
    async def test_failed_m_direct_read_answers_from_state_only_when_it_marks_direct(
        self,
        state_reply: nio.RoomGetStateResponse | nio.RoomGetStateError,
        expected: bool | None,
    ) -> None:
        """Test that after a failed m.direct read only an is_direct marker in the state gives a definite answer."""
        client = _failing_m_direct_client()
        client.room_get_state.return_value = state_reply

        assert await is_dm_room(client, "!room:server") is expected

    async def test_dm_cache_expires_and_rechecks_m_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test stale DM cache entries are refreshed after TTL."""
        client = AsyncMock()
        client.user_id = "@agent:server"
        client.list_direct_rooms.side_effect = [
            _direct_rooms({"@user:server": ["!room:server"]}),
            _direct_rooms({"@user:server": []}),
        ]

        client.room_get_state.return_value = _non_dm_state()

        monkeypatch.setattr(rooms, "_DM_ROOM_TTL", 0)
        monkeypatch.setattr(rooms, "_DIRECT_ROOMS_TTL", 0)

        first_result = await is_dm_room(client, "!room:server")
        second_result = await is_dm_room(client, "!room:server")

        assert first_result is True
        assert second_result is False
        assert client.list_direct_rooms.call_count == 2

    async def test_filter_non_dm_rooms_preserves_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test non-DM room filtering keeps only rooms known not to be DMs, in input order."""
        client = AsyncMock()
        answers = {"!dm1:server": True, "!dm2:server": True, "!unknown:server": None}

        async def mock_is_dm_room(_client: AsyncMock, room_id: str) -> bool | None:
            return answers.get(room_id, False)

        monkeypatch.setattr(rooms, "is_dm_room", mock_is_dm_room)

        result = await rooms.filter_non_dm_rooms(
            client,
            ["!room1:server", "!dm1:server", "!unknown:server", "!room2:server", "!dm2:server"],
        )

        assert result == ["!room1:server", "!room2:server"]

    async def test_leave_non_dm_rooms_keeps_room_whose_m_direct_read_failed(self) -> None:
        """Test a failed m.direct read keeps its room, a clean read lets a non-DM go, and a later pass keeps the DM."""
        client = _failing_m_direct_client(room_id="!dm:server")
        leave = AsyncMock(return_value=True)

        await rooms.leave_non_dm_rooms(client, ["!dm:server", "!group:server"], leave_room_action=leave)
        await rooms.leave_non_dm_rooms(client, ["!dm:server"], leave_room_action=leave)

        leave.assert_awaited_once_with("!group:server")
