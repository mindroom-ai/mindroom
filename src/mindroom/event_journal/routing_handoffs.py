"""The routing lane's rows: which bot owns each room's lane, and the turns a lane owner handed to a responder.

Bots are named by their journal principals, so accepting a handoff moves a turn between two journals in one
transaction: it enters the recipient's journal as the lane owner's events for it settle.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import journal, room_claims
from .identity import matrix_user_of_principal
from .models import EventClass, EventKind, InboundEvent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Transaction

# The purpose of the room-event claim that names the one handoff a source event belongs to, so no message is ever
# handed off twice.
ROUTING_HANDOFF_CLAIM = "routing_handoff"


class StaleLaneOwnerError(RuntimeError):
    """Another bot took the room's routing lane, so this one may no longer save what it decided."""


@dataclass(frozen=True, slots=True)
class RoutingHandoff:
    """One turn a room's lane owner hands to a responder: saved once, however often the turn replays."""

    handoff_id: str
    room_id: str
    source_event_ids: tuple[str, ...]
    # The journal principal of the bot chosen to answer; empty when nobody fits, which no bot answers.
    recipient: str
    # What rebuilds the prepared turn; None when nobody fits.
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
    A turn nobody fits has nothing to hand over, so the owner's events settle with the row.
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
        saved = room_claims.claim(transaction, handoff.room_id, event_id, ROUTING_HANDOFF_CLAIM, handoff.handoff_id)
        if saved != handoff.handoff_id:
            # Raising rolls the earlier claims back.
            msg = f"{event_id} already belongs to handoff {saved}"
            raise ValueError(msg)
    transaction.execute(
        """
        INSERT INTO routing_handoffs (
            handoff_id, room_id, source_event_ids_json, recipient, owner, generation, payload_json, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            handoff.handoff_id,
            handoff.room_id,
            json.dumps(handoff.source_event_ids),
            handoff.recipient,
            owner,
            generation,
            None if handoff.payload is None else json.dumps(dict(handoff.payload), separators=(",", ":")),
            time.time_ns(),
        ),
    )
    if handoff.payload is None:
        journal.settle_many(transaction, owner, handoff.source_event_ids)


def accept(transaction: Transaction, room_id: str | None) -> tuple[str, ...]:
    """Move every handoff no recipient holds yet, of one room or of all, into its recipient's journal; return them.

    Each becomes the journal event named by its ID there, its events settle in the journal of the lane owner that
    saved it, and the row gives up its payload, all at once. So a restart finds the turn in exactly one journal, and
    accepting again moves nothing.
    """
    scope = (room_id or "", room_id or "")
    rows = transaction.fetchall(
        """
        SELECT * FROM routing_handoffs WHERE payload_json IS NOT NULL AND (room_id = ? OR ? = '')
        ORDER BY created_at_ns
        """,
        scope,
    )
    for row in rows:
        owner = str(row["owner"])
        journal.admit_snapshotting_source(
            transaction,
            str(row["recipient"]),
            InboundEvent(
                event_id=str(row["handoff_id"]),
                room_id=str(row["room_id"]),
                thread_id=None,
                kind=EventKind.ROUTED_TURN,
                event_class=EventClass.ACTIONABLE,
                sender=matrix_user_of_principal(owner),
                origin_server_ts=int(row["created_at_ns"]) // 1_000_000,
                source=json.loads(row["payload_json"]),
            ),
            None,
        )
        journal.settle_many(transaction, owner, tuple(json.loads(row["source_event_ids_json"])))
    transaction.execute(
        "UPDATE routing_handoffs SET payload_json = NULL WHERE payload_json IS NOT NULL AND (room_id = ? OR ? = '')",
        scope,
    )
    return tuple(str(row["recipient"]) for row in rows)
