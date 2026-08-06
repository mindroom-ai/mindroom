"""The approval cards this bot sent and is still waiting on.

A tool-approval card outlives the process that sent it. The user may click it
after a restart, and the router has to expire the ones nobody answered, so the
card event has to be recoverable from somewhere. Neither of the other owners
here can carry it: ``visible_messages`` models conversation messages, and the
journal clears a settled event's payload on purpose, so by the time anyone
asked the card would be gone.

The table is deliberately narrow, and the narrowness is the point -- this is
the alternative to keeping a general event cache alive for one consumer. A
row's existence *is* the pending state: resolving a card deletes its row, so
there is no status column that can disagree with what Matrix shows.

Only cards this bot authored are ever stored, because only those are ever
recovered; a card another sender wrote is not this bot's to resolve.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_DEFAULT_ROOM_CARD_LIMIT = 256


def remember(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    card: Mapping[str, Any],
) -> None:
    """Record one sent approval card as pending under the current membership."""
    epoch = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    transaction.execute(
        """
        INSERT INTO approval_cards (
            principal_id, room_id, card_event_id, card_json, membership_epoch, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        """,
        (
            principal_id,
            room_id,
            card_event_id,
            json.dumps(dict(card), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            0 if epoch is None else int(epoch["membership_epoch"]),
            time.time_ns(),
        ),
    )


def forget(
    transaction: Transaction,
    principal_id: str,
    *,
    card_event_id: str,
) -> None:
    """Drop one card that has reached a terminal state."""
    transaction.execute(
        "DELETE FROM approval_cards WHERE principal_id = ? AND card_event_id = ?",
        (principal_id, card_event_id),
    )


def pending_card(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
) -> dict[str, Any] | None:
    """Return one still-pending card, or nothing if it is resolved or fenced."""
    row = transaction.fetchone(
        """
        SELECT cards.card_json AS card_json
        FROM approval_cards AS cards
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE cards.principal_id = ?
          AND cards.room_id = ?
          AND cards.card_event_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0)
        """,
        (principal_id, room_id, card_event_id),
    )
    return None if row is None else _card(row)


def pending_cards(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    limit: int = _DEFAULT_ROOM_CARD_LIMIT,
) -> tuple[dict[str, Any], ...]:
    """Return one room's still-pending cards, oldest first."""
    rows = transaction.fetchall(
        """
        SELECT cards.card_json AS card_json
        FROM approval_cards AS cards
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE cards.principal_id = ?
          AND cards.room_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0)
        -- Two cards sent in the same nanosecond would otherwise come back in
        -- whatever order each backend felt like, and the caller expires them
        -- in the order it reads them.
        ORDER BY cards.created_at_ns, cards.card_event_id/*bytes*/
        LIMIT ?
        """,
        (principal_id, room_id, limit),
    )
    return tuple(_card(row) for row in rows)


def _card(row: Row) -> dict[str, Any]:
    card = json.loads(row["card_json"])
    if not isinstance(card, dict):
        msg = "Stored approval card is not an object"
        raise TypeError(msg)
    return card
