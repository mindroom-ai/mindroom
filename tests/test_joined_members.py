"""The member read every joined-members reader shares."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.durable.outbound import OutboundCrypto

from mindroom.matrix.joined_members import read_joined_members
from tests.access_schema_support import joined_members_response

if TYPE_CHECKING:
    from nio.durable import DurableSync

ROOM = "!room:example.org"


@pytest.mark.asyncio
async def test_the_durable_clients_refusal_for_a_membership_change_is_read_again() -> None:
    """The durable client's own refusal, for a member change its sync applied during the read, makes one more read.

    This runs mindroom-nio's real member read, so a reworded refusal fails here instead of ending every re-read.
    """
    requests: list[str] = []

    async def request(_method: str, path: str) -> str:
        requests.append(path)
        if len(requests) == 1:
            # A sync applies a member change while the first read is in flight.
            outbound.member_cache.pop(ROOM, None)
        return json.dumps({"joined": {"@alice:example.org": {"display_name": "Alice", "avatar_url": None}}})

    session = SimpleNamespace(
        client=SimpleNamespace(encrypted_rooms=set(), rooms={}, invalidate_outbound_session=lambda _room_id: None),
        _store=SimpleNamespace(transaction=contextlib.nullcontext),
        _crypto=SimpleNamespace(olm=SimpleNamespace(wedged_devices=[], key_request_devices_no_session=[])),
        _crypto_lock=asyncio.Lock(),
        _assert_active=lambda: None,
        _transport=SimpleNamespace(request=request),
    )
    outbound = OutboundCrypto(cast("DurableSync", session))
    client = MagicMock(spec=nio.AsyncClient)
    client.joined_members = outbound.joined_members

    response = await read_joined_members(client, ROOM)

    assert isinstance(response, nio.JoinedMembersResponse)
    assert [member.user_id for member in response.members] == ["@alice:example.org"]
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("refusals", [1, 2, 3])
async def test_a_member_read_refused_for_a_membership_change_is_read_again_3_times_at_most(refusals: int) -> None:
    """Each refusal for a membership change is read again, until the third read, whose refusal raises."""
    answer = joined_members_response(ROOM, "@alice:example.org")
    client = MagicMock(spec=nio.AsyncClient)
    client.joined_members = AsyncMock(
        side_effect=[
            *(nio.LocalProtocolError("room membership changed during lookup") for _ in range(refusals)),
            answer,
        ],
    )
    if refusals < 3:
        assert await read_joined_members(client, ROOM) is answer
    else:
        with pytest.raises(nio.LocalProtocolError, match="room membership changed during lookup"):
            await read_joined_members(client, ROOM)
    assert client.joined_members.await_count == min(refusals + 1, 3)


@pytest.mark.asyncio
async def test_any_other_member_read_failure_raises_without_reading_again() -> None:
    """Only the refusal for a membership change is read again; another refusal raises at once."""
    client = MagicMock(spec=nio.AsyncClient)
    client.joined_members = AsyncMock(side_effect=nio.LocalProtocolError("durable sync session is closed"))
    with pytest.raises(nio.LocalProtocolError, match="durable sync session is closed"):
        await read_joined_members(client, ROOM)
    client.joined_members.assert_awaited_once_with(ROOM)
