"""Deterministic, claim-before-send delivery.

Two crashes have to be survivable at once: a crash after Matrix accepted a
message but before MindRoom recorded it, and a crash after a model produced
content but before it was sent. The first is handled by the deterministic
transaction ID, which makes a resend a no-op on the homeserver. The second is
handled by claiming: the row's payload becomes immutable at the moment it is
first attempted.

Claiming is what closes the dangerous case. Without it, a restarted turn could
regenerate different content, send it under the transaction ID the homeserver
already accepted, and have it silently discarded — leaving the durable result
and the visible message permanently disagreeing.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .models import DeliveryStage, OutboxDelivery

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_OUTBOX_COLUMNS = """
    turn_id, stage, room_id, thread_id, transaction_id,
    payload_json, edits_event_id, acknowledged_event_id
"""


def enqueue(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    edits_event_id: str | None,
) -> str:
    """Record delivery intent, refusing to change an already attempted row."""
    transaction_id = delivery_transaction_id(principal_id, turn_id, stage.value)
    transaction.execute(
        """
        INSERT INTO response_outbox (
            principal_id, turn_id, stage, room_id, thread_id, transaction_id,
            payload_json, edits_event_id, attempted, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT (principal_id, turn_id, stage) DO UPDATE SET
            room_id = excluded.room_id,
            thread_id = excluded.thread_id,
            payload_json = excluded.payload_json,
            edits_event_id = excluded.edits_event_id
        WHERE response_outbox.attempted = 0
        """,
        (
            principal_id,
            turn_id,
            stage.value,
            room_id,
            encode_thread_id(thread_id),
            transaction_id,
            json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            edits_event_id,
            time.time_ns(),
        ),
    )
    return transaction_id


def claim(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
) -> OutboxDelivery | None:
    """Freeze one delivery's content and return exactly what to send.

    Committed before any network I/O, so a delivery that may have reached the
    homeserver can only ever be retried with the identical payload and
    transaction ID.
    """
    transaction.execute(
        """
        UPDATE response_outbox SET attempted = 1
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
        """,
        (principal_id, turn_id, stage.value),
    )
    row = transaction.fetchone(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM response_outbox
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, turn_id, stage.value),
    )
    return None if row is None else _delivery(row)


def acknowledge(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
    event_id: str,
) -> None:
    """Record the Matrix event a claimed delivery produced."""
    transaction.execute(
        """
        UPDATE response_outbox SET acknowledged_event_id = ?
        WHERE principal_id = ? AND turn_id = ? AND stage = ? AND acknowledged_event_id IS NULL
        """,
        (event_id, principal_id, turn_id, stage.value),
    )


def unacknowledged(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
) -> tuple[OutboxDelivery, ...]:
    """Return deliveries that may or may not have reached Matrix, oldest first."""
    rows = transaction.fetchall(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM response_outbox
        WHERE principal_id = ? AND acknowledged_event_id IS NULL
        ORDER BY created_at_ns, turn_id, stage
        LIMIT ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, limit),
    )
    return tuple(_delivery(row) for row in rows)


def load(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
) -> OutboxDelivery | None:
    """Return one delivery without claiming it."""
    row = transaction.fetchone(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM response_outbox
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, turn_id, stage.value),
    )
    return None if row is None else _delivery(row)


def _delivery(row: Row) -> OutboxDelivery:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        msg = f"Outbox payload for turn {row['turn_id']!r} is not an object"
        raise ValueError(msg)
    return OutboxDelivery(
        turn_id=row["turn_id"],
        stage=DeliveryStage(row["stage"]),
        room_id=row["room_id"],
        thread_id=decode_thread_id(row["thread_id"]),
        transaction_id=row["transaction_id"],
        payload=payload,
        edits_event_id=row["edits_event_id"],
        acknowledged_event_id=row["acknowledged_event_id"],
    )
