"""The process-wide responder selection: one saved choice per source event, shared by every bot."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.responder_selection import NO_RESPONDER, ResponderSelections

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import EventJournalStore

_ROOM = "!room:localhost"


class _Chooser:
    """A selector that records how often it ran."""

    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        await asyncio.sleep(0)
        return self.choice


def _never() -> Awaitable[str]:
    msg = "A saved choice must not run the selector again"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_concurrent_bots_run_choose_once(journal_store: EventJournalStore) -> None:
    """Three bots asking about one turn at once get one selector run and the same answer."""
    selections = ResponderSelections(lambda: journal_store)
    choose = _Chooser("alpha")
    results = await asyncio.gather(*(selections.select(_ROOM, ["$e1"], choose) for _ in range(3)))
    assert results == ["alpha", "alpha", "alpha"]
    assert choose.calls == 1


@pytest.mark.asyncio
async def test_saved_choice_is_reused_after_restart(journal_database: Callable[[], EventJournalStore]) -> None:
    """A new service on the same journal reads the saved choice and never runs the selector."""
    journal = journal_database()
    assert await ResponderSelections(lambda: journal).select(_ROOM, ["$e1"], _Chooser("alpha")) == "alpha"
    await journal.close()
    reopened = journal_database()
    assert await ResponderSelections(lambda: reopened).select(_ROOM, ["$e1"], _never) == "alpha"


@pytest.mark.asyncio
async def test_larger_batch_inherits_first_source_choice(journal_store: EventJournalStore) -> None:
    """A later batch that adds a source keeps the earlier batch's choice and saves it for the new source."""
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e1"], _Chooser("alpha")) == "alpha"
    assert await selections.select(_ROOM, ["$e1", "$e2"], _never) == "alpha"
    assert await journal_store.room_event_claims(_ROOM, ["$e1", "$e2"], "responder") == {"$e1": "alpha", "$e2": "alpha"}
    # A batch that starts at a new source inherits the choice of the first source that has one.
    assert await selections.select(_ROOM, ["$e0", "$e2"], _never) == "alpha"


@pytest.mark.asyncio
async def test_conflicting_rows_follow_first_source(journal_store: EventJournalStore) -> None:
    """When two bots split a burst differently and saved different choices, each turn follows its own first source."""
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e1"], _Chooser("alpha")) == "alpha"
    assert await selections.select(_ROOM, ["$e2"], _Chooser("beta")) == "beta"
    assert await selections.select(_ROOM, ["$e1", "$e2"], _never) == "alpha"
    assert await selections.select(_ROOM, ["$e2", "$e1"], _never) == "beta"
    assert await journal_store.room_event_claims(_ROOM, ["$e1", "$e2"], "responder") == {"$e1": "alpha", "$e2": "beta"}


@pytest.mark.asyncio
async def test_no_fit_is_saved_and_returns_none(journal_store: EventJournalStore) -> None:
    """An accepted no-fit is a saved choice: nobody answers, now or on replay."""
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e1"], _Chooser(NO_RESPONDER)) is None
    assert await selections.select(_ROOM, ["$e1"], _never) is None
    assert await journal_store.room_event_claims(_ROOM, ["$e1"], "responder") == {"$e1": NO_RESPONDER}
