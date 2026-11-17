"""One saved responder choice per source event, which every bot of the process reads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.event_journal import EventJournalStore

# The saved choice of a turn nobody fits: every candidate skips it.
NO_RESPONDER = ""
_PURPOSE = "responder"


@dataclass
class ResponderSelections:
    """Pick who answers an unmentioned turn once, however many bots ask and however often the turn replays."""

    _journal: Callable[[], EventJournalStore]
    _room_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def select(
        self,
        room_id: str,
        source_event_ids: Sequence[str],
        choose: Callable[[], Awaitable[str]],
    ) -> str | None:
        """Return the entity saved for this turn's first source, or None when the saved choice is ``NO_RESPONDER``.

        The first source in turn order that has a saved choice decides, and the sources without one inherit it, so a
        later, larger batch keeps the choice an earlier, smaller batch made. Only a turn none of whose sources has one
        awaits ``choose``. The room's lock makes the bots that ask at once wait for the first and read its rows.
        """
        async with self._room_locks.setdefault(room_id, asyncio.Lock()):
            journal = self._journal()
            saved = await journal.room_event_claims(room_id, source_event_ids, _PURPOSE)
            choice = next((saved[event_id] for event_id in source_event_ids if event_id in saved), None)
            if choice is None:
                choice = await choose()
            for event_id in source_event_ids:
                if event_id not in saved:
                    await journal.claim_room_event(room_id, event_id, _PURPOSE, choice)
        return None if choice == NO_RESPONDER else choice
