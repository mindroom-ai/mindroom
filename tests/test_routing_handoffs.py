"""The routing lane's rows: one owner per room fenced by a generation, and one durable handoff per routed turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    ROUTING_HANDOFF_CLAIM,
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
_OWNER = "alpha@@mindroom_alpha:localhost"
_RECIPIENT = "beta@@mindroom_beta:localhost"


def _handoff(
    handoff_id: str = "h1",
    sources: tuple[str, ...] = ("$e1",),
    recipient: str = _RECIPIENT,
) -> RoutingHandoff:
    return RoutingHandoff(handoff_id, _ROOM, sources, recipient, {"events": list(sources)} if recipient else None)


def _message(event_id: str) -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        room_id=_ROOM,
        thread_id=None,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender="@user:localhost",
        origin_server_ts=1,
        source={"event_id": event_id},
    )


async def _handed_off(store: EventJournalStore, *sources: str) -> dict[str, str]:
    """Return the handoff each of these room events belongs to."""
    return await store.room_event_claims(_ROOM, sources, ROUTING_HANDOFF_CLAIM)


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
    assert await _handed_off(journal_store, "$e1") == {}
    assert await journal_store.accept_routing_handoffs(_ROOM) == ()
    await journal_store.save_routing_handoff(_handoff(), owner="beta", generation=current)
    assert await _handed_off(journal_store, "$e1") == {"$e1": "h1"}


@pytest.mark.asyncio
async def test_a_source_belongs_to_one_handoff(journal_store: EventJournalStore) -> None:
    """A second handoff naming an already handed-off source saves nothing, also for its other sources."""
    generation = await journal_store.hold_routing_lane(_ROOM, _OWNER)
    await journal_store.save_routing_handoff(_handoff(), owner=_OWNER, generation=generation)
    with pytest.raises(ValueError, match="already belongs"):
        await journal_store.save_routing_handoff(_handoff("h2", ("$e0", "$e1")), owner=_OWNER, generation=generation)
    assert await _handed_off(journal_store, "$e0", "$e1") == {"$e1": "h1"}
    await journal_store.save_routing_handoff(_handoff("h3", ("$e0",)), owner=_OWNER, generation=generation)
    assert await _handed_off(journal_store, "$e0", "$e1") == {"$e0": "h3", "$e1": "h1"}


@pytest.mark.asyncio
async def test_acceptance_moves_the_turn_from_the_owners_journal_to_the_recipients(
    journal_store: EventJournalStore,
) -> None:
    """Until acceptance the lane owner's events stay pending; acceptance settles them as the recipient gets the turn."""
    owner, recipient = journal_store.principal(_OWNER), journal_store.principal(_RECIPIENT)
    await owner.admit(_message("$e1"))
    generation = await journal_store.hold_routing_lane(_ROOM, _OWNER)
    handoff = _handoff()
    await journal_store.save_routing_handoff(handoff, owner=_OWNER, generation=generation)
    assert await owner.is_pending("$e1")
    assert await recipient.load_event("h1") is None
    assert await journal_store.accept_routing_handoffs("!other:localhost") == ()

    assert await journal_store.accept_routing_handoffs(_ROOM) == (_RECIPIENT,)

    assert not await owner.is_pending("$e1")
    routed = await recipient.load_event("h1")
    assert routed is not None
    assert (routed.kind, routed.source, routed.sender) == (
        EventKind.ROUTED_TURN,
        handoff.payload,
        "@mindroom_alpha:localhost",
    )
    assert await recipient.is_pending("h1")


@pytest.mark.asyncio
async def test_accepting_a_handoff_again_enqueues_nothing(journal_store: EventJournalStore) -> None:
    """The recipient's journal holds one event per handoff, also once that event settled."""
    generation = await journal_store.hold_routing_lane(_ROOM, _OWNER)
    await journal_store.save_routing_handoff(_handoff(), owner=_OWNER, generation=generation)
    recipient = journal_store.principal(_RECIPIENT)
    await journal_store.accept_routing_handoffs(_ROOM)
    await recipient.settle_many(("h1",))

    assert await journal_store.accept_routing_handoffs(_ROOM) == ()
    assert await journal_store.accept_routing_handoffs() == ()
    assert not await recipient.is_pending("h1")


@pytest.mark.asyncio
async def test_a_restart_accepts_every_handoff_no_recipient_holds(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """A handoff saved before a crash reaches its recipient from the row alone, in whichever room it waits."""
    store = journal_database()
    generation = await store.hold_routing_lane(_ROOM, _OWNER)
    await store.save_routing_handoff(_handoff("h1", ("$e1",)), owner=_OWNER, generation=generation)
    elsewhere = await store.hold_routing_lane("!other:localhost", _OWNER)
    other = RoutingHandoff("h2", "!other:localhost", ("$e2",), _RECIPIENT, {"events": ["$e2"]})
    await store.save_routing_handoff(other, owner=_OWNER, generation=elsewhere)
    await store.close()

    reopened = journal_database()
    assert await reopened.accept_routing_handoffs() == (_RECIPIENT, _RECIPIENT)
    recipient = reopened.principal(_RECIPIENT)
    assert await recipient.is_pending("h1")
    assert await recipient.is_pending("h2")
    assert await _handed_off(reopened, "$e1") == {"$e1": "h1"}


@pytest.mark.asyncio
async def test_no_fit_is_saved_without_a_payload_and_settles_the_owners_events(
    journal_store: EventJournalStore,
) -> None:
    """A turn nobody fits is a handoff to nobody: saved, never waiting, found again by its sources, and settled."""
    owner = journal_store.principal(_OWNER)
    await owner.admit(_message("$e1"))
    generation = await journal_store.hold_routing_lane(_ROOM, _OWNER)
    await journal_store.save_routing_handoff(_handoff(recipient=""), owner=_OWNER, generation=generation)

    assert await journal_store.accept_routing_handoffs(_ROOM) == ()
    assert await _handed_off(journal_store, "$e1") == {"$e1": "h1"}
    assert not await owner.is_pending("$e1")
