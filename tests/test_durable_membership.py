"""The durable local membership command, driven directly with a stand-in nio session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from nio.durable import DurableSync

from mindroom.event_journal import RoomMembershipPosition
from mindroom.matrix.durable_membership import change_local_membership

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ROOM = "!room:localhost"


def _session(*, ready: bool) -> Mock:
    session = create_autospec(DurableSync, instance=True)
    session.next_batch.return_value = None
    session.change_membership.return_value = True
    session.wait_for_room_ready.return_value = ready
    return session


async def _change(
    session: Mock,
    target: str,
    position: RoomMembershipPosition | None,
    subscribe: Callable[[str], Awaitable[None]],
) -> bool:
    return await change_local_membership(
        session,
        account_id="@bot:localhost",
        room_id=ROOM,
        target_membership=target,
        read_position=AsyncMock(return_value=position),
        admission_progress=asyncio.Event(),
        subscribe_joined_room=subscribe,
    )


@pytest.mark.asyncio
async def test_a_join_whose_membership_ends_before_nio_accepts_sends_fails() -> None:
    """A room the bot leaves again before nio can send to it is not joined."""
    session = _session(ready=False)

    assert await _change(session, "join", RoomMembershipPosition("join", 0), AsyncMock()) is False
    session.wait_for_room_ready.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "position", "expected"),
    [
        ("join", None, [f"subscribe {ROOM}", f"wait {ROOM}"]),
        ("join", RoomMembershipPosition("join", 0), [f"wait {ROOM}"]),
        ("leave", RoomMembershipPosition("join", 0), []),
    ],
    ids=["sent-join", "reported-join", "leave"],
)
async def test_a_sent_join_subscribes_its_room_before_waiting_for_it(
    target: str,
    position: RoomMembershipPosition | None,
    expected: list[str],
) -> None:
    """Only a join this command sends may target a room outside the source's room list and subscriptions."""
    session = _session(ready=True)
    steps: list[str] = []

    async def subscribe(room_id: str) -> None:
        steps.append(f"subscribe {room_id}")

    async def wait_for_room_ready(room_id: str) -> bool:
        steps.append(f"wait {room_id}")
        return True

    session.wait_for_room_ready.side_effect = wait_for_room_ready

    assert await _change(session, target, position, subscribe) is True
    assert steps == expected
