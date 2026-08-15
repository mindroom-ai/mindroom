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
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from mindroom.interactive_models import INTERACTIVE_PROMPT_KEY

from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .membership_state import claim_membership_epoch
from .models import DURABLE_DELIVERY_ID_KEY, DeliveryStage, MatrixDelivery, UnreadableMatrixDelivery

if TYPE_CHECKING:
    from .backend import Row, Transaction

_OUTBOX_COLUMNS = """
    delivery_id, stage, event_type, room_id, membership_epoch, thread_id, transaction_id,
    payload_json, edits_event_id, acknowledged_event_id, created_at_ns,
    attempted, retired, sending_device_id
"""
_DELIVERY_STAGE_VALUES = frozenset(item.value for item in DeliveryStage)


def _delivery_payload(
    principal_id: str,
    delivery_id: str,
    stage: DeliveryStage,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Return one frozen payload carrying its stable journal identity."""
    identity = {
        "principal": principal_id,
        "delivery_id": delivery_id,
        "stage": stage.value,
    }
    frozen = {
        **payload,
        DURABLE_DELIVERY_ID_KEY: identity,
    }
    replacement = payload.get("m.new_content")
    if isinstance(replacement, Mapping):
        frozen["m.new_content"] = {**replacement, DURABLE_DELIVERY_ID_KEY: identity}
    return frozen


def delivery_payload_json(
    principal_id: str,
    delivery_id: str,
    stage: DeliveryStage,
    payload: Mapping[str, object],
) -> str:
    """Return the canonical stored Matrix payload, including its identity."""
    return json.dumps(
        _delivery_payload(principal_id, delivery_id, stage, payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _delivery_identity(content: Mapping[str, object] | None) -> tuple[str, str, DeliveryStage] | None:
    """Return the stable outbox identity carried by one Matrix payload."""
    if content is None:
        return None
    raw_identity = content.get(DURABLE_DELIVERY_ID_KEY)
    if not isinstance(raw_identity, dict):
        return None
    identity = cast("dict[str, object]", raw_identity)
    principal_id = identity.get("principal")
    delivery_id = identity.get("delivery_id")
    stage = identity.get("stage")
    if (
        not isinstance(principal_id, str)
        or not principal_id
        or not isinstance(delivery_id, str)
        or not delivery_id
        or not isinstance(stage, str)
        or stage not in _DELIVERY_STAGE_VALUES
    ):
        return None
    return principal_id, delivery_id, DeliveryStage(stage)


def _lock_delivery_stages(transaction: Transaction, principal_id: str, delivery_id: str) -> None:
    """Serialize stage decisions on the INITIAL row shared by both stages."""
    transaction.execute(
        """
        UPDATE matrix_delivery_outbox SET attempted = attempted
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,
        (principal_id, delivery_id, DeliveryStage.INITIAL.value),
    )


def enqueue(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    event_type: str,
    room_id: str,
    membership_epoch: int,
    thread_id: str | None,
    payload: Mapping[str, object],
    edits_event_id: str | None,
    edit_target_pending: bool = False,
) -> str | None:
    """Record delivery intent without changing its durable membership owner."""
    _lock_delivery_stages(transaction, principal_id, delivery_id)
    existing_owner = transaction.fetchone(
        """
        SELECT room_id, membership_epoch, retired FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ?
        LIMIT 1
        """,
        (principal_id, delivery_id),
    )
    if existing_owner is not None and (
        bool(existing_owner["retired"])
        or str(existing_owner["room_id"]) != room_id
        or int(existing_owner["membership_epoch"]) != membership_epoch
    ):
        return None
    if stage is DeliveryStage.FINAL and edit_target_pending:
        initial = transaction.fetchone(
            """
            SELECT acknowledged_event_id FROM matrix_delivery_outbox
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
            """,
            (principal_id, delivery_id, DeliveryStage.INITIAL.value),
        )
        if initial is not None and initial["acknowledged_event_id"] is not None:
            edits_event_id = str(initial["acknowledged_event_id"])
            edit_target_pending = False
    transaction_id = delivery_transaction_id(principal_id, delivery_id, stage.value)
    transaction.execute(
        """
        INSERT INTO matrix_delivery_outbox (
            principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
            thread_id, transaction_id, payload_json, edits_event_id,
            edit_target_pending, attempted, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT (principal_id, delivery_id, stage) DO UPDATE SET
            room_id = excluded.room_id,
            membership_epoch = excluded.membership_epoch,
            thread_id = excluded.thread_id,
            event_type = excluded.event_type,
            payload_json = excluded.payload_json,
            edits_event_id = excluded.edits_event_id,
            edit_target_pending = excluded.edit_target_pending
        WHERE matrix_delivery_outbox.attempted = 0
        """,
        (
            principal_id,
            delivery_id,
            stage.value,
            event_type,
            room_id,
            membership_epoch,
            encode_thread_id(thread_id),
            transaction_id,
            delivery_payload_json(principal_id, delivery_id, stage, payload),
            edits_event_id,
            int(edit_target_pending),
            time.time_ns(),
        ),
    )
    return transaction_id


def is_attempted(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
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
        SELECT 1 AS present FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ? AND attempted = 1
        """,
        (principal_id, delivery_id, stage.value),
    )
    return row is not None


def claim(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    sending_device_id: str | None,
    replacement_payload: Mapping[str, object] | None,
) -> MatrixDelivery | None:
    """Freeze one delivery and atomically record its first device intent.

    The conditional update is the PostgreSQL-safe claim winner: only its caller
    sees ``attempted=False``. It also records that winner's device before a
    process can die between this commit and the send. Later claims preserve the
    original marker so a changed device still owes history reconciliation.

    INITIAL and FINAL are also one durable ordering decision. An unattempted
    INITIAL is withdrawn once FINAL exists. An attempted, unacknowledged
    INITIAL is different: Matrix may still accept it, so FINAL cannot be
    offered under its distinct transaction ID until retrying INITIAL has
    resolved that unknown outcome. Locking the INITIAL row makes those
    checks atomic with both claiming and insertion on PostgreSQL as well as
    SQLite.
    """
    _lock_delivery_stages(transaction, principal_id, delivery_id)
    current = transaction.fetchone(
        """
        SELECT attempted, retired, edits_event_id, edit_target_pending FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,
        (principal_id, delivery_id, stage.value),
    )
    if current is None or bool(current["retired"]):
        return None
    if replacement_payload is not None and not bool(current["attempted"]):
        transaction.execute(
            """
            UPDATE matrix_delivery_outbox SET payload_json = ?
            WHERE principal_id = ? AND delivery_id = ? AND stage = ? AND attempted = 0
            """,
            (
                delivery_payload_json(principal_id, delivery_id, stage, replacement_payload),
                principal_id,
                delivery_id,
                stage.value,
            ),
        )
    if stage is DeliveryStage.INITIAL and not bool(current["attempted"]):
        final = transaction.fetchone(
            """
            SELECT edit_target_pending FROM matrix_delivery_outbox
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
            """,
            (principal_id, delivery_id, DeliveryStage.FINAL.value),
        )
        if final is not None and not bool(final["edit_target_pending"]):
            transaction.execute(
                """
                DELETE FROM matrix_delivery_outbox
                WHERE principal_id = ? AND delivery_id = ? AND stage = ? AND attempted = 0
                """,
                (principal_id, delivery_id, DeliveryStage.INITIAL.value),
            )
            return None
    current_edits_event_id = current["edits_event_id"]
    if stage is DeliveryStage.FINAL and bool(current["edit_target_pending"]):
        initial = transaction.fetchone(
            """
            SELECT acknowledged_event_id FROM matrix_delivery_outbox
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
            """,
            (principal_id, delivery_id, DeliveryStage.INITIAL.value),
        )
        if initial is None or initial["acknowledged_event_id"] is None:
            return None
        current_edits_event_id = initial["acknowledged_event_id"]
        transaction.execute(
            """
            UPDATE matrix_delivery_outbox
            SET edits_event_id = ?, edit_target_pending = 0
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
            """,
            (current_edits_event_id, principal_id, delivery_id, DeliveryStage.FINAL.value),
        )
    if stage is DeliveryStage.FINAL and current_edits_event_id is None:
        unresolved_initial = transaction.fetchone(
            """
            SELECT 1 AS present FROM matrix_delivery_outbox
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
              AND attempted = 1 AND acknowledged_event_id IS NULL
              AND retired = 0
            """,
            (principal_id, delivery_id, DeliveryStage.INITIAL.value),
        )
        if unresolved_initial is not None:
            return None
    marked = transaction.fetchone(
        """
        UPDATE matrix_delivery_outbox SET attempted = 1, sending_device_id = ?
        WHERE principal_id = ? AND delivery_id = ? AND stage = ? AND attempted = 0
        RETURNING delivery_id
        """,
        (sending_device_id, principal_id, delivery_id, stage.value),
    )
    row = transaction.fetchone(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, delivery_id, stage.value),
    )
    if row is None:
        return None
    return replace(_delivery(row), attempted=marked is None)


def record_matrix_delivery_device(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
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
        UPDATE matrix_delivery_outbox SET sending_device_id = ?
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,
        (device_id, principal_id, delivery_id, stage.value),
    )


def acknowledge(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
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
    process to arrive at. ``_recover_unacknowledged_matrix_deliveries`` runs after
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
        UPDATE matrix_delivery_outbox SET acknowledged_event_id = ?
        WHERE principal_id = ? AND delivery_id = ? AND stage = ? AND acknowledged_event_id IS NULL
        RETURNING delivery_id
        """,
        (event_id, principal_id, delivery_id, stage.value),
    )
    if bound is not None and stage is DeliveryStage.INITIAL:
        transaction.execute(
            """
            UPDATE matrix_delivery_outbox
            SET edits_event_id = ?, edit_target_pending = 0
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
              AND attempted = 0
              AND edit_target_pending = 1
            """,
            (event_id, principal_id, delivery_id, DeliveryStage.FINAL.value),
        )
    return bound is not None


def delivery_ownership(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
) -> tuple[str, int] | None:
    """Return one delivery's room and frozen membership, if the row exists."""
    row = transaction.fetchone(
        """
        SELECT room_id, membership_epoch FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,
        (principal_id, delivery_id, stage.value),
    )
    if row is None:
        return None
    return str(row["room_id"]), int(row["membership_epoch"])


def turn_ownership(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
) -> tuple[str, int] | None:
    """Return the room and membership shared by one delivery's stages."""
    row = transaction.fetchone(
        """
        SELECT room_id, membership_epoch FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ?
        LIMIT 1
        """,
        (principal_id, delivery_id),
    )
    if row is None:
        return None
    return str(row["room_id"]), int(row["membership_epoch"])


def _projection_ownership(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
) -> tuple[str, int] | None:
    """Lock one delivery and return its projection owner unless it was retired."""
    row = transaction.fetchone(
        """
        UPDATE matrix_delivery_outbox SET retired = retired
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        RETURNING room_id, membership_epoch, retired
        """,
        (principal_id, delivery_id, stage.value),
    )
    if row is None or bool(row["retired"]):
        return None
    return str(row["room_id"]), int(row["membership_epoch"])


def claim_active_delivery_ownership(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    expected_room_id: str | None = None,
) -> tuple[str, int] | None:
    """Claim one delivery's membership and row while both remain current."""
    ownership = delivery_ownership(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
    )
    if ownership is None:
        return None
    room_id, membership_epoch = ownership
    if expected_room_id is not None and room_id != expected_room_id:
        return None
    membership_is_current = claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    )
    locked_ownership = _projection_ownership(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
    )
    return ownership if membership_is_current and locked_ownership == ownership else None


def event_belongs_to_membership(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    event_id: str,
    membership_epoch: int,
    sender: str,
    transaction_id: str | None = None,
    content: Mapping[str, object] | None = None,
) -> bool:
    """Return whether a delivery event belongs to this room membership."""
    delivery: Row | None = None
    delivery_identity = _delivery_identity(content)
    sender_owns_principal = principal_id.endswith(f"@{sender}")
    if delivery_identity is not None and sender_owns_principal:
        marked_principal, delivery_id, stage = delivery_identity
        if marked_principal == principal_id:
            delivery = transaction.fetchone(
                """
                SELECT delivery_id, stage, room_id, membership_epoch FROM matrix_delivery_outbox
                WHERE principal_id = ? AND delivery_id = ? AND stage = ?
                """,
                (principal_id, delivery_id, stage.value),
            )
            if delivery is not None and str(delivery["room_id"]) != room_id:
                delivery = None
    if delivery is None and sender_owns_principal:
        delivery = transaction.fetchone(
            """
            SELECT delivery_id, stage, room_id, membership_epoch FROM matrix_delivery_outbox
            WHERE principal_id = ? AND room_id = ? AND acknowledged_event_id = ?
            LIMIT 1
            """,
            (principal_id, room_id, event_id),
        )
    if delivery is None and sender_owns_principal and transaction_id is not None:
        delivery = transaction.fetchone(
            """
            SELECT delivery_id, stage, room_id, membership_epoch FROM matrix_delivery_outbox
            WHERE principal_id = ? AND room_id = ? AND transaction_id = ?
            LIMIT 1
            """,
            (principal_id, room_id, transaction_id),
        )
    if delivery is None:
        return True
    owner = claim_active_delivery_ownership(
        transaction,
        principal_id,
        delivery_id=str(delivery["delivery_id"]),
        stage=DeliveryStage(str(delivery["stage"])),
        expected_room_id=room_id,
    )
    return owner == (room_id, membership_epoch)


def unacknowledged(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
    event_type: str,
    after: tuple[int, str, str] | None = None,
) -> tuple[MatrixDelivery | UnreadableMatrixDelivery, ...]:
    """Return deliveries that may or may not have reached Matrix, oldest first.

    ``after`` resumes past a row already visited, in the same order the scan
    uses. A failed delivery stays unacknowledged on purpose, so without a
    cursor a page of failures is re-read forever and nothing behind it is ever
    attempted.
    """
    # delivery_id and stage shipped as unpinned TEXT and cannot be retyped without
    # rewriting the table, so the byte-order pin goes on the comparison itself.
    # Without it a server whose collation is not byte order sorts these
    # differently from the cursor's own comparison, and the scan skips rows or
    # revisits them.
    cursor_clause = "" if after is None else " AND (created_at_ns, delivery_id/*bytes*/, stage/*bytes*/) > (?, ?, ?)"
    cursor_params: tuple[object, ...] = () if after is None else after
    rows = transaction.fetchall(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM matrix_delivery_outbox
        WHERE principal_id = ? AND event_type = ?
          AND acknowledged_event_id IS NULL AND retired = 0{cursor_clause}
        ORDER BY created_at_ns, delivery_id/*bytes*/, stage/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and a fixed clause, not input
        (principal_id, event_type, *cursor_params, limit),
    )
    return tuple(_recovery_delivery(row) for row in rows)


def has_attempted_unacknowledged_prompt_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    membership_epoch: int,
) -> bool:
    """Return whether this membership may show an unprojected prompt or edit."""
    row = transaction.fetchone(
        """
        SELECT 1 AS present FROM matrix_delivery_outbox
        WHERE principal_id = ? AND room_id = ? AND membership_epoch = ?
          AND event_type = 'm.room.message'
          AND attempted = 1 AND retired = 0 AND acknowledged_event_id IS NULL
          AND (
              edits_event_id IS NOT NULL
              OR payload_json LIKE ?
          )
        LIMIT 1
        """,
        (principal_id, room_id, membership_epoch, f'%"{INTERACTIVE_PROMPT_KEY}"%'),
    )
    return row is not None


def load(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
) -> MatrixDelivery | None:
    """Return one delivery without claiming it."""
    row = transaction.fetchone(
        f"""
        SELECT {_OUTBOX_COLUMNS} FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, delivery_id, stage.value),
    )
    return None if row is None else _delivery(row)


def retire(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    room_id: str,
    membership_epoch: int,
) -> MatrixDelivery | None:
    """Retire one obsolete delivery and return its concurrent durable outcome."""
    row = transaction.fetchone(
        f"""
        UPDATE matrix_delivery_outbox SET attempted = attempted
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        RETURNING {_OUTBOX_COLUMNS}
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, delivery_id, stage.value),
    )
    if row is None:
        return None
    delivery = _delivery(row)
    if delivery.room_id != room_id or delivery.membership_epoch != membership_epoch:
        msg = f"Delivery owner changed while retiring {delivery_id!r}/{stage.value!r}"
        raise RuntimeError(msg)
    if delivery.acknowledged_event_id is not None:
        return delivery
    transaction.execute(
        """
        UPDATE matrix_delivery_outbox SET retired = 1
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,
        (principal_id, delivery_id, stage.value),
    )
    return replace(delivery, retired=True)


def _delivery(row: Row) -> MatrixDelivery:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        msg = f"Outbox payload for delivery {row['delivery_id']!r} is not an object"
        raise TypeError(msg)
    return MatrixDelivery(
        delivery_id=row["delivery_id"],
        stage=DeliveryStage(row["stage"]),
        event_type=row["event_type"],
        room_id=row["room_id"],
        membership_epoch=int(row["membership_epoch"]),
        thread_id=decode_thread_id(row["thread_id"]),
        transaction_id=row["transaction_id"],
        payload=payload,
        edits_event_id=row["edits_event_id"],
        acknowledged_event_id=row["acknowledged_event_id"],
        created_at_ns=int(row["created_at_ns"]),
        attempted=bool(row["attempted"]),
        retired=bool(row["retired"]),
        sending_device_id=row["sending_device_id"],
    )


def _recovery_delivery(row: Row) -> MatrixDelivery | UnreadableMatrixDelivery:
    """Decode one recovery row without hiding later durable debt behind it."""
    try:
        return _delivery(row)
    except (json.JSONDecodeError, TypeError) as exc:
        return UnreadableMatrixDelivery(
            delivery_id=str(row["delivery_id"]),
            stage=DeliveryStage(row["stage"]),
            room_id=str(row["room_id"]),
            created_at_ns=int(row["created_at_ns"]),
            error=str(exc),
        )
