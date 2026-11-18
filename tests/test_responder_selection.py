"""The process-wide responder selection: one saved choice per source event, shared by every bot."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import turn_rows
from mindroom.responder_selection import NO_RESPONDER, ResponderSelections
from mindroom.turn_record import TurnRecord

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
    assert results == [{"alpha"}] * 3
    assert choose.calls == 1


@pytest.mark.asyncio
async def test_saved_choice_is_reused_after_restart(journal_database: Callable[[], EventJournalStore]) -> None:
    """A new service on the same journal reads the saved choice and never runs the selector."""
    journal = journal_database()
    assert await ResponderSelections(lambda: journal).select(_ROOM, ["$e1"], _Chooser("alpha")) == {"alpha"}
    await journal.close()
    reopened = journal_database()
    assert await ResponderSelections(lambda: reopened).select(_ROOM, ["$e1"], _never) == {"alpha"}


@pytest.mark.asyncio
async def test_larger_batch_inherits_first_source_choice(journal_store: EventJournalStore) -> None:
    """A later batch that adds a source keeps the earlier batch's choice and saves it for the new source."""
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e1"], _Chooser("alpha")) == {"alpha"}
    assert await selections.select(_ROOM, ["$e1", "$e2"], _never) == {"alpha"}
    assert await journal_store.room_event_claims(_ROOM, ["$e1", "$e2"], "responder") == {"$e1": "alpha", "$e2": "alpha"}
    # A batch that starts at a new source inherits the choice of the first source that has one.
    assert await selections.select(_ROOM, ["$e0", "$e2"], _never) == {"alpha"}


@pytest.mark.asyncio
async def test_conflicting_rows_follow_first_source(journal_store: EventJournalStore) -> None:
    """When two bots split a burst differently and saved different choices, a new source follows the first one."""
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e1"], _Chooser("alpha")) == {"alpha"}
    assert await selections.select(_ROOM, ["$e2"], _Chooser("beta")) == {"beta"}
    assert await selections.select(_ROOM, ["$e2", "$e1", "$e3"], _never) == {"alpha", "beta"}
    assert await journal_store.room_event_claims(_ROOM, ["$e1", "$e2", "$e3"], "responder") == {
        "$e1": "alpha",
        "$e2": "beta",
        "$e3": "beta",
    }


@pytest.mark.asyncio
async def test_mixed_batch_names_every_saved_owner(journal_store: EventJournalStore) -> None:
    """A batch whose sources were saved for different bots names them all, so neither bot drops its own source."""
    selections = ResponderSelections(lambda: journal_store)
    await selections.select(_ROOM, ["$e1"], _Chooser("alpha"))
    await selections.select(_ROOM, ["$e2"], _Chooser("beta"))
    assert await selections.select(_ROOM, ["$e1", "$e2"], _never) == {"alpha", "beta"}
    # A saved no-fit names nobody, and takes no source from the bot another source names.
    await selections.select(_ROOM, ["$e3"], _Chooser(NO_RESPONDER))
    assert await selections.select(_ROOM, ["$e3", "$e2"], _never) == {"beta"}


@pytest.mark.asyncio
async def test_no_fit_is_saved_and_names_nobody(journal_store: EventJournalStore) -> None:
    """An accepted no-fit is a saved choice: nobody answers, now or on replay."""
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e1"], _Chooser(NO_RESPONDER)) == frozenset()
    assert await selections.select(_ROOM, ["$e1"], _never) == frozenset()
    assert await journal_store.room_event_claims(_ROOM, ["$e1"], "responder") == {"$e1": NO_RESPONDER}


async def _record_router_answer(journal_store: EventJournalStore, source_event_id: str, response_event_id: str) -> None:
    """Record that the router answered one source with a message of its own, as the relaying router did."""
    await journal_store.turn_records(ROUTER_AGENT_NAME).write(
        turn_rows.record_responded_turn,
        TurnRecord.create([source_event_id], response_event_id=response_event_id),
        room_id=_ROOM,
    )


@pytest.mark.asyncio
async def test_message_the_old_router_relayed_is_left_to_its_relay(journal_store: EventJournalStore) -> None:
    """A message whose relay the old router already sent gets no second, direct answer: the relay is its delivery."""
    await _record_router_answer(journal_store, "$e1", "$relay")
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$e0", "$e1"], _never) == frozenset()
    assert await journal_store.room_event_claims(_ROOM, ["$e0", "$e1"], "responder") == {
        "$e0": NO_RESPONDER,
        "$e1": NO_RESPONDER,
    }


@pytest.mark.asyncio
async def test_router_voice_echo_is_not_a_relay(journal_store: EventJournalStore) -> None:
    """The transcript echo the router still records as its answer to a voice message leaves the message selectable."""
    router_turns = journal_store.turn_records(ROUTER_AGENT_NAME)
    await router_turns.write(
        turn_rows.record_visible_echo,
        room_id=_ROOM,
        source_event_id="$voice",
        echo_event_id="$echo",
    )
    await _record_router_answer(journal_store, "$voice", "$echo")
    selections = ResponderSelections(lambda: journal_store)
    assert await selections.select(_ROOM, ["$voice"], _Chooser("alpha")) == {"alpha"}
