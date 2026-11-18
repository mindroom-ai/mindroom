"""One saved responder per source event, which every bot of the process reads before it answers an unmentioned turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import turn_rows

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.event_journal import EventJournalStore

# The saved choice of a turn nobody fits: every candidate skips it.
NO_RESPONDER = ""
_PURPOSE = "responder"


@dataclass
class ResponderSelections:
    """Save who answers an unmentioned turn once, however many bots ask and however often the turn replays."""

    _journal: Callable[[], EventJournalStore]
    _room_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def select(
        self,
        room_id: str,
        source_event_ids: Sequence[str],
        choose: Callable[[], Awaitable[str]] | None,
    ) -> frozenset[str]:
        """Return the entities saved for this turn's sources; a bot answers the turn only when it is one of them.

        The first source in turn order that has a saved choice decides for the sources without one, so a later, larger
        batch keeps the choice an earlier, smaller batch made. Only a turn none of whose sources has one awaits
        ``choose``; a bot that would not answer by itself passes None, which saves nothing and reads what another bot
        saved. The room's lock makes the bots that ask at once wait for the first and read its rows. Bots that
        batched a burst differently can have saved different entities for its messages; each then answers its whole
        batch, so a message can get two answers but is never left to nobody. ``NO_RESPONDER`` names nobody.
        """
        async with self._room_locks.setdefault(room_id, asyncio.Lock()):
            journal = self._journal()
            saved = await journal.room_event_claims(room_id, source_event_ids, _PURPOSE)
            choice = next((saved[event_id] for event_id in source_event_ids if event_id in saved), None)
            if choice is None and choose is None:
                return frozenset()
            unsaved = [
                event_id
                for event_id in source_event_ids
                if event_id not in saved and not await _relayed_by_the_old_router(journal, event_id)
            ]
            if unsaved:
                if choice is None:
                    assert choose is not None, "a read without a saved choice returned above"
                    choice = await choose()
                for event_id in unsaved:
                    saved[event_id] = await journal.claim_room_event(room_id, event_id, _PURPOSE, choice)
        return frozenset(saved.values()) - {NO_RESPONDER}


# LEGACY_COMPAT: a person's message the relaying router already answered with its relay ("@agent could you help with
# this?") or its routing notice, which a bot that had not planned its own copy before the upgrade must not answer too.
# Legacy format: the router's turn row for the message, with a response event that is not its voice echo.
# Last legacy release: v2026.11.396, the newest release whose router relays; see `IngressFacts.router_relay`.
# Handling: such a message gets no saved responder, so the relay stays its one delivery; the messages batched with it
# keep theirs.
# Coverage: tests/test_responder_selection.py::test_message_the_old_router_relayed_is_left_to_its_relay and
# tests/test_turns_focused.py::test_original_the_old_router_already_relayed_gets_no_second_answer
async def _relayed_by_the_old_router(journal: EventJournalStore, event_id: str) -> bool:
    router_turns = journal.turn_records(ROUTER_AGENT_NAME)
    turn = await router_turns.read(turn_rows.get_turn_record, event_id)
    if turn is None or turn.response_event_id is None:
        return False
    # Until the answering agent posts the transcript, the router records its echo of a voice message as its answer.
    echoes = [await router_turns.read(turn_rows.visible_echo, source) for source in turn.source_event_ids]
    return turn.response_event_id not in {echo.event_id for echo in echoes if echo is not None}
