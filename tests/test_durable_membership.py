"""The durable local membership command, driven directly with a stand-in nio session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from nio.durable import DurableSync

from mindroom.event_journal import RoomMembershipPosition
from mindroom.matrix.durable_membership import change_local_membership
from tests.conftest import SETTLE_TIMEOUT_SECONDS

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
    ("target", "position", "ready", "expected"),
    [
        ("join", None, False, [f"wait {ROOM}", f"subscribe {ROOM}", f"wait {ROOM}"]),
        ("join", RoomMembershipPosition("join", 0), False, [f"wait {ROOM}", f"subscribe {ROOM}", f"wait {ROOM}"]),
        ("join", None, True, [f"wait {ROOM}"]),
        ("join", RoomMembershipPosition("join", 0), True, [f"wait {ROOM}"]),
        ("leave", RoomMembershipPosition("join", 0), False, []),
    ],
    ids=["sent-join", "reported-join", "sent-join-sendable", "reported-join-sendable", "leave"],
)
async def test_a_join_subscribes_its_room_only_when_nio_cannot_send_to_it_yet(
    target: str,
    position: RoomMembershipPosition | None,
    ready: bool,
    expected: list[str],
) -> None:
    """Sliding Sync may reach a joined room through neither its list nor a subscription, and a ready room needs none."""
    session = _session(ready=True)
    steps: list[str] = []
    room_ready = asyncio.Event()
    if ready:
        room_ready.set()

    async def subscribe(room_id: str) -> None:
        steps.append(f"subscribe {room_id}")
        room_ready.set()

    async def wait_for_room_ready(room_id: str) -> bool:
        steps.append(f"wait {room_id}")
        await room_ready.wait()
        return True

    session.wait_for_room_ready.side_effect = wait_for_room_ready

    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        assert await _change(session, target, position, subscribe) is True
    assert steps == expected
