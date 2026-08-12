"""Deterministic, claim-before-send delivery.

Two crashes have to be survivable at once: a crash after Matrix accepted a
message but before MindRoom recorded it, and a crash after a model produced
content but before it was sent. The first is handled by the deterministic
transaction ID, which makes a resend a no-op on the homeserver. The second is
handled by claiming: the row's payload becomes immutable at the moment it is
first attempted.

That first guarantee has a boundary, and the row records where it ends. A
Matrix transaction ID is idempotent within the device that used it, so a row
attempted before a re-login carries an ID the homeserver has never seen from
the device now retrying, and the "no-op" resend posts a second answer. The
claim therefore stores the sending device alongside the attempt, which is what
lets delivery notice that the guarantee no longer holds and go and look
instead.

Claiming is what closes the dangerous case. Without it, a restarted turn could
regenerate different content, send it under the transaction ID the homeserver
already accepted, and have it silently discarded — leaving the durable result
and the visible message permanently disagreeing.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .models import DeliveryStage, OutboxDelivery

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_OUTBOX_COLUMNS = """
    turn_id, stage, room_id, thread_id, transaction_id,
    payload_json, edits_event_id, acknowledged_event_id, created_at_ns,
    attempted, sending_device_id
"""


def _lock_turn_deliveries(transaction: Transaction, principal_id: str, turn_id: str) -> None:
    """Serialize stage decisions on the INITIAL row shared by both stages."""
    transaction.execute(
        """
        UPDATE response_outbox SET attempted = attempted
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
        """,
        (principal_id, turn_id, DeliveryStage.INITIAL.value),
    )


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
    _lock_turn_deliveries(transaction, principal_id, turn_id)
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


def is_attempted(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
) -> bool:
    """Return whether this delivery has already been offered to the homeserver.

    An attempted row is a different object from an unattempted one. Its
    outcome is unknown, the homeserver may hold it already, and the frozen
    transaction ID is the only thing that makes a retry collapse onto the same
    event rather than post a second answer.
    """
    row = transaction.fetchone(
        """
        SELECT 1 AS present FROM response_outbox
        WHERE principal_id = ? AND turn_id = ? AND stage = ? AND attempted = 1
        """,
        (principal_id, turn_id, stage.value),
    )
    return row is not None


def claim(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
) -> OutboxDelivery | None:
    """Freeze one delivery's content and return the row as it stood.

    Committed before any network I/O, so a delivery that may have reached the
    homeserver can only ever be retried with the identical payload and
    transaction ID.

    What comes back describes the row as it stood *before* this call, which is
    what the caller needs to see the state it is taking over from: whether
    anyone has sent this before, and from which device. Reporting this attempt
    back to itself would make every resend look like a first one, and a first
    one skips the room lookup that stops a changed device posting a second
    answer.

    Reading the row and then marking it is the obvious way to get that view and
    it is wrong, for the same reason it was wrong in ``acknowledge``: two
    processes against one PostgreSQL database can both read ``attempted = 0``,
    and both then believe they are the first sender and send. Reproduced with
    two stores on one database -- both claims came back unattempted, both would
    have gone straight to the wire. SQLite never showed it, because
    ``BEGIN IMMEDIATE`` holds a write lock across the whole transaction, so a
    read followed by a write there is already atomic. A rule that holds on only
    one backend is not one this codebase has.

    So the marking statement reports the pre-claim state itself. No portable
    SQL hands back pre-update values -- ``RETURNING`` is post-update on both
    backends, and PostgreSQL's self-join trick for the old row is rejected by
    SQLite -- but none is needed, because ``attempted`` is the only column this
    write touches and a conditional update tells you exactly what it was: the
    row was unattempted if and only if this statement is the one that flipped
    it. Everything else is read afterwards unchanged.

    The sending device is deliberately *not* written here. Claiming freezes the
    payload; it does not mean this device is going to send. When the recorded
    device differs from this process's, delivery has to read the room before it
    can send at all, and that lookup can fail. Advancing the marker first would
    erase the only evidence that the lookup is still owed: the next pass would
    see its own device, skip the lookup, and post the answer a second time.
    ``record_sending_device`` is called once the send is actually about to
    happen.

    INITIAL and FINAL are also one durable ordering decision. An unattempted
    INITIAL is withdrawn once FINAL exists. An attempted, unacknowledged
    INITIAL is different: Matrix may still accept it, so FINAL cannot be
    offered under its distinct transaction ID until retrying INITIAL has
    resolved that unknown outcome. Locking the INITIAL row makes those
    checks atomic with both claiming and insertion on PostgreSQL as well as
    SQLite.
    """
    _lock_turn_deliveries(transaction, principal_id, turn_id)
    current = transaction.fetchone(
        """
        SELECT attempted, edits_event_id FROM response_outbox
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
        """,
        (principal_id, turn_id, stage.value),
    )
    if current is None:
        return None
    if stage is DeliveryStage.INITIAL and not bool(current["attempted"]):
        final = transaction.fetchone(
            """
            SELECT 1 AS present FROM response_outbox
            WHERE principal_id = ? AND turn_id = ? AND stage = ?
            """,
            (principal_id, turn_id, DeliveryStage.FINAL.value),
        )
        if final is not None:
            transaction.execute(
                """
                DELETE FROM response_outbox
                WHERE principal_id = ? AND turn_id = ? AND stage = ? AND attempted = 0
                """,
                (principal_id, turn_id, DeliveryStage.INITIAL.value),
            )
            return None
    if stage is DeliveryStage.FINAL and current["edits_event_id"] is None:
        unresolved_initial = transaction.fetchone(
            """
            SELECT 1 AS present FROM response_outbox
            WHERE principal_id = ? AND turn_id = ? AND stage = ?
              AND attempted = 1 AND acknowledged_event_id IS NULL
            """,
            (principal_id, turn_id, DeliveryStage.INITIAL.value),
        )
        if unresolved_initial is not None:
            return None
    marked = transaction.fetchone(
        """
        UPDATE response_outbox SET attempted = 1
        WHERE principal_id = ? AND turn_id = ? AND stage = ? AND attempted = 0
        RETURNING turn_id
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
    if row is None:
        return None
    return replace(_delivery(row), attempted=marked is None)


def record_sending_device(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
    device_id: str | None,
) -> None:
    """Record the device whose transaction-ID namespace is about to be used.

    Committed before the network call, for the reason ``attempted`` is: a crash
    mid-send has to leave behind the fact that this device may already hold the
    ID, so the next attempt resends rather than reading the room.

    Only ever called on the path that is about to send. A pass that could not
    determine whether an earlier device's answer reached the room leaves the
    marker alone, so the lookup stays owed.
    """
    transaction.execute(
        """
        UPDATE response_outbox SET sending_device_id = ?
        WHERE principal_id = ? AND turn_id = ? AND stage = ?
        """,
        (device_id, principal_id, turn_id, stage.value),
    )


def acknowledge(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
    event_id: str,
) -> bool:
    """Record the Matrix event a claimed delivery produced, if nothing else has.

    Returns whether this call is the one that bound the row. Acknowledgement is
    first-writer-wins, so a second caller for the same delivery changes nothing
    here -- and anything it wanted to write *beside* the acknowledgement must
    not be written either. A losing caller that carried on would leave the row
    naming one event and whatever it wrote naming another.

    The conditional update reports its own ownership through ``RETURNING``,
    which is the only way this answer is trustworthy. Reading the column first
    and then updating it looks equivalent and is not: both readers can see a
    null, and the loser's update then matches zero rows while it still believes
    it won.

    Two callers for one row is ordinary, not exotic, and does not need a second
    process to arrive at. ``_recover_unacknowledged_deliveries`` runs after
    every sync response and flushes each unacknowledged row, while the live
    turn that owns that row is still inside its own flush -- the row stays
    unacknowledged across the network send, and nothing excludes the two. Two
    stores over one PostgreSQL database reach it as well, and that is how it
    was reproduced: both returned success, the outbox named the first event and
    the terminal record named the second. Only the writing statement knows
    whether it wrote.
    """
    bound = transaction.fetchone(
        """
        UPDATE response_outbox SET acknowledged_event_id = ?
        WHERE principal_id = ? AND turn_id = ? AND stage = ? AND acknowledged_event_id IS NULL
        RETURNING turn_id
        """,
        (event_id, principal_id, turn_id, stage.value),
    )
    return bound is not None


def unacknowledged(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
    after: tuple[int, str, str] | None = None,
) -> tuple[OutboxDelivery, ...]:
    """Return deliveries that may or may not have reached Matrix, oldest first.

    ``after`` resumes past a row already visited, in the same order the scan
    uses. A failed delivery stays unacknowledged on purpose, so without a
    cursor a page of failures is re-read forever and nothing behind it is ever
    attempted.
    """
    # turn_id and stage shipped as unpinned TEXT and cannot be retyped without
    # rewriting the table, so the byte-order pin goes on the comparison itself.
    # Without it a server whose collation is not byte order sorts these
    # differently from the cursor's own comparison, and the scan skips rows or
    # revisits them.
    cursor_clause = "" if after is None else " AND (created_at_ns, turn_id/*bytes*/, stage/*bytes*/) > (?, ?, ?)"
    cursor_params: tuple[object, ...] = () if after is None else after
    rows = transaction.fetchall(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM response_outbox
        WHERE principal_id = ? AND acknowledged_event_id IS NULL{cursor_clause}
        ORDER BY created_at_ns, turn_id/*bytes*/, stage/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and a fixed clause, not input
        (principal_id, *cursor_params, limit),
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
        raise TypeError(msg)
    return OutboxDelivery(
        turn_id=row["turn_id"],
        stage=DeliveryStage(row["stage"]),
        room_id=row["room_id"],
        thread_id=decode_thread_id(row["thread_id"]),
        transaction_id=row["transaction_id"],
        payload=payload,
        edits_event_id=row["edits_event_id"],
        acknowledged_event_id=row["acknowledged_event_id"],
        created_at_ns=int(row["created_at_ns"]),
        attempted=bool(row["attempted"]),
        sending_device_id=row["sending_device_id"],
    )
