"""One departure fences once, however many times it is reported."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mindroom.event_journal import MembershipFence

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"


@dataclass
class FakeStore:
    """A store that records which rooms were asked to invalidate."""

    advanced: list[str] = field(default_factory=list)
    epochs: dict[str, int] = field(default_factory=dict)

    async def advance_membership_epoch(self, room_id: str) -> int:
        """Record one invalidation and return the room's new epoch."""
        self.advanced.append(room_id)
        self.epochs[room_id] = self.epochs.get(room_id, 0) + 1
        return self.epochs[room_id]


def fence() -> tuple[MembershipFence, FakeStore]:
    """Return a fence and the store behind it."""
    store = FakeStore()
    return MembershipFence(store=store), store


async def test_a_local_departure_fences_immediately() -> None:
    """A local departure fences immediately, without waiting for sync."""
    membership, store = fence()

    await membership.fence_local_departure(ROOM)

    assert store.advanced == [ROOM]


async def test_a_sync_reported_departure_fences() -> None:
    """A departure the bot did not initiate still fences."""
    membership, store = fence()

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_the_echo_of_a_local_departure_does_not_fence_again() -> None:
    """The sync report of a local leave is the same departure, not a second one."""
    membership, store = fence()

    await membership.fence_local_departure(ROOM)
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_a_rejoin_before_the_echo_keeps_its_projection() -> None:
    """The echo must not delete a conversation hydrated under the new membership.

    Nothing here tells the fence about the rejoin, which is the point: the
    departure was already accounted for, so no later report of it can fence
    again regardless of what happened in between.
    """
    membership, store = fence()

    await membership.fence_local_departure(ROOM)
    # ... the bot rejoins and hydrates the room again ...
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_a_second_departure_after_the_echo_fences_again() -> None:
    """Absorbing one echo must not deafen the fence to the next real departure."""
    membership, store = fence()

    await membership.fence_local_departure(ROOM)
    await membership.fence_reported_departures([ROOM])
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_an_echo_absorbs_only_its_own_room() -> None:
    """An echo absorbs only its own room."""
    membership, store = fence()

    await membership.fence_local_departure(ROOM)
    await membership.fence_reported_departures([ROOM, OTHER_ROOM])

    assert store.advanced == [ROOM, OTHER_ROOM]


async def test_no_departures_asks_for_nothing() -> None:
    """An ordinary sync response fences nothing."""
    membership, store = fence()

    await membership.fence_reported_departures([])

    assert store.advanced == []
