"""Durable reply records: the single owner of each agent or team reply's state.

Rows hold presentations as opaque JSON written by the reply layer, so this
module never imports tool-system or presentation types.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.reply_lifecycle import (
    LegacyPending,
    OwedWrite,
    Reply,
    ReplyState,
    VisibilityPolicy,
    departed,
)

from . import reply_spans

if TYPE_CHECKING:
    from mindroom.reply_lifecycle import Span, Transition

    from .backend import Row, Transaction

_REPLY_COLUMNS = """
    reply_id, entity_name, room_id, thread_id, membership_epoch, requester_id, visibility_policy,
    event_id, continuation_event_ids_json, state, current_span_id, last_span_id, presentation_json,
    frozen_display_json, possibly_shown_json, possibly_shown_seq, confirmed_seq, revision, legacy_pending,
    placeholder_only, stop_receipt_order, stop_applied_receipt_order, stop_button_event_id,
    redaction_pending_json, owed_write_json, reply_sequence, approval_id, created_at_ns, updated_at_ns
"""


def _ids_json(event_ids: tuple[str, ...]) -> str | None:
    return json.dumps(list(event_ids), separators=(",", ":")) if event_ids else None


def _ids(stored: object) -> tuple[str, ...]:
    if stored is None:
        return ()
    raw = json.loads(str(stored))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        msg = "Stored reply event id list is malformed"
        raise TypeError(msg)
    return tuple(cast("list[str]", raw))


def _owed_json(owed: OwedWrite | None) -> str | None:
    if owed is None:
        return None
    return json.dumps(
        {"span_id": owed.span_id, "note": owed.note, "text": owed.text},
        separators=(",", ":"),
        sort_keys=True,
    )


def _owed(stored: object) -> OwedWrite | None:
    if stored is None:
        return None
    raw = json.loads(str(stored))
    if not isinstance(raw, dict):
        msg = "Stored owed reply write is malformed"
        raise TypeError(msg)
    data = cast("dict[str, object]", raw)
    text = data.get("text")
    return OwedWrite(
        span_id=str(data["span_id"]),
        note=str(data["note"]),
        text=text if isinstance(text, str) else None,
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(cast("int", value))


def _reply(row: Row) -> Reply:
    legacy_pending = row["legacy_pending"]
    return Reply(
        reply_id=str(row["reply_id"]),
        entity_name=str(row["entity_name"]),
        room_id=str(row["room_id"]),
        thread_id=cast("str | None", row["thread_id"]),
        membership_epoch=int(row["membership_epoch"]),
        requester_id=str(row["requester_id"]),
        visibility_policy=VisibilityPolicy(str(row["visibility_policy"])),
        state=ReplyState(str(row["state"])),
        last_span_id=str(row["last_span_id"]),
        presentation=str(row["presentation_json"]),
        revision=int(row["revision"]),
        reply_sequence=int(row["reply_sequence"]),
        created_at_ns=int(row["created_at_ns"]),
        updated_at_ns=int(row["updated_at_ns"]),
        event_id=cast("str | None", row["event_id"]),
        continuation_event_ids=_ids(row["continuation_event_ids_json"]),
        current_span_id=cast("str | None", row["current_span_id"]),
        frozen_display=cast("str | None", row["frozen_display_json"]),
        possibly_shown=cast("str | None", row["possibly_shown_json"]),
        possibly_shown_seq=_optional_int(row["possibly_shown_seq"]),
        confirmed_seq=_optional_int(row["confirmed_seq"]),
        legacy_pending=None if legacy_pending is None else LegacyPending(str(legacy_pending)),
        placeholder_only=bool(row["placeholder_only"]),
        stop_receipt_order=_optional_int(row["stop_receipt_order"]),
        stop_applied_receipt_order=_optional_int(row["stop_applied_receipt_order"]),
        stop_button_event_id=cast("str | None", row["stop_button_event_id"]),
        redaction_pending=_ids(row["redaction_pending_json"]),
        approval_id=cast("str | None", row["approval_id"]),
        owed_write=_owed(row["owed_write_json"]),
    )


def load(transaction: Transaction, principal_id: str, reply_id: str) -> Reply | None:
    """Return one reply, if it exists."""
    row = transaction.fetchone(
        f"SELECT {_REPLY_COLUMNS} FROM reply_messages WHERE principal_id = ? AND reply_id = ?",  # noqa: S608
        (principal_id, reply_id),
    )
    return None if row is None else _reply(row)


def lock(transaction: Transaction, principal_id: str, reply_id: str) -> Reply | None:
    """Return one reply after taking its row lock, so concurrent transitions serialize on PostgreSQL."""
    row = transaction.fetchone(
        f"""
        UPDATE reply_messages SET revision = revision
        WHERE principal_id = ? AND reply_id = ?
        RETURNING {_REPLY_COLUMNS}
        """,  # noqa: S608 - a fixed column list
        (principal_id, reply_id),
    )
    return None if row is None else _reply(row)


def for_event(transaction: Transaction, principal_id: str, event_id: str) -> Reply | None:
    """Return the reply bound to one Matrix event."""
    row = transaction.fetchone(
        f"SELECT {_REPLY_COLUMNS} FROM reply_messages WHERE principal_id = ? AND event_id = ?",  # noqa: S608
        (principal_id, event_id),
    )
    return None if row is None else _reply(row)


def for_room(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    *,
    states: tuple[ReplyState, ...],
) -> tuple[Reply, ...]:
    """Return a room's replies in the given states, oldest first."""
    placeholders = ", ".join("?" for _ in states)
    rows = transaction.fetchall(
        f"""
        SELECT {_REPLY_COLUMNS} FROM reply_messages
        WHERE principal_id = ? AND room_id = ? AND state IN ({placeholders})
        ORDER BY created_at_ns, reply_id
        """,  # noqa: S608 - fixed columns and placeholders
        (principal_id, room_id, *(state.value for state in states)),
    )
    return tuple(_reply(row) for row in rows)


def in_states(transaction: Transaction, principal_id: str, states: tuple[ReplyState, ...]) -> tuple[Reply, ...]:
    """Return this principal's replies in the given states, oldest first."""
    placeholders = ", ".join("?" for _ in states)
    rows = transaction.fetchall(
        f"""
        SELECT {_REPLY_COLUMNS} FROM reply_messages
        WHERE principal_id = ? AND state IN ({placeholders})
        ORDER BY created_at_ns, reply_id
        """,  # noqa: S608 - fixed columns and placeholders
        (principal_id, *(state.value for state in states)),
    )
    return tuple(_reply(row) for row in rows)


def event_ids_of_spans(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    span_ids: tuple[str, ...],
) -> frozenset[str]:
    """Return the bound events of a room's replies whose current span is one of these."""
    placeholders = ", ".join("?" for _ in span_ids)
    rows = transaction.fetchall(
        f"""
        SELECT event_id FROM reply_messages
        WHERE principal_id = ? AND room_id = ? AND event_id IS NOT NULL AND current_span_id IN ({placeholders})
        """,  # noqa: S608 - fixed placeholders
        (principal_id, room_id, *span_ids),
    )
    return frozenset(str(row["event_id"]) for row in rows)


def depart_room(transaction: Transaction, principal_id: str, room_id: str, *, now_ns: int) -> None:
    """End the room's replies as its membership ends, without touching Matrix (DESIGN.md §6.4 ``departed``).

    Running replies end gone with their spans released, and what finished
    replies still owed the room is dropped. The departing bot cancels the span
    tasks it runs after this commits.
    """
    rows = transaction.fetchall(
        f"""
        SELECT {_REPLY_COLUMNS} FROM reply_messages
        WHERE principal_id = ? AND room_id = ?
          AND (state IN ('active', 'paused') OR redaction_pending_json IS NOT NULL OR owed_write_json IS NOT NULL)
        ORDER BY created_at_ns, reply_id
        """,  # noqa: S608 - a fixed column list
        (principal_id, room_id),
    )
    for reply in (_reply(row) for row in rows):
        current = (
            None
            if reply.current_span_id is None
            else reply_spans.load(transaction, principal_id, reply.current_span_id)
        )
        persist(transaction, principal_id, departed(reply, current, now_ns=now_ns))


def spans_in_room(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    span_ids: tuple[str, ...],
) -> frozenset[str]:
    """Return which of these spans belong to the room's replies."""
    placeholders = ", ".join("?" for _ in span_ids)
    rows = transaction.fetchall(
        f"""
        SELECT span.span_id FROM reply_spans AS span
        JOIN reply_messages AS reply ON reply.principal_id = span.principal_id AND reply.reply_id = span.reply_id
        WHERE span.principal_id = ? AND reply.room_id = ? AND span.span_id IN ({placeholders})
        """,  # noqa: S608 - fixed placeholders
        (principal_id, room_id, *span_ids),
    )
    return frozenset(str(row["span_id"]) for row in rows)


def open_replies(transaction: Transaction) -> tuple[tuple[str, Reply], ...]:
    """Return every principal's replies not yet terminal, with their principal."""
    rows = transaction.fetchall(
        f"""
        SELECT principal_id, {_REPLY_COLUMNS} FROM reply_messages
        WHERE state IN ('active', 'paused')
        ORDER BY principal_id, created_at_ns, reply_id
        """,  # noqa: S608 - a fixed column list
    )
    return tuple((str(row["principal_id"]), _reply(row)) for row in rows)


def with_pending_work(transaction: Transaction, principal_id: str) -> tuple[Reply, ...]:
    """Return replies owing a redaction or a write that has not been enqueued yet."""
    rows = transaction.fetchall(
        f"""
        SELECT {_REPLY_COLUMNS} FROM reply_messages
        WHERE principal_id = ? AND (redaction_pending_json IS NOT NULL OR owed_write_json IS NOT NULL)
        ORDER BY created_at_ns, reply_id
        """,  # noqa: S608 - a fixed column list
        (principal_id,),
    )
    return tuple(_reply(row) for row in rows)


def for_sources(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> Reply | None:
    """Return the most recently created reply a span of which answers any of these sources."""
    reply_ids = reply_spans.reply_ids_for_sources(transaction, principal_id, event_ids)
    return None if not reply_ids else load(transaction, principal_id, reply_ids[0])


def save(transaction: Transaction, principal_id: str, reply: Reply) -> None:
    """Insert or replace one reply row."""
    transaction.execute(
        """
        INSERT INTO reply_messages (
            principal_id, reply_id, entity_name, room_id, thread_id, membership_epoch, requester_id,
            visibility_policy, event_id, continuation_event_ids_json, state, current_span_id, last_span_id,
            presentation_json, frozen_display_json, possibly_shown_json, possibly_shown_seq, confirmed_seq,
            revision, legacy_pending, placeholder_only, stop_receipt_order, stop_applied_receipt_order,
            stop_button_event_id, redaction_pending_json, owed_write_json, reply_sequence, approval_id,
            created_at_ns, updated_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, reply_id) DO UPDATE SET
            event_id = excluded.event_id,
            continuation_event_ids_json = excluded.continuation_event_ids_json,
            state = excluded.state,
            current_span_id = excluded.current_span_id,
            last_span_id = excluded.last_span_id,
            presentation_json = excluded.presentation_json,
            frozen_display_json = excluded.frozen_display_json,
            possibly_shown_json = excluded.possibly_shown_json,
            possibly_shown_seq = excluded.possibly_shown_seq,
            confirmed_seq = excluded.confirmed_seq,
            revision = excluded.revision,
            legacy_pending = excluded.legacy_pending,
            placeholder_only = excluded.placeholder_only,
            stop_receipt_order = excluded.stop_receipt_order,
            stop_applied_receipt_order = excluded.stop_applied_receipt_order,
            stop_button_event_id = excluded.stop_button_event_id,
            redaction_pending_json = excluded.redaction_pending_json,
            owed_write_json = excluded.owed_write_json,
            reply_sequence = excluded.reply_sequence,
            approval_id = excluded.approval_id,
            updated_at_ns = excluded.updated_at_ns
        """,
        (
            principal_id,
            reply.reply_id,
            reply.entity_name,
            reply.room_id,
            reply.thread_id,
            reply.membership_epoch,
            reply.requester_id,
            reply.visibility_policy.value,
            reply.event_id,
            _ids_json(reply.continuation_event_ids),
            reply.state.value,
            reply.current_span_id,
            reply.last_span_id,
            reply.presentation,
            reply.frozen_display,
            reply.possibly_shown,
            reply.possibly_shown_seq,
            reply.confirmed_seq,
            reply.revision,
            None if reply.legacy_pending is None else reply.legacy_pending.value,
            reply.placeholder_only,
            reply.stop_receipt_order,
            reply.stop_applied_receipt_order,
            reply.stop_button_event_id,
            _ids_json(reply.redaction_pending),
            _owed_json(reply.owed_write),
            reply.reply_sequence,
            reply.approval_id,
            reply.created_at_ns,
            reply.updated_at_ns,
        ),
    )


def persist(transaction: Transaction, principal_id: str, transition: Transition) -> None:
    """Write the reply and spans one applied transition produced."""
    if not transition.applied:
        if transition.spans:
            # A claim deferred for durable-write debt can still end a span an
            # older bot instance left current.
            _persist_spans(transaction, principal_id, transition.spans)
            if transition.reply is not None:
                save(transaction, principal_id, transition.reply)
        return
    if transition.reply is not None:
        save(transaction, principal_id, transition.reply)
    _persist_spans(transaction, principal_id, transition.spans)


def _persist_spans(transaction: Transaction, principal_id: str, spans: tuple[Span, ...]) -> None:
    for span in spans:
        reply_spans.save(transaction, principal_id, span)


def has_unresolved_create(transaction: Transaction, principal_id: str, room_id: str) -> bool:
    """Return whether a running reply in the room offered a create the homeserver has not acknowledged yet.

    Only then can a Stop name an event no reply is bound to: the user saw the
    event before this bot learned which one it was.
    """
    row = transaction.fetchone(
        """
        SELECT 1 AS present FROM reply_messages AS reply
        WHERE reply.principal_id = ? AND reply.room_id = ? AND reply.event_id IS NULL
          AND reply.state IN ('active', 'paused')
          AND EXISTS (
            SELECT 1 FROM matrix_delivery_outbox AS row
            WHERE row.principal_id = reply.principal_id AND row.reply_id = reply.reply_id
              AND row.stage IN ('initial', 'final') AND row.edits_event_id IS NULL
              AND row.attempted = 1 AND row.acknowledged_event_id IS NULL
              AND row.retired = 0 AND row.permanent_failure_reason IS NULL
          )
        LIMIT 1
        """,
        (principal_id, room_id),
    )
    return row is not None


def record_pending_stop(
    transaction: Transaction,
    principal_id: str,
    *,
    target_event_id: str,
    receipt_order: int,
    room_id: str,
    now_ns: int,
) -> bool:
    """Store a Stop for an event no reply is bound to yet; return whether it is newer than any stored."""
    row = transaction.fetchone(
        """
        INSERT INTO pending_reply_stops (principal_id, target_event_id, receipt_order, room_id, created_at_ns)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, target_event_id) DO UPDATE SET receipt_order = excluded.receipt_order
        WHERE pending_reply_stops.receipt_order < excluded.receipt_order
        RETURNING receipt_order
        """,
        (principal_id, target_event_id, receipt_order, room_id, now_ns),
    )
    return row is not None


def take_pending_stop(transaction: Transaction, principal_id: str, event_id: str) -> tuple[int, str] | None:
    """Remove and return the pending Stop for one event, as its receipt order and room."""
    row = transaction.fetchone(
        """
        DELETE FROM pending_reply_stops WHERE principal_id = ? AND target_event_id = ?
        RETURNING receipt_order, room_id
        """,
        (principal_id, event_id),
    )
    return None if row is None else (int(row["receipt_order"]), str(row["room_id"]))


def write_generation(transaction: Transaction, principal_id: str, *, generation: str, now_ns: int) -> None:
    """Make one bot instance the owner of this principal's replies."""
    transaction.execute(
        """
        INSERT INTO reply_principal_generations (principal_id, generation, started_at_ns)
        VALUES (?, ?, ?)
        ON CONFLICT (principal_id) DO UPDATE SET
            generation = excluded.generation, started_at_ns = excluded.started_at_ns
        """,
        (principal_id, generation, now_ns),
    )


def active_generation(transaction: Transaction, principal_id: str) -> str | None:
    """Return the bot instance that owns this principal's replies now."""
    row = transaction.fetchone(
        "SELECT generation FROM reply_principal_generations WHERE principal_id = ?",
        (principal_id,),
    )
    return None if row is None else str(row["generation"])
