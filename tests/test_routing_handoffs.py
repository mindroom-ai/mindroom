"""The routing lane's rows: one owner per room fenced by a generation, and one durable handoff per routed turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    AdmissionResult,
    EventClass,
    EventKind,
    InboundEvent,
    RoutingHandoff,
    StaleLaneOwnerError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore

_ROOM = "!room:localhost"


def _handoff(handoff_id: str = "h1", sources: tuple[str, ...] = ("$e1",), recipient: str = "beta") -> RoutingHandoff:
    return RoutingHandoff(handoff_id, _ROOM, sources, recipient, {"events": list(sources)} if recipient else None)


def _event(handoff: RoutingHandoff) -> InboundEvent:
    assert handoff.payload is not None
    return InboundEvent(
        event_id=handoff.handoff_id,
        room_id=handoff.room_id,
        thread_id=None,
        kind=EventKind.ROUTED_TURN,
        event_class=EventClass.ACTIONABLE,
        sender="@user:localhost",
        origin_server_ts=1,
        source=handoff.payload,
    )


@pytest.mark.asyncio
async def test_lane_generation_grows_only_when_the_owner_changes(journal_store: EventJournalStore) -> None:
    """Holding the lane again keeps its generation; another bot taking it makes a new one, per room."""
    assert await journal_store.hold_routing_lane(_ROOM, "alpha") == 1
    assert await journal_store.hold_routing_lane(_ROOM, "alpha") == 1
    assert await journal_store.hold_routing_lane(_ROOM, "beta") == 2
    assert await journal_store.hold_routing_lane(_ROOM, "alpha") == 3
    assert await journal_store.hold_routing_lane("!other:localhost", "beta") == 1


@pytest.mark.asyncio
async def test_stale_lane_owner_cannot_save_a_handoff(journal_store: EventJournalStore) -> None:
    """A bot that lost the lane after it decided saves nothing; the bot that holds it saves."""
    stale = await journal_store.hold_routing_lane(_ROOM, "alpha")
    current = await journal_store.hold_routing_lane(_ROOM, "beta")
    with pytest.raises(StaleLaneOwnerError):
        await journal_store.save_routing_handoff(_handoff(), owner="alpha", generation=stale)
    assert await journal_store.routing_handoffs(_ROOM, ["$e1"]) == ()
    await journal_store.save_routing_handoff(_handoff(), owner="beta", generation=current)
    assert await journal_store.routing_handoffs(_ROOM, ["$e1"]) == (_handoff(),)


@pytest.mark.asyncio
async def test_handoff_survives_reopen_and_is_found_by_any_of_its_sources(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """A restart reads the saved handoff back, whichever of its sources the replayed turn holds."""
    store = journal_database()
    generation = await store.hold_routing_lane(_ROOM, "alpha")
    handoff = _handoff(sources=("$e1", "$e2"))
    await store.save_routing_handoff(handoff, owner="alpha", generation=generation)
    await store.close()
    reopened = journal_database()
    assert await reopened.routing_handoffs(_ROOM, ["$e2", "$e3"]) == (handoff,)
    assert await reopened.routing_handoffs("!other:localhost", ["$e2"]) == ()


@pytest.mark.asyncio
async def test_a_source_belongs_to_one_handoff(journal_store: EventJournalStore) -> None:
    """A second handoff naming an already handed-off source saves nothing, also for its other sources."""
    generation = await journal_store.hold_routing_lane(_ROOM, "alpha")
    await journal_store.save_routing_handoff(_handoff(), owner="alpha", generation=generation)
    with pytest.raises(ValueError, match="already belongs"):
        await journal_store.save_routing_handoff(
            _handoff("h2", ("$e0", "$e1")),
            owner="alpha",
            generation=generation,
        )
    assert await journal_store.routing_handoffs(_ROOM, ["$e0"]) == (_handoff(),)
    await journal_store.save_routing_handoff(_handoff("h3", ("$e0",)), owner="alpha", generation=generation)


@pytest.mark.asyncio
async def test_pending_handoffs_of_the_room_are_listed_until_accepted(journal_store: EventJournalStore) -> None:
    """A successor finds a handoff no recipient holds without knowing its sources; acceptance releases the payload."""
    generation = await journal_store.hold_routing_lane(_ROOM, "alpha")
    handoff = _handoff()
    await journal_store.save_routing_handoff(handoff, owner="alpha", generation=generation)
    assert await journal_store.routing_handoffs(_ROOM, []) == (handoff,)

    recipient = journal_store.principal("@mindroom_beta:localhost")
    await recipient.accept_routing_handoff(_event(handoff))
    assert await journal_store.routing_handoffs(_ROOM, []) == ()
    accepted = RoutingHandoff("h1", _ROOM, ("$e1",), "beta", None)
    assert await journal_store.routing_handoffs(_ROOM, ["$e1"]) == (accepted,)
    assert await recipient.is_pending("h1")
    assert (await recipient.load_event("h1")).source == handoff.payload


@pytest.mark.asyncio
async def test_accepting_a_handoff_again_enqueues_nothing(journal_store: EventJournalStore) -> None:
    """The recipient's journal holds one event per handoff ID, also once that event settled."""
    generation = await journal_store.hold_routing_lane(_ROOM, "alpha")
    handoff = _handoff()
    await journal_store.save_routing_handoff(handoff, owner="alpha", generation=generation)
    recipient = journal_store.principal("@mindroom_beta:localhost")
    await recipient.accept_routing_handoff(_event(handoff))
    await recipient.settle_many(("h1",))
    await recipient.accept_routing_handoff(_event(handoff))
    assert not await recipient.is_pending("h1")
    assert await recipient.admit(_event(handoff)) is AdmissionResult.DUPLICATE


@pytest.mark.asyncio
async def test_no_fit_is_saved_without_a_payload(journal_store: EventJournalStore) -> None:
    """A turn nobody fits is a handoff to nobody: saved, never pending, and found again by its sources."""
    generation = await journal_store.hold_routing_lane(_ROOM, "alpha")
    no_fit = _handoff(recipient="")
    await journal_store.save_routing_handoff(no_fit, owner="alpha", generation=generation)
    assert await journal_store.routing_handoffs(_ROOM, []) == ()
    assert await journal_store.routing_handoffs(_ROOM, ["$e1"]) == (no_fit,)
