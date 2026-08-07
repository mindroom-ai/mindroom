"""The approval cards this bot sent and is still waiting on.

A tool-approval card outlives the process that sent it. The user may click it
after a restart, and the router has to expire the ones nobody answered, so the
card event has to be recoverable from somewhere. Neither of the other owners
here can carry it: ``visible_messages`` models conversation messages, and the
journal clears a settled event's payload on purpose, so by the time anyone
asked the card would be gone.

The table is deliberately narrow, and the narrowness is the point -- this is
the alternative to keeping a general event cache alive for one consumer.

A card is answered the moment the bot commits to a decision, which is before
it can know whether the edit carrying that decision reached the room. The
decision is therefore written down first and the row is dropped only once the
room shows it. A row that survives with a decision on it is not a card anyone
still owes an answer to; it is an answer that may not have been delivered, and
resending the identical edit is what settles it.

Only cards this bot authored are ever stored, because only those are ever
recovered; a card another sender wrote is not this bot's to resolve.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_DEFAULT_ROOM_CARD_LIMIT = 256


@dataclass(frozen=True, slots=True)
class StoredApprovalCard:
    """One recorded card, and the decision it is already carrying if any."""

    card: dict[str, Any]
    # None while the card is genuinely unanswered. Once set, the decision was
    # made and only its delivery is in doubt.
    resolution: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RecordedApprovalDecision:
    """What the durable row carries after one attempt to record a decision."""

    # The decision the row now holds, which is not always the one just
    # offered: a card already carrying a decision keeps its first one. None
    # when no row exists at all, so nothing durable agrees with any decision
    # and nothing will ever redeliver or expire the card.
    resolution: dict[str, Any] | None
    # Whether this call is what committed the decision it offered. False both
    # when there was no row to write and when the row refused the write.
    recorded: bool


def resolve(
    transaction: Transaction,
    principal_id: str,
    *,
    card_event_id: str,
    resolution: Mapping[str, Any],
) -> RecordedApprovalDecision:
    """Record the decision this bot is about to show, before it shows it.

    Written before the Matrix edit, so a crash between the two leaves an
    answered card rather than a pending one. Startup then redelivers this exact
    decision instead of expiring a card the room may already show as approved.

    The update can match nothing, and the caller cannot act on a decision the
    store did not take, so what the row actually ends up carrying is reported
    rather than assumed.
    """
    offered = dict(resolution)
    row = transaction.fetchone(
        """
        UPDATE approval_cards SET resolution_json = ?
        WHERE principal_id = ? AND card_event_id = ? AND resolution_json IS NULL
        RETURNING card_event_id
        """,
        (
            json.dumps(offered, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            principal_id,
            card_event_id,
        ),
    )
    if row is not None:
        return RecordedApprovalDecision(resolution=offered, recorded=True)
    # Matching nothing has two causes that are not the same fact. Either no
    # such card was ever stored, or the card already carries a decision that
    # the ``resolution_json IS NULL`` guard exists to protect. Only the second
    # leaves something a later startup will redeliver.
    existing = transaction.fetchone(
        "SELECT resolution_json FROM approval_cards WHERE principal_id = ? AND card_event_id = ?",
        (principal_id, card_event_id),
    )
    stored = None if existing is None else _resolution(existing["resolution_json"])
    return RecordedApprovalDecision(resolution=stored, recorded=False)


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
) -> StoredApprovalCard | None:
    """Return one card this bot still owes work on, or nothing if it is fenced."""
    row = transaction.fetchone(
        """
        SELECT cards.card_json AS card_json, cards.resolution_json AS resolution_json
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
) -> tuple[StoredApprovalCard, ...]:
    """Return one room's unfinished cards, oldest first."""
    rows = transaction.fetchall(
        """
        SELECT cards.card_json AS card_json, cards.resolution_json AS resolution_json
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


def _card(row: Row) -> StoredApprovalCard:
    card = json.loads(row["card_json"])
    if not isinstance(card, dict):
        msg = "Stored approval card is not an object"
        raise TypeError(msg)
    return StoredApprovalCard(card=card, resolution=_resolution(row["resolution_json"]))


def _resolution(stored: str | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    resolution = json.loads(stored)
    if not isinstance(resolution, dict):
        msg = "Stored approval resolution is not an object"
        raise TypeError(msg)
    return resolution
