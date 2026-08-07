"""Rejoin answers Matrix accepted to the ledger that records who owns them.

Delivery settles the journal source and acknowledges the outbox row in one
transaction. The ledger write is a second transaction that follows it, so a
crash in between leaves a durable acknowledged answer beside a ledger record
that still says the turn is unfinished and knows no response event. Awaiting
the ledger write narrows that window but cannot close it: two transactions can
always be interrupted between.

Nothing else repairs that. Outbox recovery walks unacknowledged rows, so it
steps over this one; the journal has no pending source, so the turn engine
never re-enters and the visible-response reconciler is never triggered. The
state is therefore permanent, and it is not inert: an edit of the original
message reaches ``EditRegenerator`` and is dropped, because the recovered
record has no response event to edit. The user edits their question and
nothing happens, forever.

This closes that window from the side that has the durable evidence. An
acknowledged ``final`` row is proof that a visible answer exists and what its
event ID is, which is exactly the fact the ledger is missing.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Protocol

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.turn_record import TurnRecord

logger = get_logger(__name__)

_PAGE = 200


class _AcknowledgedFinals(Protocol):
    """The outbox read this repair needs, and nothing else."""

    async def acknowledged_final_deliveries(
        self,
        *,
        limit: int = ...,
        after: tuple[int, str] | None = ...,
    ) -> tuple[tuple[int, str, str], ...]:
        """Return ``(created_at_ns, turn_id, acknowledged_event_id)`` for delivered answers."""
        ...


class _TerminalTurns(Protocol):
    """The ledger side of the join."""

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the durable record for one source event, if any."""
        ...

    async def record_turn(self, turn_record: TurnRecord) -> None:
        """Persist one terminal turn, durable once it returns."""
        ...


async def repair_delivered_turns(deliveries: _AcknowledgedFinals, turns: _TerminalTurns) -> int:
    """Complete ledger records for answers the outbox proves were delivered.

    Returns how many records were repaired. Runs once at startup, before
    anything that reads terminal state can act on it -- a replay guard asking
    ``is_handled`` or an edit looking for a response event both get the wrong
    answer until this has run.

    A record already carrying a response event is left alone. The ledger is
    authoritative when it has the fact; this only supplies one it is missing,
    and overwriting could replace a later, better answer with the first one
    ever sent.
    """
    repaired = 0
    cursor: tuple[int, str] | None = None
    seen = 0
    while True:
        page = await deliveries.acknowledged_final_deliveries(limit=_PAGE, after=cursor)
        if not page:
            break
        for created_at_ns, turn_id, response_event_id in page:
            seen += 1
            record = turns.get_turn_record(turn_id)
            if record is None or record.response_event_id is not None:
                continue
            await turns.record_turn(
                dataclasses.replace(record, response_event_id=response_event_id, completed=True),
            )
            repaired += 1
            logger.info(
                "delivered_turn_ledger_repaired",
                turn_id=turn_id,
                response_event_id=response_event_id,
                created_at_ns=created_at_ns,
            )
        if len(page) < _PAGE:
            break
        # Resume past the last row visited, in the order the read used. This
        # repair changes neither key, so a cursor cannot skip a row or revisit
        # one -- and walking every page matters: stopping at the first would
        # report success while leaving the rest permanently unrepaired.
        cursor = (page[-1][0], page[-1][1])
    if repaired:
        logger.info("delivered_turn_ledger_repair_complete", repaired=repaired, examined=seen)
    return repaired
