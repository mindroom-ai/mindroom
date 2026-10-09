"""Durable spans: one claim on a reply by one executor, with immutable sources."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.reply_lifecycle import ReplyState, Rollback, Span, SpanKind, SpanOutcome, SpanSources

if TYPE_CHECKING:
    from .backend import Row, Transaction

_SPAN_COLUMNS = """
    span_id, reply_id, kind, delivery_id, approval_id, bot_generation,
    base_sequence, rollback_json, outcome, claimed_at_ns
"""
_ROLES = ("pending", "logical", "discovery")


def _rollback_json(rollback: Rollback | None) -> str | None:
    if rollback is None:
        return None
    return json.dumps(
        {
            "presentation": rollback.presentation,
            "state": rollback.state.value,
            "possibly_shown": rollback.possibly_shown,
            "possibly_shown_seq": rollback.possibly_shown_seq,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _rollback(stored: str | None) -> Rollback | None:
    if stored is None:
        return None
    raw = json.loads(stored)
    if not isinstance(raw, dict):
        msg = "Stored rollback snapshot is not an object"
        raise TypeError(msg)
    data = cast("dict[str, object]", raw)
    presentation = data["presentation"]
    if not isinstance(presentation, str) or not isinstance(data["state"], str):
        msg = "Stored rollback snapshot is malformed"
        raise TypeError(msg)
    return Rollback(
        presentation=presentation,
        state=ReplyState(data["state"]),
        possibly_shown=shown if isinstance((shown := data.get("possibly_shown")), str) else None,
        possibly_shown_seq=shown_seq if isinstance((shown_seq := data.get("possibly_shown_seq")), int) else None,
    )


def _sources(transaction: Transaction, principal_id: str, span_id: str) -> SpanSources:
    rows = transaction.fetchall(
        """
        SELECT role, event_id FROM reply_span_sources
        WHERE principal_id = ? AND span_id = ?
        ORDER BY role, ordinal
        """,
        (principal_id, span_id),
    )
    by_role: dict[str, list[str]] = {role: [] for role in _ROLES}
    for row in rows:
        by_role[str(row["role"])].append(str(row["event_id"]))
    return SpanSources(
        pending=tuple(by_role["pending"]),
        logical=tuple(by_role["logical"]),
        discovery=tuple(by_role["discovery"]),
    )


def _span(transaction: Transaction, principal_id: str, row: Row) -> Span:
    outcome = row["outcome"]
    return Span(
        span_id=str(row["span_id"]),
        reply_id=str(row["reply_id"]),
        kind=SpanKind(str(row["kind"])),
        delivery_id=str(row["delivery_id"]),
        sources=_sources(transaction, principal_id, str(row["span_id"])),
        bot_generation=str(row["bot_generation"]),
        claimed_at_ns=int(row["claimed_at_ns"]),
        base_sequence=int(row["base_sequence"]),
        approval_id=cast("str | None", row["approval_id"]),
        rollback=_rollback(cast("str | None", row["rollback_json"])),
        outcome=None if outcome is None else SpanOutcome(str(outcome)),
    )


def load(transaction: Transaction, principal_id: str, span_id: str) -> Span | None:
    """Return one span, if it exists."""
    row = transaction.fetchone(
        f"SELECT {_SPAN_COLUMNS} FROM reply_spans WHERE principal_id = ? AND span_id = ?",  # noqa: S608
        (principal_id, span_id),
    )
    return None if row is None else _span(transaction, principal_id, row)


def latest_for_delivery(transaction: Transaction, principal_id: str, delivery_id: str) -> Span | None:
    """Return the latest span that uses one delivery id."""
    row = transaction.fetchone(
        f"""
        SELECT {_SPAN_COLUMNS} FROM reply_spans
        WHERE principal_id = ? AND delivery_id/*bytes*/ = ?
        ORDER BY claimed_at_ns DESC, span_id DESC
        LIMIT 1
        """,  # noqa: S608 - a fixed column list
        (principal_id, delivery_id),
    )
    return None if row is None else _span(transaction, principal_id, row)


def for_reply(transaction: Transaction, principal_id: str, reply_id: str) -> tuple[Span, ...]:
    """Return every span of one reply, oldest first."""
    rows = transaction.fetchall(
        f"""
        SELECT {_SPAN_COLUMNS} FROM reply_spans
        WHERE principal_id = ? AND reply_id = ?
        ORDER BY claimed_at_ns, span_id
        """,  # noqa: S608 - a fixed column list
        (principal_id, reply_id),
    )
    return tuple(_span(transaction, principal_id, row) for row in rows)


def newest_reply_id_for_sources(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> str | None:
    """Return the newest reply with a span answering any of these pending or logical sources."""
    if not event_ids:
        return None
    placeholders = ", ".join("?" for _ in event_ids)
    row = transaction.fetchone(
        f"""
        SELECT span.reply_id AS reply_id, MAX(reply.created_at_ns) AS created_at_ns
        FROM reply_span_sources AS source
        JOIN reply_spans AS span
          ON span.principal_id = source.principal_id AND span.span_id = source.span_id
        JOIN reply_messages AS reply
          ON reply.principal_id = span.principal_id AND reply.reply_id = span.reply_id
        WHERE source.principal_id = ? AND source.role IN ('pending', 'logical')
          AND source.event_id IN ({placeholders})
        GROUP BY span.reply_id
        ORDER BY created_at_ns DESC, span.reply_id DESC
        LIMIT 1
        """,  # noqa: S608 - placeholders only
        (principal_id, *event_ids),
    )
    return None if row is None else str(row["reply_id"])


def save(transaction: Transaction, principal_id: str, span: Span) -> None:
    """Insert a new span with its sources, or write the outcome of an existing one.

    Everything but the outcome is fixed at claim; an update that tried to
    change it would be a lifecycle bug, so the update touches only the
    write-once columns and only while they are still empty.
    """
    existing = transaction.fetchone(
        "SELECT outcome FROM reply_spans WHERE principal_id = ? AND span_id = ?",
        (principal_id, span.span_id),
    )
    if existing is None:
        transaction.execute(
            """
            INSERT INTO reply_spans (
                principal_id, span_id, reply_id, kind, delivery_id, approval_id,
                bot_generation, base_sequence, rollback_json, outcome, claimed_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                span.span_id,
                span.reply_id,
                span.kind.value,
                span.delivery_id,
                span.approval_id,
                span.bot_generation,
                span.base_sequence,
                _rollback_json(span.rollback),
                None if span.outcome is None else span.outcome.value,
                span.claimed_at_ns,
            ),
        )
        roles = (
            ("pending", span.sources.pending),
            ("logical", span.sources.logical),
            ("discovery", span.sources.discovery),
        )
        for role, event_ids in roles:
            for ordinal, event_id in enumerate(event_ids):
                transaction.execute(
                    """
                    INSERT INTO reply_span_sources (principal_id, span_id, event_id, role, ordinal)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (principal_id, span.span_id, event_id, role, ordinal),
                )
        return
    if span.outcome is None:
        return
    if existing["outcome"] is not None:
        if str(existing["outcome"]) != span.outcome.value:
            msg = f"Span {span.span_id} already ended {existing['outcome']}"
            raise RuntimeError(msg)
        return
    transaction.execute(
        """
        UPDATE reply_spans SET outcome = ?
        WHERE principal_id = ? AND span_id = ? AND outcome IS NULL
        """,
        (span.outcome.value, principal_id, span.span_id),
    )


def record_tool_call(
    transaction: Transaction,
    principal_id: str,
    *,
    span_id: str,
    call_id: str,
    entry_json: str,
    now_ns: int,
) -> None:
    """Record or update one tool call a span made, keeping when it was first recorded."""
    transaction.execute(
        """
        INSERT INTO reply_tool_calls (principal_id, span_id, call_id, entry_json, recorded_at_ns)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, span_id, call_id) DO UPDATE SET entry_json = excluded.entry_json
        """,
        (principal_id, span_id, call_id, entry_json, now_ns),
    )


def tool_calls(transaction: Transaction, principal_id: str, span_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return the recorded tool calls of these spans, in the order they started."""
    if not span_ids:
        return ()
    placeholders = ", ".join("?" for _ in span_ids)
    rows = transaction.fetchall(
        f"""
        SELECT entry_json FROM reply_tool_calls
        WHERE principal_id = ? AND span_id IN ({placeholders})
        ORDER BY recorded_at_ns, call_id
        """,  # noqa: S608 - fixed placeholders
        (principal_id, *span_ids),
    )
    return tuple(str(row["entry_json"]) for row in rows)
