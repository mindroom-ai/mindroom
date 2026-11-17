"""Per-event room claims: the first writer of one room event's purpose wins, for every bot and across restarts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore

_ROOM = "!room:localhost"


@pytest.mark.asyncio
async def test_first_claim_wins(journal_store: EventJournalStore) -> None:
    """A second claim of the same event and purpose returns the first owner and changes nothing."""
    assert await journal_store.claim_room_event(_ROOM, "$e1", "responder", "alpha") == "alpha"
    assert await journal_store.claim_room_event(_ROOM, "$e1", "responder", "beta") == "alpha"
    assert await journal_store.room_event_claims(_ROOM, ["$e1", "$e2"], "responder") == {"$e1": "alpha"}


@pytest.mark.asyncio
async def test_claims_are_keyed_by_room_event_and_purpose(journal_store: EventJournalStore) -> None:
    """Another purpose, event or room is another claim; an empty owner is a saved owner."""
    assert await journal_store.claim_room_event(_ROOM, "$e1", "responder", "") == ""
    assert await journal_store.claim_room_event(_ROOM, "$e1", "other", "beta") == "beta"
    assert await journal_store.claim_room_event("!other:localhost", "$e1", "responder", "gamma") == "gamma"
    assert await journal_store.claim_room_event(_ROOM, "$e1", "responder", "alpha") == ""
    assert await journal_store.room_event_claims(_ROOM, ["$e1"], "responder") == {"$e1": ""}
    assert await journal_store.room_event_claims(_ROOM, [], "responder") == {}


@pytest.mark.asyncio
async def test_claims_are_shared_across_principals(journal_database: Callable[[], EventJournalStore]) -> None:
    """The table has no principal: every bot's store over the database reads the one saved owner."""
    first, second = journal_database(), journal_database()
    # Each bot holds its own principal view; a claim is not made through one.
    first.principal("@mindroom_alpha:localhost")
    second.principal("@mindroom_beta:localhost")
    assert await first.claim_room_event(_ROOM, "$e1", "responder", "alpha") == "alpha"
    assert await second.claim_room_event(_ROOM, "$e1", "responder", "beta") == "alpha"
    assert await second.room_event_claims(_ROOM, ["$e1"], "responder") == {"$e1": "alpha"}


@pytest.mark.asyncio
async def test_claims_survive_reopen(journal_database: Callable[[], EventJournalStore]) -> None:
    """A claim saved before a restart is the answer after it."""
    store = journal_database()
    assert await store.claim_room_event(_ROOM, "$e1", "responder", "alpha") == "alpha"
    await store.close()
    reopened = journal_database()
    assert await reopened.room_event_claims(_ROOM, ["$e1"], "responder") == {"$e1": "alpha"}
    assert await reopened.claim_room_event(_ROOM, "$e1", "responder", "beta") == "alpha"
