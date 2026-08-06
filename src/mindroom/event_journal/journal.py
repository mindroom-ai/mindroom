"""Durable admission and replay of inbound Matrix events.

Admission is the boundary that makes the no-loss guarantee real: nio is told an
event was accepted only after this transaction commits, so a crash before the
commit leaves the event for redelivery rather than losing it.

There is deliberately no durable ``running`` state. A process that dies
mid-turn must leave its event eligible for retry, and a state that says
"someone is working on this" would instead leave it stranded until a human
noticed.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .identity import decode_thread_id, encode_thread_id
from .models import (
    AdmissionResult,
    EventClass,
    EventKind,
    JournalEvent,
    SettlementOutcome,
)
from .projection import ProjectedEvent, project, replacement_target
from .schema import PENDING_STATE, SETTLED_STATE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction
    from .models import InboundEvent

_JOURNAL_COLUMNS = """
    event_id, room_id, thread_id, kind, event_class, sender,
    origin_server_ts, source_json, receipt_order, membership_epoch
"""


def current_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> int:
    """Return the room's current membership epoch, starting at zero."""
    row = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    return 0 if row is None else int(row["membership_epoch"])


def advance_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> int:
    """Invalidate everything hydrated for a room the bot has left and rejoined.

    Rejoining can expose a different slice of history than the bot saw before,
    so anything derived from the previous membership has to stop being trusted
    rather than be merged with the new view.
    """
    epoch = current_membership_epoch(transaction, principal_id, room_id) + 1
    transaction.execute(
        """
        INSERT INTO room_membership (principal_id, room_id, membership_epoch)
        VALUES (?, ?, ?)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET membership_epoch = excluded.membership_epoch
        """,
        (principal_id, room_id, epoch),
    )
    transaction.execute(
        "DELETE FROM conversation_hydration WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    return epoch


def admit(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
    projected: ProjectedEvent | None,
) -> AdmissionResult:
    """Insert, deduplicate, and project one event in a single transaction."""
    epoch = current_membership_epoch(transaction, principal_id, event.room_id)
    row = transaction.fetchone(
        """
        INSERT INTO journal_events (
            principal_id, event_id, room_id, thread_id, kind, event_class, sender,
            origin_server_ts, source_json, membership_epoch, state, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, event_id) DO NOTHING
        RETURNING receipt_order
        """,
        (
            principal_id,
            event.event_id,
            event.room_id,
            encode_thread_id(event.thread_id),
            event.kind.value,
            event.event_class.value,
            event.sender,
            event.origin_server_ts,
            json.dumps(dict(event.source), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            epoch,
            PENDING_STATE if event.event_class is EventClass.ACTIONABLE else SETTLED_STATE,
            time.time_ns(),
        ),
    )
    if row is None:
        return AdmissionResult.DUPLICATE
    if projected is not None:
        project(
            transaction,
            principal_id,
            projected,
            receipt_order=int(row["receipt_order"]),
            membership_epoch=epoch,
        )
    return AdmissionResult.ADMITTED


def pending(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
) -> tuple[JournalEvent, ...]:
    """Return actionable events awaiting semantic work, in receipt order."""
    rows = transaction.fetchall(
        f"""
        SELECT {_JOURNAL_COLUMNS} FROM journal_events
        WHERE principal_id = ? AND state = 'pending'
        ORDER BY receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, limit),
    )
    return tuple(_journal_event(row) for row in rows)


def load(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
) -> JournalEvent | None:
    """Return one admitted event regardless of its settlement state."""
    row = transaction.fetchone(
        f"SELECT {_JOURNAL_COLUMNS} FROM journal_events WHERE principal_id = ? AND event_id = ?",  # noqa: S608
        (principal_id, event_id),
    )
    return None if row is None else _journal_event(row)


def is_pending(transaction: Transaction, principal_id: str, event_id: str) -> bool:
    """Return whether one event still owes semantic work."""
    row = transaction.fetchone(
        "SELECT 1 AS present FROM journal_events WHERE principal_id = ? AND event_id = ? AND state = 'pending'",
        (principal_id, event_id),
    )
    return row is not None


def settle(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
    outcome: SettlementOutcome,
) -> None:
    """Mark one event's semantic work terminal and release its replay payload.

    The payload is cleared rather than the row deleted: the row is the proof
    that this event already produced its one turn, and it has to outlive the
    work it authorized.
    """
    transaction.execute(
        """
        UPDATE journal_events
        SET state = ?, outcome = ?, settled_at_ns = ?, source_json = ''
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        """,
        (SETTLED_STATE, outcome.value, time.time_ns(), principal_id, event_id),
    )


def _journal_event(row: Row) -> JournalEvent:
    source = json.loads(row["source_json"]) if row["source_json"] else {}
    if not isinstance(source, dict):
        msg = f"Journal event {row['event_id']!r} has a non-object source"
        raise ValueError(msg)
    return JournalEvent(
        event_id=row["event_id"],
        room_id=row["room_id"],
        thread_id=decode_thread_id(row["thread_id"]),
        kind=EventKind(row["kind"]),
        event_class=EventClass(row["event_class"]),
        sender=row["sender"],
        origin_server_ts=int(row["origin_server_ts"]),
        source=source,
        receipt_order=int(row["receipt_order"]),
        membership_epoch=int(row["membership_epoch"]),
    )


def projected_event_from_source(
    *,
    event_id: str,
    room_id: str,
    thread_id: str | None,
    sender: str,
    origin_server_ts: int,
    content: Mapping[str, object],
    redacts_event_id: str | None,
) -> ProjectedEvent:
    """Build the projection view of one event."""
    return ProjectedEvent(
        event_id=event_id,
        room_id=room_id,
        thread_id=thread_id,
        sender=sender,
        origin_server_ts=origin_server_ts,
        content=content,
        replaces_event_id=replacement_target(content),
        redacts_event_id=redacts_event_id,
    )
