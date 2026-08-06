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

from mindroom.logging_config import get_logger

from .identity import decode_thread_id, encode_thread_id
from .models import (
    AdmissionResult,
    EventClass,
    EventKind,
    JournalEvent,
    SemanticConsumer,
    SettlementOutcome,
)
from .projection import ProjectedEvent, project
from .schema import PENDING_STATE, SETTLED_STATE

if TYPE_CHECKING:
    from .backend import Row, Transaction
    from .models import InboundEvent

logger = get_logger(__name__)

_JOURNAL_COLUMNS = """
    event_id, room_id, thread_id, kind, event_class, sender,
    origin_server_ts, source_json, receipt_order, membership_epoch, semantic_consumer
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
    """Invalidate everything derived for a room the bot has left and rejoined.

    Rejoining can expose a different slice of history than the bot saw before,
    so anything derived from the previous membership has to stop being trusted
    rather than be merged with the new view. Clearing the hydration marker
    alone would not do that: the projected messages it produced would still be
    readable, and the next hydration would merge the two memberships into one
    conversation. The projection is therefore dropped with it, and rebuilt from
    what the new membership can actually see.

    The journal rows survive on purpose. They are the proof that an event
    already produced its one turn, and that has to outlive any rejoin.
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
    for table in ("conversation_hydration", "visible_messages", "unresolved_edits", "redaction_tombstones"):
        transaction.execute(
            f"DELETE FROM {table} WHERE principal_id = ? AND room_id = ?",  # noqa: S608 - a fixed table list
            (principal_id, room_id),
        )
    # A delivery that has not reached Matrix was written for the conversation
    # this bot was in before it left. Sending it now would answer the previous
    # membership inside the new one. An acknowledged delivery is kept, because
    # its row is the record that the message is already visible.
    #
    # An attempted-but-unacknowledged delivery goes too, even though its
    # network outcome is unknown. Keeping it would only preserve a row nothing
    # reads, and what the room actually contains is re-observed anyway: the
    # projection was just dropped, so the next read rehydrates from the server
    # and sees whichever of these messages landed. What must not happen is the
    # turn's next attempt colliding with a transaction the homeserver already
    # accepted, and that is prevented at the source, by binding the transaction
    # ID to the epoch rather than by keeping the row.
    transaction.execute(
        """
        DELETE FROM response_outbox
        WHERE principal_id = ? AND room_id = ? AND acknowledged_event_id IS NULL
        """,
        (principal_id, room_id),
    )
    return epoch


def admit(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
    projected: ProjectedEvent | None,
) -> AdmissionResult:
    """Insert, deduplicate, and project one event in a single transaction.

    A context-only event is projected here and never replayed, so it keeps no
    payload: it is admitted already settled, and settlement is what would
    otherwise have cleared it. Storing the source anyway would turn the journal
    into the raw-event cache this design exists to remove, at roughly half a
    kilobyte for every message the bot has ever seen.
    """
    epoch = current_membership_epoch(transaction, principal_id, event.room_id)
    actionable = event.event_class is EventClass.ACTIONABLE
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
            (
                json.dumps(dict(event.source), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                if actionable
                else ""
            ),
            epoch,
            PENDING_STATE if actionable else SETTLED_STATE,
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
    after_receipt_order: int | None = None,
) -> tuple[JournalEvent, ...]:
    """Return actionable events awaiting semantic work, in receipt order.

    ``after_receipt_order`` resumes the scan past events a caller has already
    seen. Without it, a caller whose first page is entirely events it cannot
    act on yet — turns still running — could never reach the ones behind them.
    """
    cursor_clause = "" if after_receipt_order is None else " AND receipt_order > ?"
    cursor_params: tuple[object, ...] = () if after_receipt_order is None else (after_receipt_order,)
    rows = transaction.fetchall(
        f"""
        SELECT {_JOURNAL_COLUMNS} FROM journal_events
        WHERE principal_id = ? AND state = 'pending'{cursor_clause}
        ORDER BY receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and a fixed clause, not input
        (principal_id, *cursor_params, limit),
    )
    return _decode_rows(rows)


def _decode_rows(rows: tuple[Row, ...]) -> tuple[JournalEvent, ...]:
    """Decode pending rows, skipping any whose payload cannot be read.

    One unreadable row must not hide every other pending event behind it.
    A row that cannot be decoded stays in place rather than being settled,
    because settling it would claim work was done that never ran.
    """
    events: list[JournalEvent] = []
    for row in rows:
        try:
            events.append(_journal_event(row))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.exception("journal_event_row_unreadable", event_id=row["event_id"])
    return tuple(events)


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
        SET state = ?, outcome = ?, settled_at_ns = ?, source_json = '', semantic_consumer = NULL
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        """,
        (SETTLED_STATE, outcome.value, time.time_ns(), principal_id, event_id),
    )


def settle_many(
    transaction: Transaction,
    principal_id: str,
    event_ids: tuple[str, ...],
    outcome: SettlementOutcome,
) -> None:
    """Settle several events that one terminal turn accounted for."""
    for event_id in event_ids:
        settle(transaction, principal_id, event_id, outcome)


def unsettled_event_ids(transaction: Transaction, principal_id: str) -> frozenset[str]:
    """Return every event that still owes semantic work."""
    rows = transaction.fetchall(
        "SELECT event_id FROM journal_events WHERE principal_id = ? AND state = 'pending'",
        (principal_id,),
    )
    return frozenset(row["event_id"] for row in rows)


def pending_of_kind(
    transaction: Transaction,
    principal_id: str,
    kind: EventKind,
    *,
    limit: int,
) -> tuple[JournalEvent, ...]:
    """Return pending events of one kind, in receipt order."""
    rows = transaction.fetchall(
        f"""
        SELECT {_JOURNAL_COLUMNS} FROM journal_events
        WHERE principal_id = ? AND state = 'pending' AND kind = ?
        ORDER BY receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, kind.value, limit),
    )
    return _decode_rows(rows)


def claim_semantic_consumer(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
    consumer: SemanticConsumer,
) -> SemanticConsumer:
    """Record the sole consumer of one event, returning whoever holds it.

    First claim wins, durably. A replay after a crash therefore cannot let a
    second consumer act on the same reaction.
    """
    row = transaction.fetchone(
        """
        UPDATE journal_events
        SET semantic_consumer = COALESCE(semantic_consumer, ?)
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        RETURNING semantic_consumer
        """,
        (consumer.value, principal_id, event_id),
    )
    if row is None:
        msg = f"Cannot claim a consumer for settled or missing event {event_id!r}"
        raise RuntimeError(msg)
    return SemanticConsumer(row["semantic_consumer"])


def _journal_event(row: Row) -> JournalEvent:
    source = json.loads(row["source_json"]) if row["source_json"] else {}
    if not isinstance(source, dict):
        msg = f"Journal event {row['event_id']!r} has a non-object source"
        raise TypeError(msg)
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
        semantic_consumer=(
            SemanticConsumer(row["semantic_consumer"]) if row["semantic_consumer"] is not None else None
        ),
    )
