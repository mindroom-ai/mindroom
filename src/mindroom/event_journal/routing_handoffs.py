"""The routing lane's rows: which bot owns each room's lane, and the turns a lane owner handed to a responder."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import journal, room_claims

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .backend import Row, Transaction
    from .models import InboundEvent

# A source event's claim names the one handoff it belongs to, so no message is ever handed off twice.
_SOURCE_PURPOSE = "routing_handoff"


class StaleLaneOwnerError(RuntimeError):
    """Another bot took the room's routing lane, so this one may no longer save what it decided."""


@dataclass(frozen=True, slots=True)
class RoutingHandoff:
    """One turn a room's lane owner handed to a responder: the same row however often the turn replays."""

    handoff_id: str
    room_id: str
    source_event_ids: tuple[str, ...]
    # The entity chosen to answer; empty when nobody fits, which no bot answers.
    recipient: str
    # What rebuilds the prepared turn; None once the recipient holds it in its own journal, or when nobody fits.
    payload: Mapping[str, object] | None


def hold_lane(transaction: Transaction, room_id: str, owner: str) -> int:
    """Make ``owner`` the room's lane owner and return the lane's generation, which grows when the owner changes."""
    transaction.execute(
        """
        INSERT INTO routing_lanes (room_id, owner, generation) VALUES (?, ?, 1)
        ON CONFLICT (room_id) DO UPDATE SET
            generation = routing_lanes.generation + (CASE WHEN routing_lanes.owner = excluded.owner THEN 0 ELSE 1 END),
            owner = excluded.owner
        """,
        (room_id, owner),
    )
    row = transaction.fetchone("SELECT generation FROM routing_lanes WHERE room_id = ?", (room_id,))
    assert row is not None
    return int(row["generation"])


def save(transaction: Transaction, handoff: RoutingHandoff, *, owner: str, generation: int) -> None:
    """Save one handoff as the lane owner ``hold_lane`` gave ``generation``; a bot that lost the lane since is refused.

    The no-op update locks the lane's row, so on PostgreSQL a takeover cannot commit between this check and the insert.
    """
    lane = transaction.fetchone(
        """
        UPDATE routing_lanes SET generation = generation
        WHERE room_id = ? AND owner = ? AND generation = ?
        RETURNING generation
        """,
        (handoff.room_id, owner, generation),
    )
    if lane is None:
        msg = f"{owner} no longer owns the routing lane of {handoff.room_id}"
        raise StaleLaneOwnerError(msg)
    for event_id in handoff.source_event_ids:
        saved = room_claims.claim(transaction, handoff.room_id, event_id, _SOURCE_PURPOSE, handoff.handoff_id)
        if saved != handoff.handoff_id:
            # Raising rolls the earlier claims back.
            msg = f"{event_id} already belongs to handoff {saved}"
            raise ValueError(msg)
    transaction.execute(
        """
        INSERT INTO routing_handoffs (
            handoff_id, room_id, source_event_ids_json, recipient, generation, payload_json, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            handoff.handoff_id,
            handoff.room_id,
            json.dumps(handoff.source_event_ids),
            handoff.recipient,
            generation,
            None if handoff.payload is None else json.dumps(dict(handoff.payload), separators=(",", ":")),
            time.time_ns(),
        ),
    )


def for_turn(transaction: Transaction, room_id: str, source_event_ids: Sequence[str]) -> tuple[RoutingHandoff, ...]:
    """Return the handoffs these sources belong to and every handoff of the room no recipient holds yet, oldest first."""
    handoff_ids = set(room_claims.claims(transaction, room_id, source_event_ids, _SOURCE_PURPOSE).values())
    rows = {
        str(row["handoff_id"]): row
        for row in transaction.fetchall(
            "SELECT * FROM routing_handoffs WHERE room_id = ? AND payload_json IS NOT NULL",
            (room_id,),
        )
    }
    for handoff_id in handoff_ids - rows.keys():
        row = transaction.fetchone("SELECT * FROM routing_handoffs WHERE handoff_id = ?", (handoff_id,))
        assert row is not None, "a source's claim is saved with its handoff"
        rows[handoff_id] = row
    return tuple(_handoff(row) for row in sorted(rows.values(), key=lambda row: int(row["created_at_ns"])))


def accept(transaction: Transaction, principal_id: str, event: InboundEvent) -> None:
    """Put a handoff, as the journal event named by its ID, in its recipient's journal and release the row's copy.

    The recipient's journal admits one event per ID, so accepting a handoff again changes nothing.
    """
    journal.admit_snapshotting_source(transaction, principal_id, event, None)
    transaction.execute("UPDATE routing_handoffs SET payload_json = NULL WHERE handoff_id = ?", (event.event_id,))


def _handoff(row: Row) -> RoutingHandoff:
    payload = row["payload_json"]
    return RoutingHandoff(
        handoff_id=str(row["handoff_id"]),
        room_id=str(row["room_id"]),
        source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
        recipient=str(row["recipient"]),
        payload=None if payload is None else json.loads(payload),
    )
