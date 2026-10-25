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
    subscribe: Callable[[], Awaitable[None]],
) -> bool:
    return await change_local_membership(
        session,
        account_id="@bot:localhost",
        room_id=ROOM,
        target_membership=target,
        read_position=AsyncMock(return_value=position),
        admission_progress=asyncio.Event(),
        subscribe_awaited_rooms=subscribe,
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
        ("join", None, ["subscribe", "observed", "join", "subscribe", "ready"]),
        ("join", RoomMembershipPosition("join", 0), ["subscribe", "observed", "subscribe", "ready"]),
        ("leave", RoomMembershipPosition("join", 0), ["subscribe", "observed", "leave"]),
    ],
    ids=["sent-join", "reported-join", "leave"],
)
async def test_each_wait_for_a_sync_follows_a_subscription_of_the_rooms_nio_awaits(
    target: str,
    position: RoomMembershipPosition | None,
    expected: list[str],
) -> None:
    """The previous command's observation and a join's room state both need a sync that reports the room."""
    session = _session(ready=True)
    steps: list[str] = []

    async def subscribe() -> None:
        steps.append("subscribe")

    def record(step: str, result: object) -> Callable[..., Awaitable[object]]:
        async def side_effect(*_args: object, **_kwargs: object) -> object:
            steps.append(step)
            return result

        return side_effect

    session.wait_for_membership_idle.side_effect = record("observed", None)
    session.change_membership.side_effect = record(target, True)
    session.wait_for_room_ready.side_effect = record("ready", True)

    assert await _change(session, target, position, subscribe) is True
    assert steps == expected
